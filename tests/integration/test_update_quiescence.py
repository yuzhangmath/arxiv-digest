"""Real application shutdown with a deliberately disarmed lock-only helper."""

from __future__ import annotations

import http.client
import json
import os
import select
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import pytest

from arxiv_digest.application import Application, _DefaultRuntime
from arxiv_digest.maintenance import MaintenanceBarrier, UpdateInProgressError
from arxiv_digest.paths import resolve_paths
from arxiv_digest.profile import ProfileRepository
from arxiv_digest.update_contract import ShutdownIntent
from arxiv_digest.update_locks import LockTimeoutError, acquire_exclusive
from arxiv_digest.web.lifecycle import LifecycleController, SingleInstance


def _request(server, method, target):
    connection = http.client.HTTPConnection(server.host, server.port, timeout=2)
    headers = {
        "Host": f"{server.host}:{server.port}",
        "Authorization": f"Bearer {server.token}",
        "Origin": f"http://{server.host}:{server.port}",
    }
    connection.request(method, target, headers=headers)
    response = connection.getresponse()
    value = json.loads(response.read())
    connection.close()
    return response.status, value


def test_real_application_closes_resources_before_disarmed_handoff_and_keeps_guards(
    tmp_path, monkeypatch,
) -> None:
    paths = resolve_paths(environ={
        "ARXIV_DIGEST_TESTING": "1", "ARXIV_DIGEST_TEST_ROOT": str(tmp_path),
    })
    paths.ensure()
    paths.ensure_update_coordination()
    barrier = MaintenanceBarrier()
    now = [0.0]
    lifecycle = LifecycleController(clock=lambda: now[0], inactivity_seconds=1, lease_seconds=1)
    profiles = ProfileRepository(paths.profile_path, paths.profile_lock_path, maintenance=barrier)
    runtime = _DefaultRuntime(paths, profiles, barrier, lifecycle, output=lambda _: None)
    monkeypatch.setattr(runtime, "_launcher_manager", lambda: None)
    started = threading.Event()
    admitted = threading.Event()
    release_handler = threading.Event()
    preparation_owned = threading.Event()
    start_quiescence = threading.Event()
    quiescent = threading.Event()
    commit = threading.Event()
    resources_closed = threading.Event()
    release_guards = threading.Event()
    errors = []
    servers = []
    helpers = []
    connections = []
    closed_connections = []
    admitted_responses = []

    def delayed_settings(_payload):
        admitted.set()
        assert release_handler.wait(5)
        connection = runtime.store._connect()
        connections.append(connection)
        try:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        finally:
            connection.close()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
        closed_connections.append(connection)
        return {"finished": True}

    def handlers():
        # Both the server and runtime are production components; the slow
        # handler exposes its real pre-storage admission race deterministically.
        result = dict(runtime.handlers())
        result["settings_get"] = delayed_settings
        return result

    def server_factory(handler_map):
        server = runtime.server(handler_map)
        servers.append(server)
        return server

    def present(url):
        assert urlsplit(url).hostname == "127.0.0.1"
        started.set()
        return True

    def application_stopping():
        try:
            assert servers[0]._httpd is None
            assert runtime._runtime_closed
            assert barrier.update_active
            assert barrier.active_operations == 0
            assert not barrier.work_active
            assert connections
            assert connections == closed_connections
            with pytest.raises(UpdateInProgressError):
                with barrier.operation():
                    pytest.fail("resource closure released the update latch")
            for path in (
                paths.process_lock_path,
                paths.update_transition_lock_path,
                paths.launcher_operation_lock_path,
            ):
                with pytest.raises(LockTimeoutError):
                    acquire_exclusive(path, timeout=0)
            resources_closed.set()
        except BaseException as error:
            errors.append(error)
            raise

    app = Application(
        paths=paths, profile_exists=lambda: False,
        instance_factory=lambda: SingleInstance(paths.process_lock_path, paths.runtime_descriptor_path),
        server_factory=server_factory, handlers_factory=handlers,
        resolve_restore_journal=lambda: None,
        open_database=runtime.open_database, start_sync=runtime.start_sync,
        browser_open=present, wait_for_server=lambda server: server.wait(poll_interval=0.01),
        application_stopping=application_stopping,
    )

    def run_app():
        try:
            assert app.open_dashboard("default") == 0
        except BaseException as error:
            errors.append(error)

    def coordinator():
        guards = []
        try:
            with lifecycle.update_owner("update_disarmed_fixture"):
                preparation_owned.set()
                assert start_quiescence.wait(5)
                with runtime.begin_update_quiescence(timeout=5) as canceled:
                    assert canceled == ()
                    for path in (paths.update_transition_lock_path, paths.launcher_operation_lock_path):
                        guards.append(acquire_exclusive(path, timeout=1).transfer_close_only())
                    helper = subprocess.Popen(
                        [sys.executable, "-I", "-c", (
                            "import os,sys\n"
                            "print('READY', flush=True)\n"
                            "assert sys.stdin.buffer.read(1) == b'X'\n"
                            "for fd in map(int, sys.argv[1:]): os.close(fd)\n"
                        ), *(str(guard.fileno()) for guard in guards)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        pass_fds=tuple(guard.fileno() for guard in guards),
                    )
                    helpers.append(helper)
                    assert select.select([helper.stdout], [], [], 5)[0]
                    assert helper.stdout.readline() == b"READY\n"
                    quiescent.set()
                    assert commit.wait(5)
                    assert lifecycle.request_shutdown(ShutdownIntent.UPDATE_RESTART)
                    assert release_guards.wait(10)
                    assert resources_closed.is_set()
                    # The production helper waits for process death.
                    # This disarmed harness proves the earlier resource/lock
                    # boundary while intentionally running no installation.
        except BaseException as error:
            errors.append(error)
        finally:
            for guard in reversed(guards):
                guard.close()

    app_thread = threading.Thread(target=run_app, daemon=True)
    update_thread = threading.Thread(target=coordinator, daemon=True)
    requester = None
    app_thread.start()
    try:
        assert started.wait(5)
        server = servers[0]
        # Cache actual runtime health before quiescence. Later health must not
        # reenter status(), whose production path reads profiles and SQLite.
        status, cached = _request(server, "GET", "/api/v1/status")
        assert status == 200
        update_thread.start()
        assert preparation_owned.wait(5)
        now[0] = 1000.0
        assert not lifecycle.should_stop()
        assert not lifecycle.request_quit()
        requester = threading.Thread(
            target=lambda: admitted_responses.append(_request(server, "GET", "/api/v1/settings")),
            daemon=True,
        )
        requester.start()
        assert admitted.wait(5)
        start_quiescence.set()
        # Observe the actual latch through the server, without racing a sleep
        # against coordinator scheduling.
        with barrier._condition:
            assert barrier._condition.wait_for(lambda: barrier.update_active, timeout=5)
        assert not quiescent.is_set()
        status, blocked = _request(server, "GET", "/api/v1/backup/export")
        assert status == 409
        assert blocked["error"]["code"] == "update_in_progress"
        release_handler.set()
        assert quiescent.wait(5)
        requester.join(5)
        assert admitted_responses[0][0] == 200
        assert _request(server, "GET", "/api/v1/status") == (200, cached)
        assert _request(server, "POST", "/api/v1/application/quit")[0] == 409
        assert helpers[0].poll() is None
        commit.set()
        assert resources_closed.wait(5), repr(errors)
        app_thread.join(5)
        assert not app_thread.is_alive()
        assert barrier.update_active
        assert not paths.runtime_descriptor_path.exists()
        ordinary = acquire_exclusive(paths.process_lock_path, timeout=0)
        ordinary.release()
        with pytest.raises(OSError):
            _request(server, "GET", "/api/v1/status")
        release_guards.set()
        update_thread.join(5)
        assert not update_thread.is_alive()
        # Closing the parent's references cannot unlock the helper's borrowed
        # open-file descriptions; they remain held after runtime teardown.
        for path in (paths.update_transition_lock_path, paths.launcher_operation_lock_path):
            with pytest.raises(LockTimeoutError):
                acquire_exclusive(path, timeout=0)
        helpers[0].communicate(b"X", timeout=5)
        assert helpers[0].returncode == 0
        assert errors == []
        assert not barrier.update_active
    finally:
        release_handler.set()
        start_quiescence.set()
        commit.set()
        release_guards.set()
        if not lifecycle.is_closing:
            if lifecycle._update_job is not None:
                lifecycle.allow_update_failure_quit("update_disarmed_fixture")
            lifecycle.request_quit()
        app_thread.join(5)
        if update_thread.ident is not None:
            update_thread.join(5)
        if requester is not None:
            requester.join(5)
        for helper in helpers:
            if helper.poll() is None:
                helper.kill()
                helper.communicate(timeout=5)
