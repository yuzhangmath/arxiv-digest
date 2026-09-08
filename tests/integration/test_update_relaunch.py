from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from arxiv_digest.update_runtime import protocol
from arxiv_digest.update_locks import LockTimeoutError, acquire_exclusive
from tests.unit.test_backup import initialized_paths
from tests.update_protocol_factory import next_record, plan_record


def _environment(tmp_path):
    from arxiv_digest.update_internal import InternalContext
    paths = initialized_paths(tmp_path)
    paths.ensure_update_coordination()
    owned = {name: acquire_exclusive(path, timeout=0) for name, path in {
        "transition": paths.update_transition_lock_path, "launcher": paths.launcher_operation_lock_path,
        "instance": paths.process_lock_path}.items()}
    record = plan_record(paths.update_recovery_dir)
    record["paths"].update({name: str(getattr(paths, name)) for name in ("config_dir", "data_dir", "cache_dir")})
    record["paths"]["instance_lock"] = str(paths.process_lock_path)
    record["lock_identities"] = {name: asdict(lock.identity) for name, lock in owned.items()}
    plan = protocol.ProtectedPlanStore(paths.update_recovery_dir).publish(record)
    store = protocol.JournalStore(paths.update_recovery_dir)
    current = store.admit(plan)
    for state in ("committed", "installing", "target_installed", "launching_target"):
        target = next_record(current, state)
        auth = protocol.InstallerAuthorization(current.sha256, current.record["attempt_id"], target["installer"]) if state == "installing" else None
        current = store.transition(current, target, authorization=auth)
    first, second = socket.socketpair()
    boot = {"schema_version": 1, "attempt_id": record["attempt_id"], "nonce": "b" * 64, "kind": "BOOT",
        "mode": "relaunch", "plan_sha256": plan.sha256, "launch_id": "c" * 64, "outcome": "updated",
        "lock_fds": {"instance": os.dup(owned["instance"].fileno())}}
    from arxiv_digest.update_locks import adopt_borrowed, LockMode
    borrowed = adopt_borrowed(boot["lock_fds"]["instance"], expected_identity=owned["instance"].identity, mode=LockMode.EXCLUSIVE)
    context = InternalContext("relaunch", plan, current, boot, protocol.ControlChannel(second), {"instance": borrowed})
    return paths, record, store, owned, context, protocol.ControlChannel(first)


def _health(message):
    connection = http.client.HTTPConnection("127.0.0.1", message["port"], timeout=2)
    try:
        headers = {"Host": f"127.0.0.1:{message['port']}", "Authorization": f"Bearer {message['token']}"}
        connection.request("GET", "/api/v1/status", headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["data"]["startup_nonce"] == message["startup_nonce"]
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", message["port"], timeout=2)
        headers.update(Origin=f"http://127.0.0.1:{message['port']}", **{"Content-Type": "application/json"})
        connection.request("POST", "/api/v1/sync/start", body=b"{}", headers=headers)
        response = connection.getresponse()
        assert response.status == 409
        assert json.loads(response.read())["error"]["code"] == "update_in_progress"
    finally:
        connection.close()


@pytest.mark.parametrize("failure", [None, "before_open", "after_proposal", "after_complete", "after_complete_locked"])
def test_real_quarantine_health_ownership_and_terminal_receipt_order(tmp_path, monkeypatch, failure):
    import arxiv_digest.application as application
    import arxiv_digest.update_internal as internal
    from arxiv_digest.update_runtime import recovery
    from arxiv_digest.web.server import LoopbackServer
    paths, record, store, owned, context, helper = _environment(tmp_path)
    events = []
    monkeypatch.setattr(protocol, "HEALTH_HANDSHAKE_TIMEOUT_SECONDS", 3.0)
    activated = threading.Event()
    finish = threading.Event()
    monkeypatch.setattr(internal, "_validate_package", lambda context: ("0.3.1", record["old_token"]["core"]))
    monkeypatch.setattr(recovery, "validate_target_installation", lambda plan: record["old_token"]["core"])
    monkeypatch.setattr(internal, "_provisional_provenance", lambda context, core: None)
    monkeypatch.setattr("arxiv_digest.backup.recover_restore", lambda *a, **k: events.append("recover_data"))
    monkeypatch.setattr("arxiv_digest.browser.open_browser", lambda url: events.append("browser") or activated.set() or True)
    class Runtime:
        def __init__(self, paths, profiles, maintenance, lifecycle, **kwargs):
            self.maintenance, self.lifecycle = maintenance, lifecycle
            self.update_coordinator = SimpleNamespace(application_stopping=lambda: events.append("resources_closed"))
        def open_database(self):
            events.append("database")
            return SimpleNamespace(close=lambda: events.append("database_closed"))
        def handlers(self):
            return {"status": lambda payload: {}, "sync_start": lambda payload: pytest.fail("quarantined mutation admitted")}
        def server(self, handlers):
            server = LoopbackServer(handlers=handlers, maintenance=self.maintenance, lifecycle=self.lifecycle)
            server.wait = lambda: finish.wait(3)
            return server
        def start_sync(self):
            assert store.read_snapshot().record["state"] == "complete"
            assert self.maintenance.update_active is False
            events.append("sync")
    monkeypatch.setattr(application, "_DefaultRuntime", Runtime)
    def run():
        try:
            return internal.run_relaunch(context)
        except (ValueError, RuntimeError, OSError, EOFError) as error:
            events.append("failure:" + repr(error))
            return 2
        finally:
            context.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            child = executor.submit(run)
            try:
                health = helper.receive(time.monotonic() + 3)
            except EOFError:
                pytest.fail(str(events))
            assert health["kind"] == "HEALTH"
            _health(health)
            assert "browser" not in events and "sync" not in events
            if failure == "before_open":
                helper.close()
            else:
                owned.pop("instance").transfer_close_only().close()
                helper.send(context.message("OPEN", launch_id=context.boot["launch_id"], outcome="updated", ownership=True), time.monotonic() + 3)
                ready = helper.receive(time.monotonic() + 3)
                assert ready["kind"] == "HEALTHY_READY"
                proposal = store.read_snapshot()
                assert proposal.record["state"] == "healthy_pending_commit"
                assert store.receipt("0.3.1") is None
                assert "browser" not in events and "sync" not in events
                if failure == "after_proposal":
                    helper.close()
                else:
                    terminal = store.transition(proposal, next_record(proposal, "complete", receipt={**proposal.record["proposal"], "unacknowledged": True}))
                    assert store.receipt("0.3.1")["outcome"] == "updated"
                    if failure != "after_complete_locked":
                        owned.pop("launcher").transfer_close_only().close()
                        owned.pop("transition").transfer_close_only().close()
                    if failure in {"after_complete", "after_complete_locked"}:
                        helper.close()
                    else:
                        helper.send(context.message("UNLOCKED", launch_id=context.boot["launch_id"], journal_sha256=terminal.sha256), time.monotonic() + 3)
            if failure in {None, "after_complete"}:
                assert activated.wait(3)
                assert events.index("sync") < events.index("browser")
                finish.set()
                assert child.result(timeout=3) == 0
            else:
                assert child.result(timeout=3) == 2
                assert "browser" not in events and "sync" not in events
            assert events.index("database_closed") < events.index("resources_closed")
            if failure == "before_open":
                with pytest.raises(LockTimeoutError):
                    acquire_exclusive(paths.process_lock_path, timeout=0)
            else:
                lock = acquire_exclusive(paths.process_lock_path, timeout=0)
                lock.release()
    finally:
        finish.set()
        helper.close()
        for lock in owned.values(): lock.release()
