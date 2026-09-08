from __future__ import annotations

import contextlib
import threading
import time
from types import SimpleNamespace

import pytest

from arxiv_digest.update_coordinator import UpdateCoordinator, UpdateRequestError
from tests.update_protocol_factory import admit_journal, next_record


class FakeHelper:
    def __init__(self, store):
        self.store = store
        self.commands = []
        self.pid = 123
        self.alive = True
    def ready(self, deadline): pass
    def command(self, kind, deadline):
        self.commands.append(kind)
        current = self.store.read_snapshot()
        self.store.transition(current, next_record(current, "committed" if kind == "COMMIT" else "canceling_no_install"))
        return "COMMITTED" if kind == "COMMIT" else "CANCELED"
    def terminate_and_prove_dead(self):
        self.alive = False
        return True
    def close(self): pass


@pytest.fixture
def coordinator(tmp_path):
    root = tmp_path / "private"
    store, current = admit_journal(root)
    helper = FakeHelper(store)
    events = []
    stopped = threading.Event()
    class Prepared:
        def __init__(self):
            self.helper = helper
            self.store = store
            self.plan_snapshot = None
        def abort(self):
            snap = store.read_snapshot()
            if snap.record["state"] == "prepared":
                snap = store.transition(snap, next_record(snap, "canceling_no_install"))
            store.transition(snap, next_record(snap, "aborted_no_mutation"))
            events.append("cleanup")
    @contextlib.contextmanager
    def prepare(job_id, descriptor, phase):
        events.append("prepare")
        yield Prepared()
        events.append("released")
    lifecycle = SimpleNamespace(is_closing=False, update_owner=lambda job: contextlib.nullcontext(),
        request_shutdown=lambda intent: events.append("shutdown") or True,
        allow_update_failure_quit=lambda job: events.append("guarded"))
    runtime = SimpleNamespace(lifecycle=lifecycle, resume_after_update_failure=lambda: events.append("resume"))
    value = UpdateCoordinator(runtime=runtime, paths=SimpleNamespace(update_recovery_dir=root),
        checker=SimpleNamespace(descriptor_for=lambda version: object()), prepare=prepare,
        ready_timeout=0.25, admission=lambda: None)
    yield value, helper, events
    value.application_stopping()


def wait_phase(coordinator, identifier, state):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = coordinator.job(identifier)
        if result["state"] == state:
            return result
        time.sleep(0.005)
    pytest.fail(f"job did not reach {state}: {result}")


def test_commit_returns_guidance_before_any_helper_commit(coordinator):
    value, helper, events = coordinator
    started = value.start("0.3.1")
    identifier = started["job_id"]
    wait_phase(value, identifier, "ready_to_restart")
    assert value.start("0.3.1")["job_id"] == identifier
    result = value.commit(identifier)
    assert result["state"] == "restarting"
    assert helper.commands == []
    assert "shutdown" not in events
    value.handoff_ack(identifier)
    assert helper.commands == ["COMMIT"]
    assert "shutdown" in events
    assert "released" not in events
    value.application_stopping()
    assert "released" in events


def test_ready_deadline_cancels_before_retry_permission(coordinator):
    value, helper, events = coordinator
    started = value.start("0.3.1")
    result = wait_phase(value, started["job_id"], "canceled")
    assert result["error_code"] == "handoff_not_acknowledged"
    assert helper.commands == ["CANCEL"]
    assert events.index("cleanup") < events.index("released") < events.index("resume")
    with pytest.raises(UpdateRequestError):
        value.handoff_ack(started["job_id"])


def test_lost_committed_never_sends_cancel_or_restores_launcher(coordinator):
    value, helper, events = coordinator
    original = helper.command
    def lose(kind, deadline):
        original(kind, deadline)
        raise TimeoutError("private helper output")
    helper.command = lose
    started = value.start("0.3.1")
    wait_phase(value, started["job_id"], "ready_to_restart")
    value.commit(started["job_id"])
    with pytest.raises(UpdateRequestError):
        value.handoff_ack(started["job_id"])
    result = wait_phase(value, started["job_id"], "failed")
    assert result["error_code"] == "helper_commit_failed"
    assert "CANCEL" not in helper.commands
    assert "cleanup" not in events
    assert "shutdown" not in events
    assert "resume" not in events
    assert "private" not in str(result)
