"""Process-owned preparation and the browser's serialized update handoff."""
from __future__ import annotations

import hashlib
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from arxiv_digest import __version__
from arxiv_digest.update_contract import ShutdownIntent, UpdateJobError, canonical_version
from arxiv_digest.update_runtime import protocol


COMMIT_MESSAGE = "Updating arXiv Digest. A new dashboard will open automatically. You can close this tab."
ERROR_MESSAGES = {
    "eligibility_changed": "This installation is no longer eligible for automatic updating.",
    "download_failed": "The update could not be downloaded. Please try again.",
    "verification_failed": "The update could not be verified. Please try again.",
    "snapshot_failed": "The current installation could not be backed up safely.",
    "work_did_not_stop": "Background work did not stop in time. Please try again.",
    "backup_failed": "The update recovery backup could not be verified.",
    "recovery_preparation_failed": "Recovery could not be prepared safely. Please try again.",
    "helper_failed": "The update helper could not start safely. Please try again.",
    "handoff_not_acknowledged": "The restart was not acknowledged. Please try again.",
    "helper_cancel_failed": "Fully quit arXiv Digest before reopening it to recover the update.",
    "helper_commit_aborted": "The restart was safely canceled. Please try again.",
    "helper_commit_failed": "Fully quit arXiv Digest before reopening it to recover the update.",
    "external_change_detected": "Fully quit arXiv Digest and review changes to its installation before reopening it.",
    "application_closing": "The application is closing.",
    "pending_update_receipt": "Acknowledge the previous update result before starting another update.",
}


class UpdateRequestError(ValueError):
    http_status = 409
    def __init__(self, code):
        if code not in ERROR_MESSAGES:
            raise ValueError("unsupported public update error")
        self.code = code
        super().__init__(ERROR_MESSAGES[code])


class _PreparationFailure(Exception):
    def __init__(self, code):
        self.code = code


def _record(snapshot, state, **fields):
    result = {key: snapshot.record[key] for key in protocol.COMMON | {"attempt_id", "plan_sha256"}}
    return {**result, "state": state, **fields}


class HelperConnection:
    """The coordinator thread owns all reads/writes on this helper socket."""
    def __init__(self, plan_snapshot, transition, launcher):
        plan = plan_snapshot.record
        root = Path(plan["paths"]["recovery_root"])
        self.nonce = secrets.token_hex(32)
        self.attempt_id = plan["attempt_id"]
        parent, child = socket.socketpair()
        try:
            self.process = subprocess.Popen(
                (plan["paths"]["base_interpreter"], "-I", "-B", str(root / "runtime/recovery.py"),
                 "--helper", "--recovery-root", str(root), "--attempt-id", self.attempt_id,
                 "--nonce", self.nonce, "--control-fd", str(child.fileno()),
                 "--transition-fd", str(transition.fileno()), "--launcher-fd", str(launcher.fileno())),
                env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
                cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(child.fileno(), transition.fileno(), launcher.fileno()),
                close_fds=True, start_new_session=True,
            )
            self.pid = self.process.pid
            self.channel = protocol.ControlChannel(parent)
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()

    def _receive(self, deadline):
        value = self.channel.receive(deadline)
        if value["attempt_id"] != self.attempt_id or value["nonce"] != self.nonce:
            raise protocol.ProtocolError("helper channel authentication differs")
        return value["kind"]

    def ready(self, deadline):
        if self._receive(deadline) != "READY" or self.process.poll() is not None:
            raise protocol.ProtocolError("helper did not become ready")

    def command(self, kind, deadline):
        self.channel.send({"schema_version": 1, "attempt_id": self.attempt_id, "nonce": self.nonce, "kind": kind}, deadline)
        return self._receive(deadline)

    def terminate_and_prove_dead(self):
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=protocol.TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=protocol.TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                return False
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return True
        return False

    def close(self):
        self.channel.close()


@dataclass
class PreparedAttempt:
    plan_snapshot: protocol.PlanSnapshot
    store: protocol.JournalStore
    transition: object
    launcher: object
    helper: HelperConnection | None = None

    def abort(self):
        from arxiv_digest.update_runtime.recovery import capture_launcher_state, replace_launcher_state
        current = self.store.read_snapshot()
        if current is None or current.record["state"] not in {"prepared", "canceling_no_install"}:
            raise protocol.StoreError("update is not proven disarmed")
        if current.record["state"] == "prepared":
            current = self.store.transition(current, _record(current, "canceling_no_install"))
        plan = self.plan_snapshot.record
        prior, intended = plan["launcher"]["prior"], plan["launcher"]["intended"]
        actual = capture_launcher_state(prior["path"])
        if actual != prior:
            if actual != intended:
                raise protocol.StoreError("launcher changed after preparation")
            replace_launcher_state(intended, prior, attempt_id=plan["attempt_id"],
                transition_lock=self.transition, launcher_lock=self.launcher)
        self.store.transition(current, _record(current, "aborted_no_mutation"))


class UpdateCoordinator:
    def __init__(self, *, runtime, paths, checker, prepare=None,
                 ready_timeout=protocol.READY_DEADLINE_SECONDS, admission=None,
                 monotonic=time.monotonic):
        if type(ready_timeout) not in {int, float} or not 0 < ready_timeout <= protocol.READY_DEADLINE_SECONDS:
            raise ValueError("invalid update ready deadline")
        self.runtime, self.paths, self.checker = runtime, paths, checker
        self.lifecycle = runtime.lifecycle
        self._prepare = self._prepare_production if prepare is None else prepare
        self._admission = self._check_admission if admission is None else admission
        self._clock = monotonic
        self._ready_timeout = ready_timeout
        self._condition = threading.Condition(threading.RLock())
        self._job = None
        self._descriptor = None
        self._thread = None
        self._decision = None
        self._deadline = None
        self._commit_presented = False
        self._committed = False
        self._guarded = False
        self._stopping = threading.Event()

    def _check_admission(self):
        snapshot = protocol.JournalStore(self.paths.update_recovery_dir).read_snapshot()
        if snapshot is None:
            return
        if "receipt" in snapshot.record:
            raise UpdateRequestError("pending_update_receipt")
        if snapshot.record["state"] not in {"complete", "aborted_no_mutation"}:
            raise UpdateRequestError("eligibility_changed")

    def start(self, target_version):
        canonical_version(target_version)
        with self._condition:
            if self.lifecycle.is_closing:
                raise UpdateRequestError("application_closing")
            if self._job is not None and (not self._job["complete"] or self._guarded):
                if self._descriptor_version != target_version:
                    raise UpdateRequestError("eligibility_changed")
                return {"job_id": self._job["job_id"], "state": "running", "phase": "downloading"}
            descriptor = self.checker.descriptor_for(target_version)
            if descriptor is None:
                raise UpdateRequestError("eligibility_changed")
            self._admission()
            identifier = secrets.token_hex(32)
            self._job = {"job_id": identifier, "state": "running", "phase": "downloading", "complete": False}
            self._descriptor = descriptor
            self._descriptor_version = target_version
            self._decision = None
            self._deadline = None
            self._commit_presented = self._committed = self._guarded = False
            self._thread = threading.Thread(target=self._run, args=(identifier, descriptor),
                                            name="arxiv-digest-update-coordinator", daemon=True)
            self._thread.start()
            return {"job_id": identifier, "state": "running", "phase": "downloading"}

    def _current(self, job_id):
        if self._job is None or job_id != self._job["job_id"]:
            raise KeyError("unknown update job")
        return self._job

    def job(self, job_id):
        with self._condition:
            return dict(self._current(job_id))

    def status(self):
        with self._condition:
            if self._job is None or self._job["complete"] and not self._guarded:
                return None
            return {"status": "preparing", "automatic_update": False,
                    "job_id": self._job["job_id"], "phase": self._job["phase"]}

    def commit(self, job_id):
        with self._condition:
            job = self._current(job_id)
            if self._decision is not None or self._deadline is None or self._clock() >= self._deadline:
                raise UpdateRequestError("handoff_not_acknowledged")
            if job["state"] not in {"ready_to_restart", "restarting"}:
                raise UpdateRequestError("eligibility_changed")
            self._commit_presented = True
            job.update(state="restarting", phase="restarting")
            return {"job_id": job_id, "state": "restarting", "phase": "restarting", "message": COMMIT_MESSAGE}

    def handoff_ack(self, job_id):
        with self._condition:
            job = self._current(job_id)
            if self._committed:
                return {"job_id": job_id, "state": "restarting", "phase": "restarting"}
            if (not self._commit_presented or self._decision not in {None, "COMMIT"}
                    or self._deadline is None or self._clock() >= self._deadline and self._decision != "COMMIT"
                    or job["complete"]):
                raise UpdateRequestError(job.get("error_code", "handoff_not_acknowledged"))
            self._decision = "COMMIT"
            self._condition.notify_all()
            until = time.monotonic() + protocol.READY_DEADLINE_SECONDS
            while not self._committed and not job["complete"]:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise UpdateRequestError("helper_commit_failed")
                self._condition.wait(min(remaining, 0.1))
            if not self._committed:
                raise UpdateRequestError(job.get("error_code", "helper_commit_failed"))
            return {"job_id": job_id, "state": "restarting", "phase": "restarting"}

    def receipt(self):
        return {"receipt": protocol.JournalStore(self.paths.update_recovery_dir).receipt(__version__)}

    def acknowledge_receipt(self, receipt_id):
        return {"acknowledged": protocol.JournalStore(self.paths.update_recovery_dir).acknowledge(receipt_id)}

    def application_stopping(self):
        self._stopping.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=protocol.LOCK_WAIT_TIMEOUT_SECONDS)
            if thread.is_alive():
                raise RuntimeError("update coordinator did not release after resource shutdown")

    def _phase(self, phase):
        with self._condition:
            self._job["phase"] = phase
            self._condition.notify_all()

    def _fail(self, code, *, canceled=False, guarded=False):
        with self._condition:
            self._guarded = guarded
            self._job.update(state="canceled" if canceled else "failed", complete=True,
                             error_code=code, message=ERROR_MESSAGES[code])
            self._condition.notify_all()

    def _hold_guard(self, identifier):
        self.lifecycle.allow_update_failure_quit(identifier)
        while not self._stopping.wait(0.1):
            pass

    def _run(self, identifier, descriptor):
        resume = False
        with self.lifecycle.update_owner(identifier):
            try:
                with self._prepare(identifier, descriptor, self._phase) as prepared:
                    helper = prepared.helper
                    try:
                        helper.ready(time.monotonic() + self._ready_timeout)
                        with self._condition:
                            self._deadline = self._clock() + self._ready_timeout
                            self._job.update(state="ready_to_restart", phase="ready_to_restart")
                            self._condition.notify_all()
                            while self._decision is None:
                                remaining = self._deadline - self._clock()
                                if remaining <= 0 or self._stopping.is_set():
                                    self._decision = "CANCEL"
                                    break
                                self._condition.wait(min(remaining, 0.1))
                            decision = self._decision
                        if decision == "CANCEL":
                            try:
                                answer = helper.command("CANCEL", time.monotonic() + protocol.READY_DEADLINE_SECONDS)
                                current = prepared.store.read_snapshot()
                                proven = answer == "CANCELED" and current is not None and current.record["state"] == "canceling_no_install"
                            except Exception:
                                proven = False
                            if not proven:
                                proven = helper.terminate_and_prove_dead()
                                current = prepared.store.read_snapshot()
                                proven = proven and current is not None and current.record["state"] in {"prepared", "canceling_no_install"}
                            if not proven:
                                self._fail("helper_cancel_failed", guarded=True)
                                self._hold_guard(identifier)
                            else:
                                prepared.abort()
                                resume = True
                                self._fail("handoff_not_acknowledged", canceled=True)
                        else:
                            try:
                                answer = helper.command("COMMIT", time.monotonic() + protocol.READY_DEADLINE_SECONDS)
                                current = prepared.store.read_snapshot()
                                if answer != "COMMITTED" or current is None or current.record["state"] != "committed":
                                    raise protocol.StoreError("helper did not durably commit")
                            except Exception:
                                # COMMIT won. Never send the opposite command;
                                # only proven precommit death permits cleanup.
                                dead = helper.terminate_and_prove_dead()
                                current = prepared.store.read_snapshot()
                                if dead and current is not None and current.record["state"] in {"prepared", "canceling_no_install"}:
                                    prepared.abort()
                                    resume = True
                                    self._fail("helper_commit_aborted")
                                else:
                                    self._fail("helper_commit_failed", guarded=True)
                                    self._hold_guard(identifier)
                            else:
                                with self._condition:
                                    self._committed = True
                                    self.lifecycle.request_shutdown(ShutdownIntent.UPDATE_RESTART)
                                    self._condition.notify_all()
                                while not self._stopping.wait(0.1):
                                    pass
                    except Exception:
                        if helper is not None and helper.terminate_and_prove_dead():
                            try:
                                prepared.abort()
                                resume = True
                                self._fail("helper_failed")
                            except Exception:
                                self._fail("external_change_detected", guarded=True)
                                self._hold_guard(identifier)
                        else:
                            self._fail("helper_cancel_failed", guarded=True)
                            self._hold_guard(identifier)
                    finally:
                        if helper is not None:
                            helper.close()
            except _PreparationFailure as error:
                resume = not self._guarded
                self._fail(error.code)
            except Exception:
                resume = not self._guarded
                if not self._guarded:
                    self._fail("recovery_preparation_failed")
        if resume:
            self.runtime.resume_after_update_failure()

    @contextmanager
    def _prepare_production(self, identifier, descriptor, phase):
        from arxiv_digest.atomic import ensure_private_directory_strict
        from arxiv_digest.backup import export_update_backup_under_lease
        from arxiv_digest.update_artifacts import register_update_artifact
        from arxiv_digest.update_download import download_target_wheel
        from arxiv_digest.update_locks import acquire_exclusive, acquire_shared
        from arxiv_digest.update_runtime.recovery import (
            capture_core_installation_token, capture_installation_token,
            capture_launcher_state, create_snapshot, materialize_runtime,
            replace_launcher_state, verify_installation_token, _external_links,
        )
        root = self.paths.update_recovery_dir
        self.paths.ensure_update_coordination()
        installation = descriptor.installation
        from arxiv_digest.update_installation import revalidate_installation_identity
        try:
            revalidate_installation_identity(installation)
        except (OSError, ValueError, RuntimeError):
            raise _PreparationFailure("eligibility_changed") from None
        manager = self.runtime._launcher_manager()
        if manager is None:
            raise _PreparationFailure("eligibility_changed")
        prior_journal = protocol.JournalStore(root).read_snapshot()
        prior_provenance = protocol.ProtectedProvenanceStore(root).read_snapshot() if installation.source_kind == "updater_provenance" else None
        reference = None if prior_provenance is None else {"identity": prior_provenance.identity, "sha256": prior_provenance.sha256}
        stage = "snapshot_failed"
        prepared = None
        yielded = False
        with ExitStack() as leases:
            try:
                # Capture launcher ownership under the normal documented order.
                shared = acquire_shared(self.paths.update_transition_lock_path, timeout=protocol.LOCK_WAIT_TIMEOUT_SECONDS)
                try:
                    launcher_capture_lock = acquire_exclusive(self.paths.launcher_operation_lock_path, timeout=protocol.LOCK_WAIT_TIMEOUT_SECONDS)
                    try:
                        launcher_states = manager.update_states()
                    finally:
                        launcher_capture_lock.release()
                finally:
                    shared.release()
                # External interpreter/shared links come only from the validated
                # pipx layout and exact raw symlinks recorded in a prior token.
                from arxiv_digest.update_snapshot import installation_external_links
                external_links = installation_external_links(installation)
                token = capture_installation_token(installation.venv, installation.exposed_command,
                    installation.base_interpreter, allowed_external_symlinks=external_links, provenance=reference)
                ensure_private_directory_strict(root / "attempts")
                attempt_dir = root / "attempts" / identifier
                attempt_dir.mkdir(mode=0o700)
                stage = "download_failed"
                downloaded = download_target_wheel(descriptor, attempt_dir, cancelled=self._stopping.is_set)
                register_update_artifact(root, "wheel", identifier, descriptor.target.version, downloaded.path)
                phase("verifying")
                stage = "verification_failed"
                if downloaded.inspection.version != descriptor.target.version:
                    raise protocol.StoreError("downloaded version differs")
                phase("snapshotting_environment")
                stage = "snapshot_failed"
                ensure_private_directory_strict(installation.snapshot_root)
                snapshot_path = installation.snapshot_root / identifier
                copied = create_snapshot(installation.venv, snapshot_path, installation.exposed_command,
                    installation.base_interpreter, allowed_external_symlinks=external_links, provenance=reference)
                if copied != token:
                    raise protocol.StoreError("installation changed while preparing snapshot")
                phase("stopping_work")
                stage = "work_did_not_stop"
                leases.enter_context(self.runtime.begin_update_quiescence(timeout=protocol.LOCK_WAIT_TIMEOUT_SECONDS))
                phase("backing_up")
                stage = "backup_failed"
                backup = export_update_backup_under_lease(self.paths, identifier, source_version=installation.version)
                register_update_artifact(root, "backup", identifier, installation.version, backup.path)
                phase("preparing_recovery")
                stage = "recovery_preparation_failed"
                transition = acquire_exclusive(self.paths.update_transition_lock_path, timeout=protocol.LOCK_WAIT_TIMEOUT_SECONDS,
                                               cancelled=self._stopping.is_set).transfer_close_only()
                leases.callback(transition.close)
                launcher = acquire_exclusive(self.paths.launcher_operation_lock_path, timeout=protocol.LOCK_WAIT_TIMEOUT_SECONDS,
                                             cancelled=self._stopping.is_set).transfer_close_only()
                leases.callback(launcher.close)
                if capture_launcher_state(manager.target) != launcher_states["prior"]:
                    raise protocol.StoreError("launcher changed before preparation")
                verify_installation_token(token, provenance=reference)
                if installation.source_kind == "updater_provenance" and protocol.ProtectedProvenanceStore(root).read_snapshot() != prior_provenance:
                    raise protocol.StoreError("provenance changed during preparation")
                sources = {name: files("arxiv_digest.update_runtime").joinpath(name).read_bytes() for name in protocol.RUNTIME_FILENAMES}
                store = protocol.JournalStore(root)
                if prior_journal is not None and prior_journal.record["state"] == "complete":
                    try:
                        from arxiv_digest.update_runtime.recovery import cleanup_terminal_snapshot
                        prior_plan = protocol.ProtectedPlanStore(root).read_snapshot()
                        cleanup_terminal_snapshot(plan_snapshot=prior_plan, journal_store=store,
                            journal_snapshot=prior_journal, transition_lock=transition, launcher_lock=launcher)
                    except (OSError, ValueError, RuntimeError):
                        # Immutable cleanup intents retain their authenticated
                        # inventories across a later plan replacement. Unknown
                        # remnants do not weaken the next attempt's admission.
                        pass
                materialized = materialize_runtime(root, installation.base_interpreter, sources,
                    **({"prior_journal_store": store, "prior_journal_snapshot": prior_journal}
                       if prior_journal is not None else {}))
                plan = self._make_plan(identifier, descriptor, token, prior_provenance, downloaded, backup,
                                       launcher_states, materialized, transition, launcher)
                published = protocol.ProtectedPlanStore(root).publish(plan)
                store.admit(published)
                prepared = PreparedAttempt(published, store, transition, launcher)
                if launcher_states["prior"] != launcher_states["intended"]:
                    replace_launcher_state(launcher_states["prior"], launcher_states["intended"], attempt_id=identifier,
                        transition_lock=transition, launcher_lock=launcher)
                stage = "helper_failed"
                prepared.helper = HelperConnection(published, transition, launcher)
                yielded = True
                yield prepared
            except Exception as error:
                if yielded:
                    raise
                if prepared is not None:
                    try:
                        if prepared.helper is not None and not prepared.helper.terminate_and_prove_dead():
                            raise protocol.StoreError("preparation helper teardown is uncertain")
                        prepared.abort()
                    except Exception:
                        self._fail("external_change_detected", guarded=True)
                        self._hold_guard(identifier)
                raise _PreparationFailure(stage) from error

    def _make_plan(self, identifier, descriptor, token, prior, downloaded, backup, launcher_states, materialized, transition, launcher):
        from dataclasses import asdict
        from arxiv_digest.update_runtime.recovery import capture_executable_identity
        installation = descriptor.installation
        root = self.paths.update_recovery_dir
        path_values = {
            "recovery_root": root, "environment": installation.venv,
            "snapshot": installation.snapshot_root / identifier, "forensic": installation.snapshot_root / (identifier + ".failed"),
            "pipx": installation.pipx_executable, "base_interpreter": installation.base_interpreter,
            "exposed_command": installation.exposed_command, "instance_lock": self.paths.process_lock_path,
            "diagnostic_log": self.paths.update_diagnostic_log_path,
            "config_dir": self.paths.config_dir, "data_dir": self.paths.data_dir, "cache_dir": self.paths.cache_dir,
            "user_home": installation.home, "pipx_home": installation.pipx_home,
            "pipx_shared_libs": installation.pipx_shared_libs,
            "pipx_bin_dir": installation.exposed_command.parent,
            "pipx_man_dir": installation.pipx_man_dir,
            "pipx_completion_dir": installation.pipx_completion_dir,
        }
        wheel = descriptor.target.wheel_asset
        backup_value = {"path": str(backup.path), "size": backup.identity["size"], "sha256": backup.inspection.archive_sha256,
                        "identity": backup.identity, "source_version": installation.version, "data_generation": 2,
                        "pdf_destination": {"kind": backup.pdf_destination.kind, "path": str(backup.pdf_destination.path)}}
        ordinary = protocol.file_identity(self.paths.process_lock_path.lstat())
        ordinary = {key: ordinary[key] for key in protocol.IDENTITY_KEYS}
        return {"schema_version": 1, "product": "arxiv-digest", "updater_protocol": 1, "application_data_generation": 2,
            "attempt_id": identifier, "old_version": installation.version, "target_version": descriptor.target.version,
            "parent_pid": os.getpid(), "paths": {key: str(value) for key, value in path_values.items()},
            "old_token": token, "prior_provenance": None if prior is None else prior.record,
            "pipx_identity": capture_executable_identity(installation.pipx_executable),
            "target_wheel": {"path": str(downloaded.path), "size": wheel.size, "sha256": wheel.sha256,
                "identity": downloaded.identity, "version": descriptor.target.version,
                "manifest_sha256": descriptor.target.manifest_asset.sha256, "source_url": wheel.url},
            "backup": backup_value, "runtime": materialized["runtime"], "wrapper": materialized["wrapper"],
            "launcher": launcher_states,
            "lock_identities": {"transition": asdict(transition.identity), "launcher": asdict(launcher.identity), "instance": ordinary}}
