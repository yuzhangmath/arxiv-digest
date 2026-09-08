"""Actual supported pipx bootstrap, two production handoffs and rollback."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from arxiv_digest.models import CategoryConfig
from arxiv_digest.paths import resolve_paths
from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory, ProfileRepository
from arxiv_digest.setup import SetupService
from arxiv_digest.storage.database import open_database
from arxiv_digest.update_runtime import protocol
from tests.update_chain_factory import boundary_error_diagnostics, publish_releases, source_overrides


pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="native updater supports macOS/Linux")


def wait_for(probe, *, timeout=90, description="condition", diagnostics=None):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except (FileNotFoundError, ConnectionError, OSError, json.JSONDecodeError) as error:
            last_error = type(error).__name__
        time.sleep(0.1)
    details = "" if diagnostics is None else "; update diagnostics=" + json.dumps(diagnostics(), sort_keys=True)
    raise AssertionError(f"timed out waiting for {description}; last transient error={last_error}{details}")


def chain_diagnostics(root, paths):
    """Only bounded, validated state labels; never log URLs, tokens or paths."""
    from arxiv_digest.web.lifecycle import _read_private_descriptor

    result = {}
    latest_pid = None
    try:
        current = protocol.JournalStore(paths.update_recovery_dir).read_snapshot()
        result["journal_state"] = "absent" if current is None else current.record["state"]
        if current is not None:
            if "subphase" in current.record:
                result["journal_subphase"] = current.record["subphase"]
            if "receipt" in current.record:
                result["receipt_outcome"] = current.record["receipt"]["outcome"]
    except (OSError, ValueError, RuntimeError):
        result["journal_state"] = "unreadable"
    try:
        with (root / "browser-launches.jsonl").open("rb") as stream:
            payload = stream.read(65537)
        if len(payload) > 65536:
            result["browser_launch_versions"] = "over_limit"
        else:
            launches = [json.loads(line) for line in payload.splitlines()]
            versions = [entry["version"] for entry in launches]
            if any(version not in ("0.3.0", "0.3.1", "0.3.2") for version in versions):
                raise ValueError("unexpected fixture release")
            result["browser_launch_versions"] = versions[-16:]
            if launches and type(launches[-1].get("pid")) is int and launches[-1]["pid"] > 0:
                latest_pid = launches[-1]["pid"]
    except (OSError, ValueError, TypeError, KeyError):
        result["browser_launch_versions"] = "unreadable"
    result["runtime_descriptor_readable"] = False
    result["runtime_matches_latest_launch"] = False
    try:
        runtime = _read_private_descriptor(paths.runtime_descriptor_path)
        result["runtime_descriptor_readable"] = True
        result["runtime_matches_latest_launch"] = runtime.pid == latest_pid
    except (OSError, ValueError, RuntimeError):
        pass
    result["boundary_errors"] = boundary_error_diagnostics(root)
    return result


def relaunch_runtime(root, paths, *, version, outcome, after_pid=None):
    current = protocol.JournalStore(paths.update_recovery_dir).read_snapshot()
    if current is not None and current.record["state"] in protocol.TERMINAL_STATES:
        if (current.record["state"] != "complete"
                or current.record.get("receipt", {}).get("outcome") != outcome):
            # A durable incompatible result cannot become the expected launch.
            # Report it immediately while retaining the normal startup deadline.
            raise AssertionError("relaunch ended without the expected outcome; update diagnostics="
                                 + json.dumps(chain_diagnostics(root, paths), sort_keys=True))
    return browser_runtime(root, paths, version=version, after_pid=after_pid)


def api(runtime, method, path, data=None, *, expected=200, error_code=None):
    connection = http.client.HTTPConnection("127.0.0.1", runtime["port"], timeout=8)
    headers = {"Host": f"127.0.0.1:{runtime['port']}", "Authorization": "Bearer " + runtime["token"]}
    if method == "POST":
        headers.update(Origin=f"http://127.0.0.1:{runtime['port']}", **{"Content-Type": "application/json"})
    try:
        connection.request(method, path, body=None if data is None else json.dumps(data), headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        assert response.status == expected, (response.status, value)
        if error_code is not None:
            assert value["api_version"] == "v1" and value["ok"] is False
            assert value["error"]["code"] == error_code
            return value["error"]
        assert value["api_version"] == "v1" and value["ok"] is True
        return value["data"]
    finally:
        connection.close()


def seed_profile(paths, root):
    paths.ensure()
    pdfs = root / "private-pdfs"
    pdfs.mkdir()
    open_database(paths.database_path).close()
    today = date.today()
    profile = Profile(schema_version=2, revision=1,
        category_coverage=(ProfileCategory("cs.SE", today),), keywords=("synthetic software",),
        phrases=(), authors=(), seed_papers=(), pdf_destination=PdfDestination("custom", pdfs))
    repository = ProfileRepository(paths.profile_path, paths.profile_lock_path)
    SetupService(paths.database_path, repository).publish_profile(
        profile, (CategoryConfig("cs.SE", "cs:SE", today),), expected_revision=None)
    pdf = pdfs / "synthetic-preserved.pdf"
    pdf.write_bytes(b"%PDF-1.7\nsynthetic untouched user file\n")
    return pdf


def browser_runtime(root, paths, *, version, after_pid=None):
    launches = [json.loads(line) for line in (root / "browser-launches.jsonl").read_text().splitlines()]
    matching = [entry for entry in launches if entry["version"] == version and entry["pid"] != after_pid]
    if not matching:
        return None
    runtime = json.loads(paths.runtime_descriptor_path.read_text())
    if runtime["pid"] != matching[-1]["pid"]:
        return None
    status = api(runtime, "GET", "/api/v1/status")
    assert status["startup_nonce"] == runtime["startup_nonce"]
    return runtime


def available(runtime, version):
    status = api(runtime, "GET", "/api/v1/update")
    if status["status"] in {"checking", "idle"}:
        return None
    assert status["status"] == "available_automatic", status
    assert status["available_version"] == version
    return status


def handoff(runtime, version, paths):
    started = api(runtime, "POST", "/api/v1/update/start", {"target_version": version}, expected=202)
    job_id = started["job_id"]
    def prepared():
        job = api(runtime, "GET", "/api/v1/update/jobs/" + job_id)
        assert job["state"] not in {"failed", "canceled"}, job
        return job if job["state"] == "ready_to_restart" else None
    wait_for(prepared, description="real helper READY")
    plan = protocol.ProtectedPlanStore(paths.update_recovery_dir).read_snapshot().record
    assert plan["attempt_id"] == job_id
    snapshot = Path(plan["paths"]["snapshot"])
    assert snapshot.is_dir() and not snapshot.is_symlink()
    snapshot_identity = (snapshot.stat().st_dev, snapshot.stat().st_ino)
    committed = api(runtime, "POST", f"/api/v1/update/jobs/{job_id}/commit", {}, expected=202)
    assert committed["state"] == "restarting"
    # The browser contract renders this response before sending its acknowledgment.
    ack = api(runtime, "POST", f"/api/v1/update/jobs/{job_id}/handoff-ack", {})
    assert ack == {"job_id": job_id, "state": "restarting", "phase": "restarting"}
    return job_id, plan, snapshot_identity


def stop_fixture_processes(root, paths):
    """Only processes whose own command contains this unique fixture directory."""
    try:
        runtime = json.loads(paths.runtime_descriptor_path.read_text())
        api(runtime, "POST", "/api/v1/application/quit", {})
    except (OSError, ValueError, AssertionError, http.client.HTTPException):
        pass
    output = subprocess.check_output(("ps", "-axo", "pid=,command="), text=True)
    owned = []
    for line in output.splitlines():
        identifier, _, command = line.strip().partition(" ")
        if str(root) in command and ("arxiv-digest" in command or "arxiv_digest" in command or "runtime/recovery.py" in command):
            owned.append(int(identifier))
    for pid in owned:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    until = time.monotonic() + 3
    while owned and time.monotonic() < until:
        remaining = []
        for pid in owned:
            try:
                os.kill(pid, 0)
                remaining.append(pid)
            except ProcessLookupError:
                pass
        owned = remaining
        time.sleep(0.05)
    for pid in owned:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("broken_second", [False, True], ids=["two-successful-updates", "second-update-restores-previous"])
def test_real_consecutive_updates_reuse_verified_provenance_and_restore_eligible_previous_release(tmp_path, broken_second):
    from tests.update_release_factory import real_release_installation
    fixture = real_release_installation(tmp_path / "production-chain")
    root = fixture.root
    overrides = source_overrides(root, "0.3.0")
    commit = fixture.add_source_tag("0.3.0", source_overrides=overrides)
    fixture.bootstrap("0.3.0")
    wheels = {version: fixture.write_wheel(version, source_overrides=source_overrides(
        root, version, broken_target=broken_second and version == "0.3.2")) for version in ("0.3.0", "0.3.1", "0.3.2")}
    publish_releases(root, wheels, visible=("0.3.0", "0.3.1"))
    paths = resolve_paths(home=Path(fixture.environ["HOME"]), environ=fixture.environ)
    pdf = seed_profile(paths, root)
    original_pdf = hashlib.sha256(pdf.read_bytes()).hexdigest()
    original_profile = paths.profile_path.read_bytes()
    original_interpreter = (fixture.venv / "bin/python").resolve()
    site = next((fixture.venv / "lib").glob("python*/site-packages"))
    direct = json.loads((site / "arxiv_digest-0.3.0.dist-info/direct_url.json").read_text())
    assert direct == {"url": "https://github.com/yuzhangmath/arxiv-digest.git", "vcs_info": {
        "vcs": "git", "requested_revision": "v0.3.0", "commit_id": commit}}
    assert not paths.update_provenance_path.exists()
    log = (root / "initial-application.log").open("wb")
    process = subprocess.Popen((str(fixture.exposed),), env=fixture.environ, cwd=root,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        first = wait_for(lambda: browser_runtime(root, paths, version="0.3.0"), description="initial dashboard")
        wait_for(lambda: available(first, "0.3.1"), description="canonical-tag automatic eligibility")
        # The old process retains its verified descriptor; the fresh process's
        # single discovery pass sees the newly published next release.
        publish_releases(root, wheels, visible=("0.3.0", "0.3.1", "0.3.2"))
        first_job, first_plan, _ = handoff(first, "0.3.1", paths)
        assert process.wait(timeout=20) == 0
        second = wait_for(lambda: relaunch_runtime(root, paths, version="0.3.1", outcome="updated"), description="healthy first updated dashboard",
                          diagnostics=lambda: chain_diagnostics(root, paths))
        assert second["startup_nonce"] != first["startup_nonce"]
        receipt = api(second, "GET", "/api/v1/update/receipt")["receipt"]
        assert receipt["outcome"] == "updated" and receipt["installed_version"] == "0.3.1"
        assert not os.path.lexists(first_plan["paths"]["snapshot"])
        assert not os.path.lexists(first_plan["paths"]["forensic"])
        first_provenance = protocol.ProtectedProvenanceStore(paths.update_recovery_dir).read_snapshot()
        assert first_provenance.record["attempt_id"] == first_job
        assert first_provenance.record["version"] == "0.3.1"
        # Admission remains blocked until the actually displayed first receipt is acknowledged.
        wait_for(lambda: available(second, "0.3.2"), description="second eligibility from real protected provenance")
        api(second, "POST", "/api/v1/update/start", {"target_version": "0.3.2"},
            expected=409, error_code="pending_update_receipt")
        assert api(second, "POST", f"/api/v1/update/receipt/{receipt['receipt_id']}/ack", {}) == {"acknowledged": True}
        assert api(second, "GET", "/api/v1/update/receipt") == {"receipt": None}
        second_job, second_plan, second_snapshot_identity = handoff(second, "0.3.2", paths)
        final_version = "0.3.1" if broken_second else "0.3.2"
        final = wait_for(lambda: relaunch_runtime(root, paths, version=final_version,
                         outcome="restored" if broken_second else "updated", after_pid=second["pid"]),
                         description="healthy final dashboard after second attempt", diagnostics=lambda: chain_diagnostics(root, paths))
        assert final["startup_nonce"] != second["startup_nonce"]
        receipt = api(final, "GET", "/api/v1/update/receipt")["receipt"]
        assert receipt["outcome"] == ("restored" if broken_second else "updated")
        assert receipt["installed_version"] == final_version and receipt["attempted_version"] == "0.3.2"
        assert not os.path.lexists(second_plan["paths"]["snapshot"])
        saved = protocol.ProtectedProvenanceStore(paths.update_recovery_dir).read_snapshot()
        assert saved.record["version"] == final_version
        if broken_second:
            assert saved.canonical_bytes == first_provenance.canonical_bytes
            # Rollback consumes the verified snapshot into the live environment;
            # terminal cleanup must preserve the failed target and current wheel.
            assert (fixture.venv.stat().st_dev, fixture.venv.stat().st_ino) == second_snapshot_identity
            forensic = Path(second_plan["paths"]["forensic"])
            assert forensic.is_dir() and not forensic.is_symlink()
            failed_metadata = forensic / site.relative_to(fixture.venv) / "arxiv_digest-0.3.2.dist-info/METADATA"
            assert "Version: 0.3.2\n" in failed_metadata.read_text()
        else:
            assert saved.record["attempt_id"] == second_job
            assert not os.path.lexists(second_plan["paths"]["forensic"])
        assert api(final, "POST", f"/api/v1/update/receipt/{receipt['receipt_id']}/ack", {}) == {"acknowledged": True}
        if broken_second:
            wait_for(lambda: available(final, "0.3.2"), description="restored release remains automatically eligible")
        assert paths.profile_path.read_bytes() == original_profile
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == original_pdf
        assert (fixture.venv / "bin/python").resolve() == original_interpreter
        assert Path(saved.record["wheel"]["path"]).is_file()
        assert protocol.JournalStore(paths.update_recovery_dir).read_snapshot().record["state"] == "complete"
    finally:
        stop_fixture_processes(root, paths)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        log.close()


@pytest.mark.parametrize("state,outcome", [
    ("complete", "updated"), ("complete", "restored"), ("aborted_no_mutation", "handoff_failed"),
    ("recovery_failed", "recovery_failed"), ("external_change_detected", "external_change_detected"),
])
@pytest.mark.parametrize("runtime_pid", [12345, 54321])
def test_chain_timeout_reports_validated_state_without_private_fixture_details(tmp_path, state, outcome, runtime_pid, monkeypatch):
    from tests.update_protocol_factory import completed_journal

    paths = resolve_paths(home=tmp_path, environ={})
    _, current = completed_journal(paths.update_recovery_dir)
    record = {**current.record, "state": state, "receipt": {
        **current.record["receipt"], "outcome": outcome, "message_code": protocol.OUTCOME_MESSAGES[outcome],
        "installed_version": "0.3.1" if outcome == "updated" else "0.3.0",
        "launch_id": current.record["receipt"]["launch_id"] if state == "complete" else None,
    }}
    paths.update_journal_path.write_bytes(protocol.encode_journal(record))
    (tmp_path / "browser-launches.jsonl").write_text(json.dumps({
        "version": "0.3.1", "pid": 12345,
        "url": "http://127.0.0.1:1234/#secret-token", "path": str(tmp_path),
    }) + "\n")
    paths.runtime_descriptor_path.write_text(json.dumps({
        "pid": runtime_pid, "port": 1234, "startup_nonce": "a" * 32,
        "token": "A" * 43, "started_at": "2026-01-01T00:00:00Z",
    }))
    paths.runtime_descriptor_path.chmod(0o600)
    with pytest.raises(AssertionError) as failure:
        wait_for(lambda: None, timeout=0, description="synthetic dashboard",
                 diagnostics=lambda: chain_diagnostics(tmp_path, paths))
    message = str(failure.value)
    assert f'"journal_state": "{state}"' in message
    assert f'"receipt_outcome": "{outcome}"' in message
    assert '"browser_launch_versions": ["0.3.1"]' in message
    assert '"runtime_descriptor_readable": true' in message
    assert ('"runtime_matches_latest_launch": ' + str(runtime_pid == 12345).lower()) in message
    assert "secret-token" not in message and "127.0.0.1" not in message
    assert str(tmp_path) not in message and "12345" not in message
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("terminal failures must not wait"))
    marker = object()
    monkeypatch.setattr(sys.modules[__name__], "browser_runtime", lambda *args, **kwargs: marker)
    if state != "complete" or outcome != "updated":
        with pytest.raises(AssertionError, match="relaunch ended without the expected outcome"):
            wait_for(lambda: relaunch_runtime(tmp_path, paths, version="0.3.1", outcome="updated"))
    if state == "complete":
        assert relaunch_runtime(tmp_path, paths, version="0.3.1", outcome=outcome) is marker


@pytest.mark.parametrize("payload", [
    b'{"version":"/private/synthetic-secret"}\n',
    b"malformed synthetic-secret\n", b"synthetic-secret" * 6000,
])
def test_chain_diagnostics_bound_and_redact_untrusted_fixture_bytes(tmp_path, payload):
    paths = resolve_paths(home=tmp_path, environ={})
    paths.update_recovery_dir.mkdir(mode=0o700, parents=True)
    paths.update_journal_path.write_bytes(b"malformed synthetic-secret")
    paths.update_journal_path.chmod(0o600)
    (tmp_path / "browser-launches.jsonl").write_bytes(payload)
    (tmp_path / "update-boundary-errors.jsonl").write_bytes(payload)
    details = chain_diagnostics(tmp_path, paths)
    assert details["journal_state"] == "unreadable"
    assert details["browser_launch_versions"] == ("over_limit" if len(payload) > 65536 else "unreadable")
    assert details["boundary_errors"] == ("over_limit" if len(payload) > 65536 else "unreadable")
    assert "synthetic-secret" not in json.dumps(details)


def test_chain_exception_observers_preserve_behavior_and_bound_structural_logs(tmp_path):
    from tests.update_chain_factory import BOUNDARY_FUNCTIONS, _exception_observers

    namespace = {name: lambda value: value for name in BOUNDARY_FUNCTIONS["update_internal.py"]}
    exec(compile("def _validate_package(value):\n    raise ValueError(value)\n",
                 "/private/synthetic-secret/update_internal.py", "exec"), namespace)
    exec(_exception_observers(tmp_path, "update_internal.py"), namespace)
    marker = object()
    assert namespace["run_self_check"](marker) is marker
    for _ in range(70):
        with pytest.raises(ValueError, match="synthetic-secret"):
            namespace["_validate_package"]("synthetic-secret")
    records = boundary_error_diagnostics(tmp_path)
    assert len(records) == 64
    assert records[0] == {"boundary": "_validate_package", "error": "ValueError", "frames": [
        {"module": "update_internal.py", "function": "_validate_package", "line": 2},
    ]}
    payload = (tmp_path / "update-boundary-errors.jsonl").read_bytes()
    assert len(payload) <= 65536
    assert b"synthetic-secret" not in payload and str(tmp_path).encode() not in payload
    # A diagnostic I/O failure must also preserve the original exception.
    (tmp_path / "update-boundary-errors.jsonl").unlink()
    (tmp_path / "update-boundary-errors.jsonl").mkdir()
    with pytest.raises(ValueError, match="synthetic-secret"):
        namespace["_validate_package"]("synthetic-secret")


@pytest.mark.parametrize("returncode,timed_out", [(0, False), (1, False), (-9, True)])
def test_chain_observers_capture_returned_installer_failure_and_pre_rollback_state(tmp_path, returncode, timed_out):
    from types import SimpleNamespace
    from tests.update_chain_factory import BOUNDARY_FUNCTIONS, _exception_observers

    namespace = {name: lambda *args, **kwargs: None for name in BOUNDARY_FUNCTIONS["update_runtime/helper.py"]}
    namespace["recovery"] = SimpleNamespace(validate_target_installation=lambda value: value)
    namespace["protocol"] = protocol
    result = {"returncode": returncode, "timed_out": timed_out, "private": "synthetic-secret"}
    namespace["run_installer"] = lambda *_args: result
    marker = object()
    namespace["_recover_or_terminal"] = lambda *_args, **_kwargs: marker
    exec(_exception_observers(tmp_path, "update_runtime/helper.py"), namespace)
    assert namespace["run_installer"](None, None) is result
    store = SimpleNamespace(read_snapshot=lambda: SimpleNamespace(record={"state": "installing"}))
    assert namespace["_recover_or_terminal"](None, store, None, None, None) is marker
    assert boundary_error_diagnostics(tmp_path) == [
        {"boundary": "run_installer", "returncode": returncode, "timed_out": timed_out},
        {"boundary": "_recover_or_terminal", "journal_state": "installing"},
    ]
    assert "synthetic-secret" not in (tmp_path / "update-boundary-errors.jsonl").read_text()


def test_chain_observers_capture_target_validation_failure_without_changing_exception(tmp_path):
    from types import SimpleNamespace
    from tests.update_chain_factory import BOUNDARY_FUNCTIONS, _exception_observers

    namespace = {name: lambda *args, **kwargs: None for name in BOUNDARY_FUNCTIONS["update_runtime/helper.py"]}
    failure = ValueError("synthetic-secret")
    def validate_target_installation(_plan):
        raise failure
    namespace["recovery"] = SimpleNamespace(validate_target_installation=validate_target_installation)
    namespace["protocol"] = protocol
    exec(_exception_observers(tmp_path, "update_runtime/helper.py"), namespace)
    with pytest.raises(ValueError) as caught:
        namespace["recovery"].validate_target_installation(None)
    assert caught.value is failure
    assert boundary_error_diagnostics(tmp_path) == [
        {"boundary": "validate_target_installation", "error": "ValueError", "frames": []},
    ]


def test_chain_inventory_differences_expose_categories_without_paths_or_configuration_values(tmp_path):
    from arxiv_digest.update_runtime import recovery
    from tests.update_chain_factory import _fixture_inventory_changes

    environment, snapshot = tmp_path / "environment", tmp_path / "snapshot"
    for root in (environment, snapshot):
        root.mkdir(mode=0o700)
        (root / "pyvenv.cfg").write_text("command = /private/synthetic-secret/python -m venv synthetic\n")
        (root / "pyvenv.cfg").chmod(0o600)
    old = {"inventory": recovery.scan_environment(environment)}
    (environment / "pyvenv.cfg").write_text("command = /private/synthetic-secret/python3.11 -m venv synthetic\n")
    changes = _fixture_inventory_changes({
        "old_token": {"core": old}, "old_version": "0.3.0", "target_version": "0.3.1",
        "paths": {"environment": str(environment), "snapshot": str(snapshot)},
    }, recovery)
    assert changes == [{"member": "venv_configuration", "fields": ["sha256", "size"], "configuration_fields": ["command"]}]
    record = {"boundary": "validate_target_installation", "error": "SnapshotError", "frames": [], "inventory_changes": changes}
    (tmp_path / "update-boundary-errors.jsonl").write_text(json.dumps(record) + "\n")
    assert boundary_error_diagnostics(tmp_path) == [record]
    assert "synthetic-secret" not in json.dumps(record) and str(tmp_path) not in json.dumps(record)


@pytest.mark.parametrize("record", [
    {"boundary": "run_installer", "returncode": "synthetic-secret", "timed_out": False},
    {"boundary": "run_installer", "returncode": 1, "timed_out": "synthetic-secret"},
    {"boundary": "_recover_or_terminal", "journal_state": "synthetic-secret"},
    {"boundary": "validate_target_installation", "error": "SnapshotError", "frames": [],
     "inventory_changes": [{"member": "synthetic-secret", "fields": ["sha256"]}]},
    {"boundary": "validate_target_installation", "error": "SnapshotError", "frames": [],
     "inventory_changes": [{"member": "venv_configuration", "fields": ["sha256"], "configuration_fields": ["synthetic-secret"]}]},
])
def test_chain_diagnostic_reader_rejects_untrusted_installer_and_inventory_values(tmp_path, record):
    (tmp_path / "update-boundary-errors.jsonl").write_text(json.dumps(record) + "\n")
    assert boundary_error_diagnostics(tmp_path) == "unreadable"


def test_real_guard_install_retains_original_interpreter_selector_and_venv_configuration(tmp_path):
    from arxiv_digest.update_runtime import guard, recovery
    from tests.update_release_factory import real_release_installation

    fixture = real_release_installation(tmp_path / "interpreter-selector")
    base = fixture.source_python.resolve()
    selector = fixture.root / "python-selector" / fixture.source_python.name
    selector.parent.mkdir(mode=0o700)
    selector.symlink_to(base)
    fixture.source_python = selector
    fixture.add_source_tag("0.3.0")
    fixture.bootstrap("0.3.0")
    before = (fixture.venv / "pyvenv.cfg").read_bytes()
    external = {path.relative_to(fixture.venv).as_posix(): os.readlink(path)
                for path in (fixture.venv / "bin").iterdir()
                if path.is_symlink() and path.name.startswith("python") and path.resolve() == base}
    core = recovery.capture_core_installation_token(fixture.venv, fixture.exposed, base, allowed_external_symlinks=external)
    target = fixture.write_wheel("0.3.1")
    plan = {"old_version": "0.3.0", "target_version": "0.3.1", "old_token": {"core": core, "provenance": None},
        "target_wheel": {"path": str(target)}, "paths": {
            "environment": str(fixture.venv), "exposed_command": str(fixture.exposed),
            "base_interpreter": str(base), "pipx": str(fixture.pipx),
            **{key: fixture.environ[env] for key, env in (
                ("pipx_home", "PIPX_HOME"), ("pipx_bin_dir", "PIPX_BIN_DIR"),
                ("pipx_shared_libs", "PIPX_SHARED_LIBS"), ("pipx_man_dir", "PIPX_MAN_DIR"),
                ("pipx_completion_dir", "PIPX_COMPLETION_DIR"),
            )},
        }}
    fixture.run(guard.install_argv(plan), environ=guard.install_environment(plan))
    native = json.loads((fixture.venv / "pipx_metadata.json").read_bytes())
    assert native["source_interpreter"]["__Path__"] == str(selector)
    assert (fixture.venv / "pyvenv.cfg").read_bytes() == before
    recovery.validate_target_installation(plan)
