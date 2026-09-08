"""Copied standard-library installer, rollback and healthy-relaunch coordinator."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import socket
import stat
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

if __package__:
    from . import guard, protocol, recovery
else:
    import guard
    import protocol
    import recovery


class HelperError(RuntimeError):
    pass


class ProcessDeathUnproven(HelperError):
    def __init__(self, groups):
        self.groups = tuple(groups)
        super().__init__("relevant process group death is unproven")


def _retain_until_dead(error):
    # This detached owner deliberately retains borrowed update locks until all
    # descendants are gone. Releasing early would authorize unsafe recovery.
    while not all(guard.process_group_dead(group) for group in error.groups):
        time.sleep(1)


def _message(attempt, nonce, kind, **fields):
    return {"schema_version": 1, "attempt_id": attempt, "nonce": nonce, "kind": kind, **fields}


def _expect(message, attempt, nonce, kind):
    if message["attempt_id"] != attempt or message["nonce"] != nonce or message["kind"] != kind:
        raise HelperError("internal control identity differs")
    return message


def _record(snapshot, state, **fields):
    return {**{key: snapshot.record[key] for key in protocol.COMMON | {"attempt_id", "plan_sha256"}}, "state": state, **fields}


def _reference(snapshot):
    return None if snapshot is None else {"identity": snapshot.identity, "sha256": snapshot.sha256}


class LockReference:
    """Borrowed or locally acquired ownership, always released by close only."""
    mode = "exclusive"

    def __init__(self, descriptor):
        self.descriptor = descriptor

    def fileno(self):
        if self.descriptor is None:
            raise HelperError("ownership descriptor is closed")
        return self.descriptor

    def close(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


def _inherited(fd, identity):
    if type(fd) is not int or fd < 3 or recovery._identity(os.fstat(fd)) != identity:
        raise HelperError("inherited ownership descriptor differs")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise HelperError("unsafe inherited ownership descriptor")
    os.set_inheritable(fd, False)
    return LockReference(fd)


def _acquire(path, identity, *, timeout=protocol.LOCK_WAIT_TIMEOUT_SECONDS):
    descriptor = protocol.open_private_lock_file(Path(path))
    try:
        if recovery._identity(os.fstat(descriptor)) != identity:
            raise HelperError("ordinary ownership path differs")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return LockReference(descriptor)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("application ownership deadline expired")
                time.sleep(0.05)
    except BaseException:
        os.close(descriptor)
        raise


def _artifact(artifact):
    path = Path(artifact["path"])
    parent = recovery._open_directory(path.parent, private=True)
    try:
        info, digest = recovery._read_file_at(parent, path.name, device=None)
        if recovery._file_identity(info) != artifact["identity"] or info.st_size != artifact["size"] or digest != artifact["sha256"]:
            raise HelperError("protected recovery artifact differs")
    finally:
        os.close(parent)


def validate_prepared_inputs(plan):
    if recovery.capture_executable_identity(plan["paths"]["pipx"]) != plan["pipx_identity"]:
        raise HelperError("validated pipx executable changed")
    _artifact(plan["target_wheel"])
    _artifact(plan["backup"])
    prior = plan["prior_provenance"]
    if prior is not None:
        _artifact(prior["wheel"])
    provenance = protocol.ProtectedProvenanceStore(plan["paths"]["recovery_root"]).read_snapshot()
    recovery.verify_installation_token(plan["old_token"], provenance=_reference(provenance))
    old = plan["old_token"]["core"]
    if recovery.scan_environment(plan["paths"]["snapshot"], allowed_external_symlinks=recovery._external_links(old)) != old["inventory"]:
        raise HelperError("prepared environment snapshot differs")
    if recovery.capture_launcher_state(plan["launcher"]["intended"]["path"]) != plan["launcher"]["intended"]:
        raise HelperError("prepared launcher differs")


def _cancel(store, current):
    if current.record["state"] == "prepared":
        return store.transition(current, _record(current, "canceling_no_install"))
    if current.record["state"] != "canceling_no_install":
        raise HelperError("committed helper cannot cancel")
    return current


def _restore_launcher(plan, transition, launcher):
    states = plan["launcher"]
    recovery.replace_launcher_state(states["intended"], states["prior"], attempt_id=plan["attempt_id"],
                                    transition_lock=transition, launcher_lock=launcher)


def _abort_no_install(store, current, plan, transition, launcher, reason):
    provenance = protocol.ProtectedProvenanceStore(plan["paths"]["recovery_root"]).read_snapshot()
    recovery.verify_installation_token(plan["old_token"], provenance=_reference(provenance))
    _restore_launcher(plan, transition, launcher)
    authorization = protocol.NoInstallAuthorization(current.sha256, plan["attempt_id"], True, True, True, reason)
    receipt = _receipt(plan, "handoff_failed", plan["old_version"])
    return store.transition(current, _record(current, "aborted_no_mutation", receipt=receipt), authorization=authorization)


def _receipt(plan, outcome, installed, *, launch_id=None):
    return {"receipt_id": secrets.token_hex(32), "outcome": outcome, "installed_version": installed,
            "attempted_version": plan["target_version"], "message_code": protocol.OUTCOME_MESSAGES[outcome],
            "attempt_id": plan["attempt_id"], "launch_id": launch_id, "unacknowledged": True}


def _wait_parent_exit(parent_pid, *, channel=None, attempt=None, nonce=None):
    deadline = time.monotonic() + protocol.PARENT_EXIT_TIMEOUT_SECONDS
    while os.getppid() == parent_pid:
        if time.monotonic() >= deadline:
            raise TimeoutError("parent exit deadline expired")
        if channel is None:
            time.sleep(0.05)
            continue
        try:
            command = channel.receive(min(deadline, time.monotonic() + 0.1))
            _expect(command, attempt, nonce, "COMMIT")
            channel.send(_message(attempt, nonce, "COMMITTED"), min(deadline, time.monotonic() + 1))
        except TimeoutError:
            pass
        except (OSError, ValueError, RuntimeError, EOFError):
            channel.close()
            channel = None  # A committed EOF/opposite command never cancels.


def _runtime_command(plan, mode, nonce, control_fd):
    return (plan["paths"]["base_interpreter"], "-I", "-B",
            str(Path(plan["paths"]["recovery_root"]) / "runtime/recovery.py"), mode,
            "--recovery-root", plan["paths"]["recovery_root"], "--attempt-id", plan["attempt_id"],
            "--nonce", nonce, "--control-fd", str(control_fd))


def run_installer(plan_snapshot, transition):
    plan = plan_snapshot.record
    nonce = secrets.token_hex(32)
    parent, child = socket.socketpair()
    process = subprocess.Popen(_runtime_command(plan, "--guard", nonce, child.fileno()),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               close_fds=True, pass_fds=(child.fileno(), transition.fileno()), start_new_session=True,
                               env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}, cwd=plan["paths"]["recovery_root"])
    child.close()
    channel = protocol.ControlChannel(parent)
    pgid = None
    try:
        channel.send(_message(plan["attempt_id"], nonce, "GUARD_BOOT", plan_sha256=plan_snapshot.sha256,
                     transition_fd=transition.fileno()), time.monotonic() + protocol.READY_DEADLINE_SECONDS)
        started = _expect(channel.receive(time.monotonic() + protocol.READY_DEADLINE_SECONDS), plan["attempt_id"], nonce, "GUARD_STARTED")
        pgid = started["pgid"]
        result = _expect(channel.receive(time.monotonic() + protocol.PIPX_COMMAND_TIMEOUT_SECONDS + 3 * protocol.TERM_GRACE_SECONDS), plan["attempt_id"], nonce, "GUARD_RESULT")
        if result["process_group_id"] != pgid or not guard.process_group_dead(pgid):
            raise HelperError("installer group death differs")
        process.wait(timeout=protocol.TERM_GRACE_SECONDS)
        return result
    except BaseException:
        try:
            channel.send(_message(plan["attempt_id"], nonce, "GUARD_CANCEL"), time.monotonic() + 1)
            process.wait(timeout=3 * protocol.TERM_GRACE_SECONDS)
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            guard.terminate_and_reap(process)
        # The durable installing record closes the guard-killed-before-STARTED
        # gap. A child was dormant until this record was fsynced and reread.
        current = protocol.JournalStore(plan["paths"]["recovery_root"]).read_snapshot()
        installer = current.record.get("installer", {})
        pgid = installer.get("process_group_id", pgid)
        if pgid is not None and not guard.process_group_dead(pgid):
            raise ProcessDeathUnproven([pgid])
        raise
    finally:
        channel.close()


def _internal_child(plan_snapshot, mode, locks, *, outcome=None):
    plan = plan_snapshot.record
    parent, child = socket.socketpair()
    nonce = secrets.token_hex(32)
    launch_id = secrets.token_hex(32) if mode == "relaunch" else None
    descriptors = {name: lock.fileno() for name, lock in locks.items()}
    command = (str(Path(plan["paths"]["environment"]) / "bin/python"), "-I", "-B", "-m", "arxiv_digest",
               "--arxiv-digest-internal-" + mode, str(child.fileno()), plan["paths"]["recovery_root"], plan["attempt_id"])
    paths = plan["paths"]
    environment = {"HOME": paths["user_home"], "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                   "PATH": os.pathsep.join(dict.fromkeys((paths["pipx_bin_dir"], str(Path(paths["pipx"]).parent),
                            str(Path(paths["base_interpreter"]).parent), "/usr/bin", "/bin"))),
                   "PIPX_HOME": paths["pipx_home"], "PIPX_BIN_DIR": paths["pipx_bin_dir"],
                   "PIPX_SHARED_LIBS": paths["pipx_shared_libs"], "PIPX_MAN_DIR": paths["pipx_man_dir"],
                   "PIPX_COMPLETION_DIR": paths["pipx_completion_dir"],
                   "PIPX_FETCH_PYTHON": "never", "PIPX_DEFAULT_BACKEND": "pip",
                   "PIP_NO_INDEX": "1", "PIP_NO_INPUT": "1", "PIP_CONFIG_FILE": os.devnull}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               close_fds=True, pass_fds=(child.fileno(), *descriptors.values()), start_new_session=True,
                               cwd=plan["paths"]["recovery_root"], env=environment)
    child.close()
    channel = protocol.ControlChannel(parent)
    try:
        channel.send(_message(plan["attempt_id"], nonce, "BOOT", mode=mode, plan_sha256=plan_snapshot.sha256,
                     launch_id=launch_id, outcome=outcome, lock_fds=descriptors), time.monotonic() + protocol.READY_DEADLINE_SECONDS)
    except BaseException:
        channel.close()
        if not guard.terminate_and_reap(process):
            raise ProcessDeathUnproven([process.pid])
        raise
    return process, channel, nonce, launch_id


def internal_operation(plan_snapshot, mode, locks, expected_version):
    process, channel, nonce, _ = _internal_child(plan_snapshot, mode, locks)
    try:
        timeout = protocol.SELF_CHECK_TIMEOUT_SECONDS if mode == "self-check" else protocol.DATA_RECOVERY_TIMEOUT_SECONDS
        response = _expect(channel.receive(time.monotonic() + timeout), plan_snapshot.record["attempt_id"], nonce,
                           "SELF_CHECK" if mode == "self-check" else "DATA_RECOVERED")
        if response["version"] != expected_version:
            raise HelperError("internal operation version differs")
        if process.wait(timeout=protocol.TERM_GRACE_SECONDS) != 0 or not guard.process_group_dead(process.pid):
            raise HelperError("internal operation failed or retained descendants")
    finally:
        channel.close()
        if process.poll() is None or not guard.process_group_dead(process.pid):
            if not guard.terminate_and_reap(process):
                raise ProcessDeathUnproven([process.pid])


def _http_health(health):
    port = health["port"]
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/status", headers={
        "Host": f"127.0.0.1:{port}", "Authorization": "Bearer " + health["token"],
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        if response.status != 200:
            raise HelperError("relaunch health HTTP status differs")
        payload = response.read(protocol.CONTROL_BYTE_LIMIT + 1)
        if len(payload) > protocol.CONTROL_BYTE_LIMIT:
            raise HelperError("relaunch health response too large")
    value = json.loads(payload)
    if value.get("api_version") != "v1" or value.get("ok") is not True or value.get("data", {}).get("startup_nonce") != health["startup_nonce"]:
        raise HelperError("relaunch health identity differs")


def _validate_healthy_provenance(plan, outcome, launch_id, saved):
    core = recovery.validate_target_installation(plan) if outcome == "updated" else recovery.validate_old_installation(plan)
    prior = plan["prior_provenance"]
    if outcome == "restored" and prior is None:
        if saved is not None:
            raise HelperError("restored canonical installation has unexpected provenance")
        return
    wheel = plan["target_wheel"] if outcome == "updated" else prior["wheel"]
    if saved is None:
        raise HelperError("healthy updater installation lacks provenance")
    record = saved.record
    expected_version = plan["target_version"] if outcome == "updated" else plan["old_version"]
    if outcome == "restored":
        if record != prior:
            raise HelperError("restored protected provenance differs from exact prior record")
    elif (record["version"] != expected_version or record["attempt_id"] != plan["attempt_id"]
            or record["launch_id"] != launch_id or record["core_token"] != core or record["wheel"] != wheel):
        raise HelperError("healthy protected provenance correspondence differs")
    _artifact(wheel)
    manifest, _ = recovery.read_owned_bytes(Path(wheel["path"]).parent / "UPDATE_MANIFEST.json")
    if hashlib.sha256(manifest).hexdigest() != wheel["manifest_sha256"]:
        raise HelperError("healthy retained manifest differs")
    manifest_value = json.loads(manifest)
    if manifest_value["runtime_requirements_sha256"] != record["runtime_requirements_sha256"]:
        raise HelperError("healthy dependency provenance differs")
    ending = f"arxiv_digest-{expected_version}.dist-info/direct_url.json"
    matches = [item for item in core["inventory"]["entries"] if item["kind"] == "file" and item["path"].endswith("/" + ending)]
    if len(matches) != 1:
        raise HelperError("healthy direct wheel source is ambiguous")
    direct, _ = recovery.read_owned_bytes(Path(plan["paths"]["environment"]) / matches[0]["path"])
    expected = {"url": Path(wheel["path"]).as_uri(), "archive_info": {
        "hash": "sha256=" + wheel["sha256"], "hashes": {"sha256": wheel["sha256"]},
    }}
    if hashlib.sha256(direct).hexdigest() != record["direct_url_sha256"] or json.loads(direct) != expected:
        raise HelperError("healthy archive source provenance differs")


def remove_success_log(plan, reference):
    """Remove only the exact file whose bytes the successful guard verified."""
    protocol.validate_record_reference(reference)
    path = Path(plan["paths"]["diagnostic_log"])
    parent = recovery._open_directory(path.parent, private=True)
    try:
        try:
            payload, identity = recovery.read_owned_bytes(path, limit=protocol.PRIVATE_LOG_BYTE_LIMIT)
        except FileNotFoundError:
            return
        if identity != reference["identity"] or hashlib.sha256(payload).hexdigest() != reference["sha256"]:
            raise HelperError("successful diagnostic log changed")
        if recovery._file_identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != identity:
            raise HelperError("successful diagnostic log identity changed")
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def healthy_relaunch(plan_snapshot, store, transition, launcher, instance, *, outcome, diagnostic_log=None):
    plan = plan_snapshot.record
    process, channel, nonce, launch_id = _internal_child(plan_snapshot, "relaunch", {"instance": instance}, outcome=outcome)
    terminal = False
    try:
        deadline = time.monotonic() + protocol.HEALTH_HANDSHAKE_TIMEOUT_SECONDS
        health = _expect(channel.receive(deadline), plan["attempt_id"], nonce, "HEALTH")
        if health["launch_id"] != launch_id or health["pid"] != process.pid:
            raise HelperError("relaunch child identity differs")
        _http_health(health)
        instance.close()  # Child is now the sole ordinary-lock reference.
        channel.send(_message(plan["attempt_id"], nonce, "OPEN", launch_id=launch_id, outcome=outcome, ownership=True), deadline)
        ready = _expect(channel.receive(deadline), plan["attempt_id"], nonce, "HEALTHY_READY")
        proposal = store.read_snapshot()
        if (ready["launch_id"] != launch_id or proposal.sha256 != ready["journal_sha256"]
                or proposal.record["state"] != "healthy_pending_commit"
                or proposal.record["proposal"]["launch_id"] != launch_id
                or proposal.record["proposal"]["outcome"] != outcome):
            raise HelperError("healthy child proposal differs")
        provenance = protocol.ProtectedProvenanceStore(plan["paths"]["recovery_root"]).read_snapshot()
        if (None if provenance is None else provenance.sha256) != ready["provenance_sha256"]:
            raise HelperError("healthy provenance differs")
        if process.poll() is not None:
            raise HelperError("healthy relaunch child exited before promotion")
        _validate_healthy_provenance(plan, outcome, launch_id, provenance)
        _http_health(health)
        if process.poll() is not None:
            raise HelperError("healthy relaunch child exited before promotion")
        complete = store.transition(proposal, _record(proposal, "complete", receipt={**proposal.record["proposal"], "unacknowledged": True}))
        terminal = True
        try:
            protocol.ArtifactCatalogStore(plan["paths"]["recovery_root"]).prune_terminal()
        except (OSError, ValueError, RuntimeError):
            pass  # Retention maintenance cannot undo durable healthy success.
        try:
            recovery.cleanup_terminal_snapshot(plan_snapshot=plan_snapshot, journal_store=store,
                journal_snapshot=complete, transition_lock=transition, launcher_lock=launcher)
        except (OSError, ValueError, RuntimeError):
            pass  # Exact cleanup is retried later; complete remains authoritative.
        if outcome == "updated" and diagnostic_log is not None:
            try:
                remove_success_log(plan, diagnostic_log)
            except (OSError, ValueError, RuntimeError):
                pass  # Preserve completed success when exact cleanup refuses.
        transition.close()
        launcher.close()
        channel.send(_message(plan["attempt_id"], nonce, "UNLOCKED", launch_id=launch_id, journal_sha256=complete.sha256), deadline)
        return True
    except BaseException:
        if process.poll() is None or not guard.process_group_dead(process.pid):
            if not guard.terminate_and_reap(process):
                raise ProcessDeathUnproven([process.pid])
        if terminal:
            # Terminal success is durable. Do not restore a healthy promoted
            # installation when only the final socket/browser phase failed.
            transition.close()
            launcher.close()
            retry, retry_channel, retry_nonce, _ = _internal_child(plan_snapshot, "postterminal-relaunch", {})
            try:
                response = _expect(retry_channel.receive(time.monotonic() + protocol.HEALTH_HANDSHAKE_TIMEOUT_SECONDS),
                                   plan["attempt_id"], retry_nonce, "SELF_CHECK")
                expected_version = plan["target_version"] if outcome == "updated" else plan["old_version"]
                if response["version"] != expected_version:
                    raise HelperError("postterminal relaunch version differs")
            except (OSError, ValueError, RuntimeError, EOFError):
                guard.terminate_and_reap(retry)
            finally:
                retry_channel.close()
            return True
        raise
    finally:
        channel.close()


def restore_and_relaunch(plan_snapshot, store, transition, launcher, instance, *, explicit=False):
    plan = plan_snapshot.record
    current = store.read_snapshot()
    groups = []
    if current.record.get("installer") is not None:
        groups = [current.record["installer"]["process_group_id"]]
    if current.record["state"] == "rolling_back":
        groups = current.record["replay"]["process_group_ids"]
    if not all(guard.process_group_dead(group) for group in groups):
        raise ProcessDeathUnproven(groups)
    if instance.descriptor is None:
        instance = _acquire(plan["paths"]["instance_lock"], plan["lock_identities"]["instance"])
    prior = plan["prior_provenance"]
    _artifact(plan["target_wheel"])
    _artifact(plan["backup"])
    if prior is not None:
        _artifact(prior["wheel"])
    provenance_store = protocol.ProtectedProvenanceStore(plan["paths"]["recovery_root"])
    if current.record["state"] != "rolling_back":
        core = plan["old_token"]["core"]
        replay = {"snapshot_path": plan["paths"]["snapshot"], "live_path": plan["paths"]["environment"],
                  "forensic_path": plan["paths"]["forensic"], "old_token": core,
                  "partial_token": recovery.capture_partial_environment(plan["paths"]["environment"], allowed_external_symlinks=recovery._external_links(core)),
                  "partial_exposed": recovery.capture_exposed_link(plan["paths"]["exposed_command"], allow_missing=True),
                  "partial_provenance": _reference(provenance_store.read_snapshot()), "prior_provenance": prior,
                  "target_started": current.record["state"] in {"launching_target", "healthy_pending_commit", *protocol.BLOCKING_STATES},
                  "process_group_ids": groups, "processes_dead": True}
        authorization = protocol.RecoveryAuthorization(current.sha256, plan["attempt_id"], replay, explicit=explicit)
        current = store.transition(current, _record(current, "rolling_back", subphase="package_restore_pending", replay=replay), authorization=authorization)
    recovery.replay_snapshot(journal_store=store, journal_snapshot=current,
                            process_death=lambda groups: all(guard.process_group_dead(group) for group in groups), provenance_store=provenance_store)
    _restore_launcher(plan, transition, launcher)
    if current.record["replay"]["target_started"]:
        internal_operation(plan_snapshot, "recover-data", {"transition": transition, "launcher": launcher, "instance": instance}, plan["old_version"])
    internal_operation(plan_snapshot, "self-check", {"transition": transition}, plan["old_version"])
    return healthy_relaunch(plan_snapshot, store, transition, launcher, instance, outcome="restored")


def _failure_terminal(store, plan):
    current = store.read_snapshot()
    if current is None or current.record["state"] in protocol.TERMINAL_STATES:
        return
    groups = current.record.get("replay", {}).get("process_group_ids", [])
    if current.record.get("installer") is not None:
        groups = [current.record["installer"]["process_group_id"]]
    if not all(guard.process_group_dead(group) for group in groups):
        raise ProcessDeathUnproven(groups)
    receipt = None
    try:
        recovery.validate_old_installation(plan)
        receipt = _receipt(plan, "recovery_failed", plan["old_version"])
    except recovery.SnapshotError:
        pass
    authorization = protocol.FailureAuthorization(current.sha256, plan["attempt_id"], True, "package_restore")
    fields = {} if receipt is None else {"receipt": receipt}
    store.transition(current, _record(current, "recovery_failed", **fields), authorization=authorization)


def _recover_or_terminal(plan_snapshot, store, transition, launcher, instance, *, explicit=False):
    while True:
        try:
            return restore_and_relaunch(plan_snapshot, store, transition, launcher, instance, explicit=explicit)
        except ProcessDeathUnproven as error:
            _retain_until_dead(error)
        except (OSError, ValueError, RuntimeError, EOFError):
            try:
                _failure_terminal(store, plan_snapshot.record)
            except ProcessDeathUnproven as error:
                _retain_until_dead(error)
                continue
            return False


def run_helper(options, plan_snapshot):
    plan = plan_snapshot.record
    if os.getppid() != plan["parent_pid"]:
        raise HelperError("helper parent differs")
    if len({options.control_fd, options.transition_fd, options.launcher_fd}) != 3:
        raise HelperError("helper descriptor roles overlap")
    transition = _inherited(options.transition_fd, plan["lock_identities"]["transition"])
    launcher = _inherited(options.launcher_fd, plan["lock_identities"]["launcher"])
    channel = protocol.ControlChannel(socket.socket(fileno=options.control_fd))
    store = protocol.JournalStore(plan["paths"]["recovery_root"])
    instance = None
    try:
        current = store.read_snapshot()
        if current is None or current.record["state"] != "prepared" or current.record["attempt_id"] != options.attempt_id:
            raise HelperError("helper lacks prepared authorization")
        validate_prepared_inputs(plan)
        deadline = time.monotonic() + protocol.READY_DEADLINE_SECONDS
        channel.send(_message(options.attempt_id, options.nonce, "READY"), deadline)
        while True:
            try:
                if os.getppid() != plan["parent_pid"]:
                    raise EOFError("helper parent exited before commit")
                command = channel.receive(min(deadline, time.monotonic() + 0.2))
                break
            except TimeoutError:
                if time.monotonic() < deadline:
                    continue
                _cancel(store, current)
                return 0
            except (EOFError, OSError):
                _cancel(store, current)
                return 0
        if command["kind"] == "CANCEL":
            _expect(command, options.attempt_id, options.nonce, "CANCEL")
            _cancel(store, current)
            channel.send(_message(options.attempt_id, options.nonce, "CANCELED"), deadline)
            return 0
        _expect(command, options.attempt_id, options.nonce, "COMMIT")
        current = store.transition(current, _record(current, "committed"))
        channel.send(_message(options.attempt_id, options.nonce, "COMMITTED"), deadline)
        try:
            _wait_parent_exit(plan["parent_pid"], channel=channel, attempt=options.attempt_id, nonce=options.nonce)
        except TimeoutError:
            _abort_no_install(store, current, plan, transition, launcher, "parent_exit_timeout")
            return 0
        channel.close()
        try:
            instance = _acquire(plan["paths"]["instance_lock"], plan["lock_identities"]["instance"])
        except TimeoutError:
            _abort_no_install(store, current, plan, transition, launcher, "instance_lock_timeout")
            return 0
        try:
            validate_prepared_inputs(plan)
            guard.require_empty_safe_trash(Path(plan["paths"]["pipx_home"]) / ".trash")
        except (OSError, ValueError, RuntimeError):
            authorization = protocol.ExternalChangeAuthorization(current.sha256, plan["attempt_id"], "inventory")
            store.transition(current, _record(current, "external_change_detected"), authorization=authorization)
            return 1
        try:
            result = run_installer(plan_snapshot, transition)
            if result["returncode"] != 0 or result["timed_out"]:
                raise HelperError("target pipx installation failed")
            recovery.validate_target_installation(plan)
            current = store.read_snapshot()
            current = store.transition(current, _record(current, "target_installed"))
            internal_operation(plan_snapshot, "self-check", {"transition": transition}, plan["target_version"])
            current = store.transition(current, _record(current, "launching_target"))
            healthy_relaunch(plan_snapshot, store, transition, launcher, instance, outcome="updated", diagnostic_log=result["log"])
            return 0
        except ProcessDeathUnproven as error:
            _retain_until_dead(error)
            return 0 if _recover_or_terminal(plan_snapshot, store, transition, launcher, instance) else 1
        except (OSError, ValueError, RuntimeError, EOFError, TimeoutError):
            return 0 if _recover_or_terminal(plan_snapshot, store, transition, launcher, instance) else 1
    finally:
        channel.close()
        if instance is not None:
            instance.close()
        launcher.close()
        transition.close()


def run_recovery(options, plan_snapshot):
    plan = plan_snapshot.record
    store = protocol.JournalStore(plan["paths"]["recovery_root"])
    current = store.read_snapshot()
    if current is None or current.record["state"] == "aborted_no_mutation":
        return 0
    if current.record["state"] == "complete":
        transition = launcher = None
        try:
            # A healthy application may still own its ordinary lock. Terminal
            # maintenance needs only the outer guards and never waits for them.
            root = Path(plan["paths"]["recovery_root"])
            transition = _acquire(root / "update-transition.lock", plan["lock_identities"]["transition"], timeout=0)
            launcher = _acquire(root / "launcher-operation.lock", plan["lock_identities"]["launcher"], timeout=0)
            recovery.cleanup_terminal_snapshot(plan_snapshot=plan_snapshot, journal_store=store,
                journal_snapshot=current, transition_lock=transition, launcher_lock=launcher)
        except (OSError, ValueError, RuntimeError):
            pass  # A refusal must not block an already healthy normal launch.
        finally:
            if launcher is not None:
                launcher.close()
            if transition is not None:
                transition.close()
        return 0
    if current.record["state"] in protocol.BLOCKING_STATES and not options.explicit_recovery:
        raise HelperError("explicit authenticated recovery is required")
    transition = _acquire(Path(plan["paths"]["recovery_root"]) / "update-transition.lock", plan["lock_identities"]["transition"])
    instance = None
    launcher = None
    try:
        instance = _acquire(plan["paths"]["instance_lock"], plan["lock_identities"]["instance"])
        launcher = _acquire(Path(plan["paths"]["recovery_root"]) / "launcher-operation.lock", plan["lock_identities"]["launcher"])
        current = store.validate_snapshot(current)
        if current.record["state"] in protocol.BLOCKING_STATES:
            # Explicit recovery never authorizes an arbitrary external tree.
            # Admit only the complete known old or intended target installation.
            try:
                recovery.validate_old_installation(plan)
            except recovery.SnapshotError:
                recovery.validate_target_installation(plan)
        if current.record["state"] in {"prepared", "canceling_no_install"}:
            provenance = protocol.ProtectedProvenanceStore(plan["paths"]["recovery_root"]).read_snapshot()
            recovery.verify_installation_token(plan["old_token"], provenance=_reference(provenance))
            current = _cancel(store, current)
            _restore_launcher(plan, transition, launcher)
            store.transition(current, _record(current, "aborted_no_mutation"))
            return 0
        return 0 if _recover_or_terminal(plan_snapshot, store, transition, launcher, instance, explicit=options.explicit_recovery) else 1
    finally:
        if launcher is not None:
            launcher.close()
        if instance is not None:
            instance.close()
        transition.close()
