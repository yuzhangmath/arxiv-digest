from __future__ import annotations

from pathlib import Path
import runpy

import pytest


class FakeApplication:
    def __init__(self) -> None:
        self.calls = []

    def open_dashboard(self, intent: str) -> int:
        self.calls.append(("dashboard", intent))
        return 0

    def doctor(self) -> int:
        self.calls.append(("doctor",))
        return 0

    def export_backup(self, destination: Path) -> int:
        self.calls.append(("export", destination))
        return 0

    def import_backup(self, source: Path) -> int:
        self.calls.append(("import", source))
        return 0

    def install_launcher(self) -> int:
        self.calls.append(("install-launcher",))
        return 0


def test_cli_exposes_only_the_exact_supported_command_surface(tmp_path) -> None:
    from arxiv_digest.cli import main

    backup = tmp_path / "backup.zip"
    cases = (
        ([], ("dashboard", "default")),
        (["init"], ("dashboard", "init")),
        (["library"], ("dashboard", "library")),
        (["config"], ("dashboard", "config")),
        (["doctor"], ("doctor",)),
        (["export", str(backup)], ("export", backup)),
        (["import", str(backup)], ("import", backup)),
        (["install-launcher"], ("install-launcher",)),
    )

    for arguments, expected in cases:
        application = FakeApplication()
        assert main(arguments, application_factory=lambda: application) == 0
        assert application.calls == [expected]


def test_malformed_and_email_commands_fail_before_application_creation() -> None:
    from arxiv_digest.cli import main

    created = []
    for arguments in (["paper.eml"], ["import-eml", "paper.eml"], ["export"]):
        with pytest.raises(SystemExit) as raised:
            main(arguments, application_factory=lambda: created.append(True))
        assert raised.value.code != 0

    assert created == []


def test_python_module_entrypoint_delegates_to_cli(monkeypatch) -> None:
    import arxiv_digest.cli

    called = []
    monkeypatch.setattr(
        arxiv_digest.cli,
        "main",
        lambda: called.append(True) or 7,
    )

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("arxiv_digest.__main__", run_name="__main__")

    assert raised.value.code == 7
    assert called == [True]


def test_plain_launch_without_profile_opens_setup_without_sync() -> None:
    from arxiv_digest.application import Application

    calls = []

    class Paths:
        def ensure(self):
            calls.append("ensure")

    class Ownership:
        def publish(self, **values):
            calls.append(("publish", values))

    class Instances:
        def acquire(self):
            calls.append("acquire")
            return Ownership()

        def release(self):
            calls.append("release")

    class Server:
        port = 43123
        token = "A" * 43
        startup_nonce = "nonce_abcd12345678"

        def start(self):
            calls.append("server-start")

        def stop(self):
            calls.append("server-stop")

        def launch_url(self, view):
            return f"http://127.0.0.1:43123/#token={'A' * 43}&view={view}"

    application = Application(
        paths=Paths(),
        profile_exists=lambda: False,
        instance_factory=lambda: Instances(),
        server_factory=lambda handlers: Server(),
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: calls.append("restore"),
        open_database=lambda: calls.append("database"),
        start_sync=lambda: calls.append("sync"),
        browser_open=lambda url: calls.append(("browser", url)) or True,
        wait_for_server=lambda server: calls.append("wait"),
    )

    assert application.open_dashboard("default") == 0
    assert "sync" not in calls
    assert calls.index("restore") < calls.index("database")
    assert calls.count("server-start") == 1
    assert calls.count("wait") == 1
    assert any(
        call[0] == "browser" and call[1].endswith("&view=setup")
        for call in calls
        if isinstance(call, tuple)
    )


def test_second_invocation_opens_verified_instance_without_server_or_mutation() -> None:
    from arxiv_digest.application import Application
    from arxiv_digest.web.lifecycle import ExistingInstance, RuntimeDescriptor

    descriptor = RuntimeDescriptor(
        pid=123,
        port=43123,
        startup_nonce="nonce_abcd12345678",
        token="B" * 43,
        started_at="2026-08-22T12:00:00Z",
    )
    opened = []

    class Paths:
        def ensure(self):
            return None

    class Instances:
        def acquire(self):
            return ExistingInstance(descriptor)

        def release(self):
            raise AssertionError("non-owner must not release the running instance")

    for intent, view in (
        ("default", "review"),
        ("init", "interests"),
        ("library", "library"),
        ("config", "settings"),
    ):
        application = Application(
            paths=Paths(),
            profile_exists=lambda: True,
            instance_factory=lambda: Instances(),
            server_factory=lambda handlers: (_ for _ in ()).throw(
                AssertionError("second invocation started a server")
            ),
            handlers_factory=lambda: (_ for _ in ()).throw(
                AssertionError("second invocation built mutations")
            ),
            resolve_restore_journal=lambda: None,
            open_database=lambda: (_ for _ in ()).throw(
                AssertionError("second invocation opened SQLite")
            ),
            start_sync=lambda: (_ for _ in ()).throw(
                AssertionError("second invocation started synchronization")
            ),
            browser_open=lambda url: opened.append(url) or True,
            wait_for_server=lambda server: None,
        )
        assert application.open_dashboard(intent) == 0
        assert opened[-1] == (
            "http://127.0.0.1:43123/"
            f"#token={'B' * 43}&view={view}"
        )


def test_standalone_export_refuses_a_running_dashboard_before_snapshot(tmp_path) -> None:
    from arxiv_digest.application import Application
    from arxiv_digest.web.lifecycle import ExistingInstance, RuntimeDescriptor

    descriptor = RuntimeDescriptor(
        pid=123,
        port=43123,
        startup_nonce="nonce_abcd12345678",
        token="C" * 43,
        started_at="2026-08-22T12:00:00Z",
    )
    exported = []

    class Paths:
        def ensure(self):
            return None

    class Instances:
        def acquire(self):
            return ExistingInstance(descriptor)

        def release(self):
            raise AssertionError("contender released another process lock")

    application = Application(
        paths=Paths(),
        profile_exists=lambda: True,
        instance_factory=lambda: Instances(),
        server_factory=lambda handlers: None,
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: None,
        open_database=lambda: None,
        start_sync=lambda: None,
        browser_open=lambda url: True,
        wait_for_server=lambda server: None,
        export_action=lambda destination: exported.append(destination) or 0,
    )

    with pytest.raises(RuntimeError, match="Settings"):
        application.export_backup(tmp_path / "backup.zip")
    assert exported == []


def test_standalone_import_holds_ownership_across_recovery_and_restore(tmp_path) -> None:
    from arxiv_digest.application import Application

    calls = []

    class Paths:
        def ensure(self):
            calls.append("ensure")

    class Ownership:
        pass

    class Instances:
        owned = False

        def acquire(self):
            calls.append("acquire")
            self.owned = True
            return Ownership()

        def release(self):
            calls.append("release")
            self.owned = False

    instances = Instances()
    source = tmp_path / "backup.zip"
    source.write_bytes(b"archive")

    def restore(path):
        assert instances.owned is True
        calls.append(("restore", path))
        return 0

    application = Application(
        paths=Paths(),
        profile_exists=lambda: True,
        instance_factory=lambda: instances,
        server_factory=lambda handlers: None,
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: calls.append("recover"),
        open_database=lambda: None,
        start_sync=lambda: None,
        browser_open=lambda url: True,
        wait_for_server=lambda server: None,
        import_action=restore,
    )

    assert application.import_backup(source) == 0
    assert calls == ["ensure", "acquire", "recover", ("restore", source), "release"]


def test_corrupt_database_is_preserved_and_reported_without_server_start(tmp_path) -> None:
    from arxiv_digest.application import Application
    from arxiv_digest.storage.database import CorruptDatabaseError

    database_path = tmp_path / "state.sqlite3"
    original = b"corrupt-but-preserved"
    database_path.write_bytes(original)
    output = []
    calls = []

    class Paths:
        def __init__(self):
            self.database_path = database_path

        def ensure(self):
            return None

    class Instances:
        def acquire(self):
            return object()

        def release(self):
            calls.append("release")

    application = Application(
        paths=Paths(),
        profile_exists=lambda: True,
        instance_factory=lambda: Instances(),
        server_factory=lambda handlers: calls.append("server") or None,
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: None,
        open_database=lambda: (_ for _ in ()).throw(
            CorruptDatabaseError("sensitive path and SQLite detail")
        ),
        start_sync=lambda: calls.append("sync"),
        browser_open=lambda url: True,
        wait_for_server=lambda server: None,
        output=output.append,
    )

    assert application.open_dashboard("default") != 0
    assert database_path.read_bytes() == original
    assert calls == ["release"]
    assert len(output) == 1
    assert "backup" in output[0].casefold()
    assert "sensitive" not in output[0]


def test_dashboard_closes_open_database_after_server_stops() -> None:
    from arxiv_digest.application import Application

    calls = []

    class Paths:
        def ensure(self):
            return None

    class Instances:
        def acquire(self):
            return object()

        def release(self):
            calls.append("release")

    class Database:
        def close(self):
            calls.append("database-close")

    class Server:
        port = 43123
        token = "D" * 43
        startup_nonce = "nonce_abcd12345678"

        def start(self):
            return None

        def stop(self):
            calls.append("server-stop")

        def launch_url(self, view):
            return f"http://127.0.0.1:43123/#token={'D' * 43}&view={view}"

    class Ownership:
        def publish(self, **values):
            return None

    instances = Instances()
    instances.acquire = lambda: Ownership()
    application = Application(
        paths=Paths(),
        profile_exists=lambda: False,
        instance_factory=lambda: instances,
        server_factory=lambda handlers: Server(),
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: None,
        open_database=Database,
        start_sync=lambda: None,
        browser_open=lambda url: True,
        wait_for_server=lambda server: None,
    )

    assert application.open_dashboard("default") == 0
    assert calls == ["server-stop", "database-close", "release"]


def test_default_composition_doctor_is_read_only_on_a_fresh_root(tmp_path) -> None:
    from arxiv_digest.application import create_application
    from arxiv_digest.paths import resolve_paths

    root = tmp_path / "uncreated-root"
    paths = resolve_paths(
        home=tmp_path / "home",
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(root),
        },
    )
    output = []

    application = create_application(paths=paths, output=output.append)

    assert application.doctor() == 0
    assert root.exists() is False
    assert len(output) == 1
    assert "not initialized" in output[0]
