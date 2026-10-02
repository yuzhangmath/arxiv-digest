from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts import publish_verifier as verifier


ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.3.1"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
NOTES = b"# Synthetic release notes\n"


@pytest.fixture
def bundle_path(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    root.mkdir()
    names = (f"arxiv_digest-{VERSION}-py3-none-any.whl", f"arxiv_digest-{VERSION}.tar.gz")
    checksums = []
    for name in names:
        payload = f"synthetic {name}\n".encode()
        (root / name).write_bytes(payload)
        checksums.append(f"{hashlib.sha256(payload).hexdigest()}  {name}\n")
    (root / "SHA256SUMS").write_text("".join(checksums), encoding="ascii")
    (root / "RELEASE_NOTES.md").write_bytes(NOTES)
    (root / "COMMIT_SHA").write_text(COMMIT + "\n", encoding="ascii")
    return root


def verified_bundle(root: Path):
    return verifier.verify_downloaded_bundle(root, version=VERSION, commit=COMMIT)


def release_record(bundle) -> dict:
    return {
        "tag_name": f"v{bundle.version}",
        "draft": False,
        "prerelease": True,
        "body": NOTES.decode(),
        "assets": [
            {
                "name": asset.name,
                "state": "uploaded",
                "size": asset.size,
                "digest": f"sha256:{asset.sha256}",
                "browser_download_url": f"{verifier.REPOSITORY}/releases/download/v{bundle.version}/{asset.name}",
            }
            for asset in bundle.assets
        ],
    }


def test_bundle_and_published_release_match_exact_artifacts(bundle_path):
    bundle = verified_bundle(bundle_path)
    assert bundle.version == VERSION
    assert bundle.commit == COMMIT
    assert bundle.channel == "prerelease"
    assert bundle.release_notes_sha256 == hashlib.sha256(NOTES).hexdigest()
    assert [asset.name for asset in bundle.assets] == [
        f"arxiv_digest-{VERSION}-py3-none-any.whl",
        f"arxiv_digest-{VERSION}.tar.gz",
        "SHA256SUMS",
    ]
    for asset in bundle.assets:
        payload = (bundle_path / asset.name).read_bytes()
        assert (asset.size, asset.sha256) == (len(payload), hashlib.sha256(payload).hexdigest())
    verifier.verify_github_release(
        json.dumps(release_record(bundle)).encode(),
        bundle=bundle, tag=f"v{VERSION}", remote_tag_commit=COMMIT,
    )


@pytest.mark.parametrize("mutation", [
    "extra", "missing", "symlink", "directory", "hardlink", "empty", "checksum", "commit", "notes",
])
def test_bundle_rejects_unsafe_or_changed_artifacts(bundle_path, mutation):
    notes = bundle_path / "RELEASE_NOTES.md"
    if mutation == "extra":
        (bundle_path / "extra.py").write_text("raise AssertionError()")
    elif mutation == "missing":
        notes.unlink()
    elif mutation in {"symlink", "directory", "hardlink"}:
        notes.unlink()
        if mutation == "symlink":
            notes.symlink_to(bundle_path / "COMMIT_SHA")
        elif mutation == "hardlink":
            os.link(bundle_path / "COMMIT_SHA", notes)
        else:
            notes.mkdir()
    elif mutation == "empty":
        (bundle_path / f"arxiv_digest-{VERSION}.tar.gz").write_bytes(b"")
    elif mutation == "checksum":
        (bundle_path / "SHA256SUMS").write_text("0" * 64)
    elif mutation == "commit":
        (bundle_path / "COMMIT_SHA").write_text("0" * 40 + "\n")
    else:
        notes.write_bytes(b"\xff")
    with pytest.raises(ValueError):
        verified_bundle(bundle_path)


@pytest.mark.parametrize("version,commit", [("03.1.0", COMMIT), (VERSION, COMMIT.upper()), (VERSION, "short")])
def test_bundle_rejects_invalid_release_identity(bundle_path, version, commit):
    with pytest.raises(ValueError):
        verifier.verify_downloaded_bundle(bundle_path, version=version, commit=commit)


def test_bundle_detects_file_substitution_during_verification(bundle_path, monkeypatch):
    read = verifier._read_regular_bytes

    def replace_after_read(path, *, byte_limit):
        payload = read(path, byte_limit=byte_limit)
        if path.name == "SHA256SUMS":
            path.write_bytes(b"changed\n")
        return payload

    monkeypatch.setattr(verifier, "_read_regular_bytes", replace_after_read)
    with pytest.raises(verifier.BundleError):
        verified_bundle(bundle_path)


@pytest.mark.parametrize("field,value", [
    ("tag_name", "v0.0.0"), ("draft", True), ("prerelease", False), ("body", "changed"),
    ("name", "renamed.whl"), ("state", "new"), ("size", True),
    ("digest", "sha256:" + "0" * 64), ("browser_download_url", "https://example.invalid/asset"),
    ("assets", []),
])
def test_published_release_rejects_mismatched_identity_or_assets(bundle_path, field, value):
    bundle = verified_bundle(bundle_path)
    record = release_record(bundle)
    target = record if field in record else record["assets"][0]
    target[field] = value
    with pytest.raises(verifier.BundleError):
        verifier.verify_github_release(
            json.dumps(record).encode(), bundle=bundle,
            tag=f"v{VERSION}", remote_tag_commit=COMMIT,
        )


@pytest.mark.parametrize("payload", [b'{"draft":false,"draft":false}', b"NaN", b"{", b"[]", b"\xff"])
def test_published_release_rejects_invalid_json(bundle_path, payload):
    with pytest.raises(verifier.BundleError):
        verifier.verify_github_release(
            payload, bundle=verified_bundle(bundle_path), tag=f"v{VERSION}", remote_tag_commit=COMMIT,
        )


def test_published_release_rejects_changed_tag_commit_and_oversized_response(bundle_path):
    bundle = verified_bundle(bundle_path)
    payload = json.dumps(release_record(bundle)).encode()
    for content, commit in ((payload, "0" * 40), (b" " * (verifier.RELEASE_PAGE_BYTE_LIMIT + 1), COMMIT)):
        with pytest.raises(verifier.BundleError):
            verifier.verify_github_release(content, bundle=bundle, tag=f"v{VERSION}", remote_tag_commit=commit)


def test_cli_reports_invalid_bundle_without_local_paths(bundle_path, capsys):
    (bundle_path / "COMMIT_SHA").unlink()
    assert verifier.main(["--bundle", str(bundle_path), "--version", VERSION, "--commit", COMMIT]) == 1
    assert capsys.readouterr().err == "release verification failed\n"


def test_workflow_embeds_the_same_stdlib_verifier_and_runs_it_in_isolation(bundle_path, tmp_path):
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    match = re.search(r"<<'PY_VERIFIER'\n(.*?)^          PY_VERIFIER$", workflow, re.MULTILINE | re.DOTALL)
    assert match is not None
    source = textwrap.dedent(match.group(1))
    assert source == (ROOT / "scripts/publish_verifier.py").read_text()
    for node in ast.walk(ast.parse(source)):
        names = [item.name for item in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
        assert all(name.split(".")[0] in sys.stdlib_module_names for name in names)
    script = tmp_path / "trusted-verifier.py"
    script.write_text(source)
    result = subprocess.run(
        [sys.executable, "-I", str(script)], capture_output=True, text=True,
        env={**os.environ, "BUNDLE": str(bundle_path), "VERSION": VERSION, "EXPECTED_COMMIT": COMMIT},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "prerelease\n"


def test_release_keeps_single_build_native_coverage_and_publish_permission_boundary():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    build, native, publish = re.split(r"^  (?:native_validation|publish):\n", workflow, flags=re.MULTILINE)
    assert workflow.count("python -m build") == 1
    assert "contents: write" not in build + native
    assert "contents: write" in publish
    assert "actions/checkout@" not in publish
    assert "needs: [build, native_validation]" in publish
    assert 'python3 -I "$RUNNER_TEMP/trusted-publish-verifier.py"' in publish
    assert "ref: ${{ needs.build.outputs.commit_sha }}" in native
    for job in (native, publish):
        assert "artifact-ids: ${{ needs.build.outputs.artifact_id }}" in job
        assert "digest-mismatch: error" in job
    for job in (native, (ROOT / ".github/workflows/tests.yml").read_text()):
        assert "os: [ubuntu-24.04, macos-latest]" in job
        assert 'python: ["3.11", "3.x"]' in job
        assert "python -m pytest tests/unit tests/integration" in job
        assert "python -m pytest tests/browser" in job
        assert "chromium webkit" in job
        assert "python -m pipx install" in job
        assert "--ignore=" not in job
    assert "UPDATE_MANIFEST" not in workflow
