"""Authenticated internal commands, before ordinary argparse or data imports."""
from __future__ import annotations

import hashlib
import os
import re
import socket
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from arxiv_digest.update_runtime import protocol


_PREFIX = "--arxiv-digest-internal-"
_MODES = {"self-check", "recover-data", "relaunch", "postterminal-relaunch"}
_REJECTION = "internal update modes require authenticated invocation"


@dataclass
class InternalContext:
    mode: str
    plan_snapshot: protocol.PlanSnapshot
    journal_snapshot: protocol.JournalSnapshot
    boot: dict
    channel: protocol.ControlChannel
    locks: dict

    @property
    def plan(self):
        return self.plan_snapshot.record

    def message(self, kind, **fields):
        return {"schema_version": 1, "attempt_id": self.boot["attempt_id"],
                "nonce": self.boot["nonce"], "kind": kind, **fields}

    def authenticate_message(self, message, kind):
        if (message["kind"] != kind or message["attempt_id"] != self.boot["attempt_id"]
                or message["nonce"] != self.boot["nonce"]
                or message.get("launch_id") != self.boot["launch_id"]):
            raise protocol.ProtocolError("internal decision authentication differs")

    def close(self):
        for lock in self.locks.values():
            lock.close()
        self.locks.clear()
        self.channel.close()


def _authenticate(argv):
    if len(argv) != 4 or not argv[0].startswith(_PREFIX):
        raise protocol.ProtocolError("invalid internal arguments")
    mode = argv[0][len(_PREFIX):]
    if mode not in _MODES or re.fullmatch(r"[1-9][0-9]{0,6}", argv[1]) is None:
        raise protocol.ProtocolError("invalid internal mode or channel")
    fd = int(argv[1])
    if fd < 3 or not stat.S_ISSOCK(os.fstat(fd).st_mode):
        raise protocol.ProtocolError("internal bootstrap is not a socket")
    root = Path(protocol.validate_path(argv[2]))
    protocol._digest(argv[3])
    channel = protocol.ControlChannel(socket.socket(fileno=fd))
    locks = {}
    try:
        boot = channel.receive(time.monotonic() + protocol.SELF_CHECK_TIMEOUT_SECONDS)
        if boot["kind"] != "BOOT" or boot["mode"] != mode or boot["attempt_id"] != argv[3]:
            raise protocol.ProtocolError("internal bootstrap authentication differs")
        plan = protocol.ProtectedPlanStore(root).read_snapshot()
        if (plan is None or plan.sha256 != boot["plan_sha256"]
                or plan.record["attempt_id"] != boot["attempt_id"]
                or Path(plan.record["paths"]["recovery_root"]) != root):
            raise protocol.ProtocolError("internal protected plan differs")
        journal = protocol.JournalStore(root).read_snapshot()
        states = {"self-check": {"target_installed", "rolling_back"},
                  "recover-data": {"rolling_back"}, "relaunch": {"launching_target", "rolling_back"},
                  "postterminal-relaunch": {"complete"}}
        if journal is None or journal.record["state"] not in states[mode]:
            raise protocol.ProtocolError("internal mode does not match journal operation")
        if mode == "relaunch":
            wanted = "updated" if journal.record["state"] == "launching_target" else "restored"
            if boot["outcome"] != wanted:
                raise protocol.ProtocolError("internal relaunch outcome differs")
        # These adapters depend only on standard-library lock primitives. Mutable
        # application, database and profile modules remain unimported until every
        # inherited descriptor and fixed path has been validated.
        from arxiv_digest.update_locks import LockIdentity, LockMode, adopt_borrowed
        for name, descriptor in boot["lock_fds"].items():
            if descriptor == fd:
                raise protocol.ProtocolError("bootstrap and lock descriptor overlap")
            os.set_inheritable(descriptor, False)
            expected = LockIdentity(**plan.record["lock_identities"][name])
            path = Path(plan.record["paths"]["instance_lock"]) if name == "instance" else root / ("update-transition.lock" if name == "transition" else "launcher-operation.lock")
            named = protocol.file_identity(path.lstat())
            if {key: named[key] for key in protocol.IDENTITY_KEYS} != plan.record["lock_identities"][name]:
                raise protocol.ProtocolError("internal lock pathname differs")
            locks[name] = adopt_borrowed(descriptor, expected_identity=expected, mode=LockMode.EXCLUSIVE)
        return InternalContext(mode, plan, journal, boot, channel, locks)
    except BaseException:
        for lock in locks.values():
            lock.close()
        channel.close()
        raise



def recovery_runtime_is_verified(root):
    """Authenticate the fixed wrapper and complete runtime before CLI dispatch."""
    from arxiv_digest.update_runtime import recovery
    try:
        root = Path(root)
        plan = protocol.ProtectedPlanStore(root).read_snapshot()
        if plan is None or Path(plan.record["paths"]["recovery_root"]) != root:
            return False
        for item in plan.record["runtime"]:
            payload, identity = recovery.read_owned_bytes(root / "runtime" / item["name"])
            if identity != item["identity"] or len(payload) != item["size"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
                return False
        wrapper = plan.record["wrapper"]
        payload, identity = recovery.read_owned_bytes(root / "recover-arxiv-digest")
        if identity != wrapper["identity"] or len(payload) != wrapper["size"] or hashlib.sha256(payload).hexdigest() != wrapper["sha256"]:
            return False
        base = plan.record["old_token"]["core"]["interpreter"]
        if recovery.capture_executable_identity(base["path"]) != base:
            return False
        return protocol.ProtectedPlanStore(root).read_snapshot() == plan
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False


def _paths(context):
    from arxiv_digest.paths import _resolved_app_paths
    values = context.plan["paths"]
    paths = _resolved_app_paths(config=Path(values["config_dir"]), data=Path(values["data_dir"]), cache=Path(values["cache_dir"]))
    if paths.update_recovery_dir != Path(values["recovery_root"]) or paths.process_lock_path != Path(values["instance_lock"]):
        raise protocol.ProtocolError("internal application paths differ")
    return paths


def _expected_version(context):
    return context.plan["target_version"] if context.journal_snapshot.record["state"] in {"target_installed", "launching_target"} else context.plan["old_version"]


def _validate_package(context):
    from importlib import import_module, metadata
    from arxiv_digest import __version__
    from arxiv_digest.update_manifest import runtime_requirements_sha256, parse_update_manifest
    from arxiv_digest.update_runtime import recovery
    wanted = _expected_version(context)
    if __version__ != wanted or Path(sys.executable).absolute() != Path(context.plan["paths"]["environment"]) / "bin/python":
        raise protocol.ProtocolError("internal installed version or interpreter differs")
    if Path(sys.executable).resolve(strict=True) != Path(context.plan["paths"]["base_interpreter"]):
        raise protocol.ProtocolError("internal base interpreter differs")
    distribution = metadata.distribution("arxiv-digest")
    if distribution.version != wanted or [(point.name, point.value) for point in distribution.entry_points if point.group == "console_scripts"] != [("arxiv-digest", "arxiv_digest.cli:main")]:
        raise protocol.ProtocolError("internal entry point differs")
    if wanted == context.plan["target_version"]:
        core = recovery.validate_target_installation(context.plan)
        wheel = context.plan["target_wheel"]
        raw, _ = recovery.read_owned_bytes(Path(wheel["path"]).parent / "UPDATE_MANIFEST.json")
        if hashlib.sha256(raw).hexdigest() != wheel["manifest_sha256"]:
            raise protocol.ProtocolError("internal target manifest differs")
        manifest = parse_update_manifest(raw)
        if manifest.runtime_requirements_sha256 != runtime_requirements_sha256(distribution.requires or ()):
            raise protocol.ProtocolError("internal runtime requirements differ")
    else:
        core = recovery.validate_old_installation(context.plan)
    entry = Path(context.plan["paths"]["environment"]) / "bin/arxiv-digest"
    if entry.resolve(strict=True) != entry or not entry.is_file() or entry.stat().st_mode & 0o022:
        raise protocol.ProtocolError("internal console script is unsafe")
    for module in ("arxiv_digest.application", "arxiv_digest.web.server", "arxiv_digest.storage.database"):
        import_module(module)
    return wanted, core


def run_self_check(context):
    from arxiv_digest.backup import inspect_portable_backup_source
    from arxiv_digest.maintenance import MaintenanceBarrier
    version, _ = _validate_package(context)
    inspection = inspect_portable_backup_source(_paths(context), maintenance=MaintenanceBarrier(),
                                               timeout=protocol.SELF_CHECK_TIMEOUT_SECONDS)
    if inspection.application_generation != 2:
        raise protocol.ProtocolError("internal data generation differs")
    context.channel.send(context.message("SELF_CHECK", version=version, updater_protocol=1,
        application_data_generation=2, ok=True), time.monotonic() + protocol.SELF_CHECK_TIMEOUT_SECONDS)
    return 0


def run_data_recovery(context):
    from arxiv_digest import __version__
    if __version__ != context.plan["old_version"] or set(context.locks) != {"transition", "launcher", "instance"}:
        raise protocol.ProtocolError("data recovery lacks old-version lock ownership")
    from arxiv_digest.update_artifacts import register_update_artifact
    from arxiv_digest.update_data_recovery import recover_update_backup_under_locks
    directory = recover_update_backup_under_locks(_paths(context), context.plan)
    register_update_artifact(Path(context.plan["paths"]["recovery_root"]), "raw", context.plan["attempt_id"],
                             __version__, directory / "raw-recovery.json")
    context.channel.send(context.message("DATA_RECOVERED", version=__version__, ok=True),
                         time.monotonic() + protocol.DATA_RECOVERY_TIMEOUT_SECONDS)
    return 0


def _provisional_provenance(context, core):
    from importlib import metadata
    from arxiv_digest.update_manifest import runtime_requirements_sha256
    from arxiv_digest.update_runtime import recovery
    store = protocol.ProtectedProvenanceStore(context.plan["paths"]["recovery_root"])
    current = store.read_snapshot()
    old = context.plan["old_token"]["provenance"]
    if old is None:
        if current is not None:
            raise protocol.StoreError("unexpected live provenance before healthy launch")
    elif current is None or current.canonical_bytes != protocol.encode_provenance(context.plan["prior_provenance"]):
        raise protocol.StoreError("prior provenance changed before healthy launch")
    outcome = context.boot["outcome"]
    if outcome == "restored":
        # The consumed snapshot changes only its root inode. Retaining the exact
        # prior record keeps replay's two authorized shapes valid across a crash
        # before the healthy proposal is durably published.
        return current
    version = context.plan["target_version"] if outcome == "updated" else context.plan["old_version"]
    wheel = context.plan["target_wheel"] if outcome == "updated" else context.plan["prior_provenance"]["wheel"]
    distribution = metadata.distribution("arxiv-digest")
    direct_path = Path(distribution._path) / "direct_url.json"
    direct, _ = recovery.read_owned_bytes(direct_path)
    record = {"schema_version": 1, "product": "arxiv-digest", "updater_protocol": 1, "application_data_generation": 2,
        "version": version, "attempt_id": context.plan["attempt_id"], "launch_id": context.boot["launch_id"],
        "wheel": wheel, "core_token": core, "direct_url_sha256": hashlib.sha256(direct).hexdigest(),
        "runtime_requirements_sha256": runtime_requirements_sha256(distribution.requires or ())}
    return store.compare_and_swap(current, record)


def _terminal_matches(context, proposal, provenance):
    store = protocol.JournalStore(context.plan["paths"]["recovery_root"])
    snapshot = store.read_snapshot()
    if snapshot is None or snapshot.record["state"] != "complete" or snapshot.record.get("receipt") != {**proposal, "unacknowledged": True}:
        raise protocol.StoreError("healthy launch has not been durably promoted")
    saved = protocol.ProtectedProvenanceStore(store.root).read_snapshot()
    if saved != provenance:
        raise protocol.StoreError("healthy launch provenance changed")
    return snapshot


def _prove_guards_released(context):
    from arxiv_digest.update_locks import acquire_exclusive
    root = Path(context.plan["paths"]["recovery_root"])
    transition = acquire_exclusive(root / "update-transition.lock", timeout=0)
    try:
        launcher = acquire_exclusive(root / "launcher-operation.lock", timeout=0)
        launcher.release()
    finally:
        transition.release()


def run_relaunch(context):
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.backup import recover_restore
    from arxiv_digest.browser import open_browser
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.profile import ProfileRepository
    from arxiv_digest.web.lifecycle import LifecycleController, SingleInstance
    from arxiv_digest.update_runtime import recovery
    paths = _paths(context)
    _, core = _validate_package(context)
    maintenance, lifecycle = MaintenanceBarrier(), LifecycleController()
    profiles = ProfileRepository(paths.profile_path, paths.profile_lock_path, maintenance=maintenance)
    runtime = _DefaultRuntime(paths, profiles, maintenance, lifecycle, output=lambda message: None,
                             update_running_command=Path(context.plan["paths"]["exposed_command"]))
    borrowed = context.locks.pop("instance")
    descriptor = borrowed.fileno()
    # from_borrowed adopts the very same descriptor; do not close the previous
    # Python handle because borrowed references release by close-only semantics.
    instance = SingleInstance.from_borrowed(paths.process_lock_path, paths.runtime_descriptor_path,
                                           fd=descriptor, identity=borrowed.identity)
    borrowed._descriptor = None
    server = database = None
    latch = None
    sole = False
    try:
        recover_restore(paths, maintenance=maintenance)
        database = runtime.open_database()
        latch = maintenance.update_latch()
        latch.__enter__()
        server = runtime.server(runtime.handlers())
        server.start()
        instance.publish_quarantined(port=server.port, startup_nonce=server.startup_nonce, token=server.token)
        context.channel.send(context.message("HEALTH", launch_id=context.boot["launch_id"], pid=os.getpid(),
            port=server.port, startup_nonce=server.startup_nonce, token=server.token),
            time.monotonic() + protocol.HEALTH_HANDSHAKE_TIMEOUT_SECONDS)
        opened = context.channel.receive(time.monotonic() + protocol.HEALTH_HANDSHAKE_TIMEOUT_SECONDS)
        context.authenticate_message(opened, "OPEN")
        if opened["outcome"] != context.boot["outcome"] or opened["ownership"] is not True:
            raise protocol.ProtocolError("healthy ownership decision differs")
        instance.adopt_sole_ownership()
        sole = True
        # Capture after target startup as well; no bytecode cache participates in
        # the immutable installation token and the data migration is journaled.
        core = recovery.validate_target_installation(context.plan) if context.boot["outcome"] == "updated" else recovery.validate_old_installation(context.plan)
        provenance = _provisional_provenance(context, core)
        proposal = {"receipt_id": secrets_token(), "outcome": context.boot["outcome"],
            "installed_version": _expected_version(context), "attempted_version": context.plan["target_version"],
            "message_code": protocol.OUTCOME_MESSAGES[context.boot["outcome"]],
            "attempt_id": context.plan["attempt_id"], "launch_id": context.boot["launch_id"]}
        store = protocol.JournalStore(paths.update_recovery_dir)
        current = store.validate_snapshot(context.journal_snapshot)
        record = {key: current.record[key] for key in protocol.COMMON | {"attempt_id", "plan_sha256"}}
        pending = store.transition(current, {**record, "state": "healthy_pending_commit", "proposal": proposal})
        context.channel.send(context.message("HEALTHY_READY", launch_id=context.boot["launch_id"], journal_sha256=pending.sha256,
            provenance_sha256=None if provenance is None else provenance.sha256), time.monotonic() + protocol.HEALTH_HANDSHAKE_TIMEOUT_SECONDS)
        try:
            unlocked = context.channel.receive(time.monotonic() + protocol.HEALTH_HANDSHAKE_TIMEOUT_SECONDS)
            context.authenticate_message(unlocked, "UNLOCKED")
            terminal = _terminal_matches(context, proposal, provenance)
            if unlocked["journal_sha256"] != terminal.sha256:
                raise protocol.StoreError("unlocked decision differs from terminal journal")
        except (EOFError, TimeoutError, OSError):
            # The only lost-UNLOCKED fallback proves the same terminal outcome
            # and fresh independent acquisition of both released outer guards.
            _terminal_matches(context, proposal, provenance)
            _prove_guards_released(context)
        latch.__exit__(None, None, None)
        latch = None
        runtime.start_sync()
        open_browser(server.launch_url("review"))
        server.wait()
        return 0
    finally:
        try:
            if server is not None:
                server.stop()
        finally:
            try:
                if database is not None:
                    database.close()
                runtime.update_coordinator.application_stopping()
            finally:
                if latch is not None:
                    latch.__exit__(None, None, None)
                if sole:
                    instance.release()
                else:
                    instance.close_borrowed()



def run_postterminal_relaunch(context):
    """One ordinary startup attempt with exact saved paths and no new receipt."""
    from arxiv_digest import __version__
    from arxiv_digest.application import create_application
    from arxiv_digest.cli import main
    if __version__ not in {context.plan["old_version"], context.plan["target_version"]}:
        raise protocol.ProtocolError("postterminal version differs")
    paths = _paths(context)
    announced = False
    def started():
        nonlocal announced
        if announced:
            return
        current = protocol.JournalStore(paths.update_recovery_dir).read_snapshot()
        if current is None or current.record["state"] != "complete" or current.record["attempt_id"] != context.plan["attempt_id"]:
            raise protocol.StoreError("terminal state changed before fallback startup")
        context.channel.send(context.message("SELF_CHECK", version=__version__, updater_protocol=1,
            application_data_generation=2, ok=True), time.monotonic() + protocol.SELF_CHECK_TIMEOUT_SECONDS)
        announced = True
    application = create_application(paths=paths, application_started=started,
                                     update_running_command=Path(context.plan["paths"]["exposed_command"]))
    return main([], paths_factory=lambda: paths, application_factory=lambda: application,
                internal_dispatch=lambda argv: None, output=lambda message: None)


def secrets_token():
    import secrets
    return secrets.token_hex(32)


def dispatch_internal(argv):
    if not argv or not argv[0].startswith(_PREFIX):
        return None
    try:
        context = _authenticate(argv)
    except (OSError, ValueError, TypeError, KeyError, EOFError):
        raise SystemExit(_REJECTION) from None
    try:
        if context.mode == "self-check":
            return run_self_check(context)
        if context.mode == "recover-data":
            return run_data_recovery(context)
        if context.mode == "postterminal-relaunch":
            return run_postterminal_relaunch(context)
        return run_relaunch(context)
    except (OSError, ValueError, RuntimeError, KeyError, EOFError):
        return 2
    finally:
        context.close()
