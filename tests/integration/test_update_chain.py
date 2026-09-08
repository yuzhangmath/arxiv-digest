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
from tests.update_chain_factory import publish_releases, source_overrides


pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="native updater supports macOS/Linux")


def wait_for(probe, *, timeout=90, description="condition"):
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
    raise AssertionError(f"timed out waiting for {description}; last transient error={last_error}")


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
        second = wait_for(lambda: browser_runtime(root, paths, version="0.3.1"), description="healthy first updated dashboard")
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
        final = wait_for(lambda: browser_runtime(root, paths, version=final_version, after_pid=second["pid"]),
                         description="healthy final dashboard after second attempt")
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
