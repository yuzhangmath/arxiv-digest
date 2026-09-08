from __future__ import annotations

from pathlib import Path
import runpy

import pytest


class FakeApplication:
    def __init__(self) -> None:
        self.calls = []

    def open_dashboard(
        self, intent: str, *, instance_resolved=lambda: None, copy_url=False
    ) -> int:
        self.calls.append(
            ("dashboard", intent, "copy") if copy_url else ("dashboard", intent)
        )
        instance_resolved()
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


@pytest.mark.parametrize(
    ("arguments", "intent"),
    [
        (["--copy-url"], "default"),
        (["init", "--copy-url"], "init"),
        (["library", "--copy-url"], "library"),
        (["config", "--copy-url"], "config"),
        (["--copy-url", "init"], "init"),
    ],
)
def test_cli_can_copy_each_dashboard_url(arguments, intent) -> None:
    from arxiv_digest.cli import main

    application = FakeApplication()
    assert main(arguments, application_factory=lambda: application) == 0
    assert application.calls == [("dashboard", intent, "copy")]


def test_copy_url_rejects_non_dashboard_commands_before_application_creation() -> None:
    from arxiv_digest.cli import main

    for command in ("doctor", "export", "import", "install-launcher"):
        arguments = ["--copy-url", command]
        if command in {"export", "import"}:
            arguments.append("synthetic-backup.zip")
        with pytest.raises(SystemExit) as raised:
            main(
                arguments,
                application_factory=lambda: pytest.fail("created application"),
            )
        assert raised.value.code == 2


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(("copy_requested", "copied"), [
    (True, False), (True, True), (False, False),
])
def test_dashboard_url_fallback_preserves_session_and_server_lifetime(
    existing, copy_requested, copied
):
    from types import SimpleNamespace

    from arxiv_digest.application import Application
    from arxiv_digest.web.lifecycle import ExistingInstance, RuntimeDescriptor

    events = []
    output = []
    url = f"http://127.0.0.1:43123/#token={'B' * 43}&view=library"
    descriptor = RuntimeDescriptor(
        pid=123,
        port=43123,
        startup_nonce="nonce_abcd12345678",
        token="B" * 43,
        started_at="2026-08-22T12:00:00Z",
    )
    claim = ExistingInstance(descriptor) if existing else SimpleNamespace(
        publish=lambda **kwargs: events.append("publish")
    )
    server = SimpleNamespace(
        port=43123,
        token="B" * 43,
        startup_nonce="nonce_abcd12345678",
        start=lambda: events.append("start"),
        stop=lambda: events.append("stop"),
        launch_url=lambda view: url,
    )

    def copy(value):
        assert copy_requested
        assert value == url
        events.append("copy")
        return copied

    def open_browser(value):
        assert not copy_requested
        assert value == url
        events.append("browser")
        return False

    application = Application(
        paths=SimpleNamespace(ensure=lambda: None),
        profile_exists=lambda: True,
        instance_factory=lambda: SimpleNamespace(
            acquire=lambda: claim,
            release=lambda: events.append("release"),
        ),
        server_factory=lambda handlers: server,
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: events.append("restore"),
        open_database=lambda: events.append("database"),
        start_sync=lambda: pytest.fail("library launch started sync"),
        browser_open=open_browser,
        clipboard_copy=copy,
        wait_for_server=lambda running: events.append("wait"),
        output=output.append,
    )

    assert application.open_dashboard("library", copy_url=copy_requested) == 0
    assert url in output
    assert ("copied to the clipboard" in "\n".join(output)) is copied
    if copy_requested and not copied:
        assert "Could not copy" in "\n".join(output)
    if not copy_requested:
        assert "Could not open a browser" in "\n".join(output)
        assert "arxiv-digest --copy-url" in "\n".join(output)
    action = "copy" if copy_requested else "browser"
    assert events == ([action] if existing else [
        "restore", "database", "start", "publish", action, "wait", "stop", "release"
    ])


def test_cli_preflight_wraps_the_complete_non_dashboard_operation() -> None:
    from arxiv_digest.cli import PreflightDisposition, main

    events: list[str] = []

    class Paths:
        update_transition_lock_path = Path("/fixed/update-transition.lock")
        update_journal_path = Path("/fixed/update-journal.json")
        recovery_wrapper_path = Path("/fixed/recover-arxiv-digest")

        def ensure_update_coordination(self) -> None:
            events.append("ensure-coordination")

    class Transition:
        def release(self) -> None:
            events.append("release-transition")

    class Application(FakeApplication):
        def doctor(self) -> int:
            events.append("action")
            return 0

    assert main(
        ["doctor"],
        application_factory=lambda: events.append("application") or Application(),
        paths_factory=lambda: events.append("paths") or Paths(),
        journal_classifier=lambda path: events.append(f"classify:{path}")
        or PreflightDisposition.ALLOW,
        transition_acquire=lambda path: events.append(f"acquire:{path}")
        or Transition(),
        internal_dispatch=lambda argv: events.append("internal-recognizer"),
    ) == 0
    assert events == [
        "internal-recognizer",
        "paths",
        "ensure-coordination",
        "acquire:/fixed/update-transition.lock",
        "classify:/fixed/update-journal.json",
        "application",
        "action",
        "release-transition",
    ]


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


def test_dashboard_reports_instance_resolution_before_browser_or_data_access() -> None:
    from arxiv_digest.application import Application
    from arxiv_digest.web.lifecycle import ExistingInstance, RuntimeDescriptor

    events: list[str] = []
    descriptor = RuntimeDescriptor(
        pid=123,
        port=43123,
        startup_nonce="nonce_abcd12345678",
        token="B" * 43,
        started_at="2026-08-22T12:00:00Z",
    )

    class Paths:
        def ensure(self) -> None:
            events.append("ensure-data")

    class Instances:
        def acquire(self):
            events.append("instance-resolved")
            return ExistingInstance(descriptor)

    application = Application(
        paths=Paths(),
        profile_exists=lambda: events.append("profile") or True,
        instance_factory=Instances,
        server_factory=lambda handlers: None,
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: events.append("restore"),
        open_database=lambda: events.append("database"),
        start_sync=lambda: events.append("sync"),
        browser_open=lambda url: events.append("browser") or True,
        wait_for_server=lambda server: None,
    )

    assert application.open_dashboard(
        "default",
        instance_resolved=lambda: events.append("transition-released"),
    ) == 0
    assert events == [
        "ensure-data",
        "instance-resolved",
        "transition-released",
        "profile",
        "browser",
    ]


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


def test_dashboard_releases_new_instance_when_transition_callback_fails() -> None:
    from types import SimpleNamespace

    from arxiv_digest.application import Application

    events: list[str] = []

    def callback() -> None:
        events.append("callback")
        raise OSError("transition release failed")

    application = Application(
        paths=SimpleNamespace(ensure=lambda: None),
        profile_exists=lambda: events.append("profile") or False,
        instance_factory=lambda: SimpleNamespace(
            acquire=lambda: object(),
            release=lambda: events.append("release-instance"),
        ),
        server_factory=lambda handlers: events.append("server"),
        handlers_factory=lambda: {},
        resolve_restore_journal=lambda: events.append("restore"),
        open_database=lambda: events.append("database"),
        start_sync=lambda: None,
        browser_open=lambda url: True,
        wait_for_server=lambda server: None,
    )

    with pytest.raises(OSError, match="transition release failed"):
        application.open_dashboard("default", instance_resolved=callback)
    assert events == ["callback", "release-instance"]


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
