from __future__ import annotations

import ast
import copy
import json
import re
import sys
import textwrap
import types
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts import publish_verifier, release_bundle
from tests.unit.test_release_bundle import COMMIT, VERSION, _github_release_payload, valid_bundle


ROOT = Path(__file__).resolve().parents[2]


def embedded_verifier():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    match = re.search(r"<<'PY_VERIFIER'\n(.*?)^          PY_VERIFIER$", workflow, re.MULTILINE | re.DOTALL)
    assert match is not None
    source = textwrap.dedent(match.group(1))
    assert source == (ROOT / "scripts/publish_verifier.py").read_text()
    module = types.ModuleType("synthetic_inline_publish_verifier")
    sys.modules[module.__name__] = module
    try:
        exec(compile(source, "trusted-workflow-inline", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module


def test_embedded_verifier_is_literal_stdlib_source_and_matches_remote_contract():
    embedded_verifier()
    source = (ROOT / "scripts/publish_verifier.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(item.name.split(".")[0] in sys.stdlib_module_names for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module.split(".")[0] in sys.stdlib_module_names
    original = ast.parse((ROOT / "scripts/release_bundle.py").read_text())
    extract = lambda body: next(node for node in body if isinstance(node, ast.FunctionDef) and node.name == "verify_github_release")
    assert ast.dump(extract(tree.body)) == ast.dump(extract(original.body))


@pytest.mark.parametrize("mutation", [
    "valid", "tag", "draft", "channel", "body", "missing", "extra", "duplicate", "renamed",
    "state", "size", "bool-size", "digest", "url", "duplicate-json-key", "nonfinite", "truncated", "array", "oversized", "tag-commit",
])
def test_actual_embedded_verifier_and_source_share_the_hostile_github_matrix(valid_bundle, mutation):
    inline = embedded_verifier()
    source_bundle = release_bundle.verify_local_bundle(valid_bundle, version=VERSION, commit=COMMIT)
    inline_bundle = inline.verify_downloaded_bundle(valid_bundle, version=VERSION, commit=COMMIT)
    assert asdict(source_bundle) == asdict(inline_bundle)
    record = json.loads(_github_release_payload(source_bundle))
    if mutation == "tag": record["tag_name"] = "v0.3.2"
    elif mutation == "draft": record["draft"] = True
    elif mutation == "channel": record["prerelease"] = False
    elif mutation == "body": record["body"] += "changed"
    elif mutation == "missing": record["assets"].pop()
    elif mutation == "extra": record["assets"].append({"name": "extra"})
    elif mutation == "duplicate": record["assets"][1] = copy.deepcopy(record["assets"][0])
    elif mutation == "renamed": record["assets"][0]["name"] = "other.whl"
    elif mutation == "state": record["assets"][0]["state"] = "new"
    elif mutation == "size": record["assets"][0]["size"] += 1
    elif mutation == "bool-size": record["assets"][0]["size"] = True
    elif mutation == "digest": record["assets"][0]["digest"] = "sha256:" + "0" * 64
    elif mutation == "url": record["assets"][0]["browser_download_url"] = "https://example.invalid/asset"
    payload = json.dumps(record).encode()
    if mutation == "duplicate-json-key": payload = b'{"draft":false,"draft":false}'
    elif mutation == "nonfinite": payload = b'NaN'
    elif mutation == "truncated": payload = b'{'
    elif mutation == "array": payload = b'[]'
    elif mutation == "oversized": payload = b" " * (inline.RELEASE_PAGE_BYTE_LIMIT + 1)
    for module, bundle in ((release_bundle, source_bundle), (inline, inline_bundle)):
        kwargs = {"bundle": bundle, "tag": f"v{VERSION}", "remote_tag_commit": "0" * 40 if mutation == "tag-commit" else COMMIT}
        if mutation == "valid":
            assert module.verify_github_release(payload, **kwargs) is None
        else:
            with pytest.raises(module.BundleError):
                module.verify_github_release(payload, **kwargs)


@pytest.mark.parametrize("mutation", ["extra", "symlink", "checksum-crlf", "commit-newline", "manifest-bool", "unsafe-wheel"])
def test_embedded_verifier_checks_downloaded_bundle_before_remote_operation(valid_bundle, mutation):
    inline = embedded_verifier()
    if mutation == "extra": (valid_bundle / "downloaded-code.py").write_text("raise Exception()")
    elif mutation == "symlink":
        (valid_bundle / "COMMIT_SHA").unlink()
        (valid_bundle / "COMMIT_SHA").symlink_to(valid_bundle / "RELEASE_NOTES.md")
    elif mutation == "checksum-crlf":
        path = valid_bundle / "SHA256SUMS"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    elif mutation == "commit-newline": (valid_bundle / "COMMIT_SHA").write_text(COMMIT)
    elif mutation == "manifest-bool":
        path = valid_bundle / "UPDATE_MANIFEST.json"
        record = json.loads(path.read_bytes())
        record["schema_version"] = True
        path.write_text(json.dumps(record))
    elif mutation == "unsafe-wheel":
        path = valid_bundle / f"arxiv_digest-{VERSION}-py3-none-any.whl"
        path.unlink()
        path.mkdir()
    with pytest.raises((inline.BundleError, OSError)):
        inline.verify_downloaded_bundle(valid_bundle, version=VERSION, commit=COMMIT)


@pytest.mark.parametrize("implementation", ["source", "embedded"])
def test_asset_hashing_stops_when_a_file_keeps_growing(tmp_path, monkeypatch, implementation):
    module = release_bundle if implementation == "source" else embedded_verifier()
    path = tmp_path / "growing.whl"
    path.write_bytes(b"small")
    fdopen = module.os.fdopen

    class GrowingReader:
        def __init__(self, descriptor):
            self.source = fdopen(descriptor, "rb")
            self.reads = 0

        def __enter__(self): return self
        def __exit__(self, *args): self.source.close()
        def fileno(self): return self.source.fileno()

        def read(self, amount):
            self.reads += 1
            assert self.reads == 1, "asset hashing exceeded the original size bound"
            return b"x" * amount

    monkeypatch.setattr(module.os, "fdopen", lambda descriptor, mode: GrowingReader(descriptor))
    with pytest.raises(module.BundleError, match="changed during verification"):
        module._asset(path)


def test_workflows_build_once_verify_manifest_and_preserve_permission_boundary():
    for name in ("tests.yml", "release.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text()
        assert workflow.count("python -m build") == 1
        assert "scripts/update_manifest.py" in workflow
        assert "scripts/release_bundle.py local" in workflow
        assert "ARXIV_DIGEST_RELEASE_ARTIFACT_DIR" in workflow
        assert "SOURCE_DATE_EPOCH" in workflow
        assert "git diff --exit-code" in workflow and "git diff --cached --exit-code" in workflow
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    build, publish = workflow.split("  publish:\n", 1)
    assert "contents: write" not in build
    assert "contents: write" in publish
    assert "actions/checkout@" not in publish
    assert '"$BUNDLE/UPDATE_MANIFEST.json"' in publish
    assert 'python3 -I "$RUNNER_TEMP/trusted-publish-verifier.py"' in publish
    assert '"refs/tags/${RELEASE_TAG}^{}"' in build


def test_publication_requires_native_matrix_for_the_exact_build_candidate():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    jobs = dict(re.findall(
        r"^  ([a-z_]+):\n(.*?)(?=^  [a-z_]+:\n|\Z)",
        workflow.split("jobs:\n", 1)[1],
        re.MULTILINE | re.DOTALL,
    ))
    validation = jobs["native_validation"]
    publish = jobs["publish"]
    assert "    needs: build\n" in validation
    assert "        os: [ubuntu-24.04, macos-latest]\n" in validation
    assert '        python: ["3.11", "3.x"]\n' in validation
    assert "      fail-fast: false\n" in validation
    assert "    runs-on: ${{ matrix.os }}\n" in validation
    assert "          python-version: ${{ matrix.python }}\n" in validation
    assert "          ref: ${{ needs.build.outputs.commit_sha }}\n" in validation
    assert "          fetch-depth: 0\n" in validation
    assert "          fetch-tags: true\n" in validation
    for job in (validation, publish):
        assert "          artifact-ids: ${{ needs.build.outputs.artifact_id }}\n" in job
        assert "          digest-mismatch: error\n" in job
        assert "continue-on-error:" not in job
    assert "    needs: [build, native_validation]\n" in publish
    assert (
        "    if: ${{ needs.build.result == 'success' && "
        "needs.native_validation.result == 'success' }}\n"
    ) in publish
    assert "exclude:" not in validation and "include:" not in validation
    assert "python -m build" not in validation
    assert "scripts/release_bundle.py local" in validation
    assert "ARXIV_DIGEST_RELEASE_ARTIFACT_DIR: ${{ runner.temp }}/release-bundle" in validation
    assert '"$head_commit" == "$EXPECTED_COMMIT"' in validation
    assert 'git rev-parse --verify "refs/tags/v0.2.1^{commit}"' in validation
    assert 'python -m pip install --disable-pip-version-check "$wheel[dev]"' in validation
    assert "python -m pytest tests/unit tests/integration -q" in validation
    assert "node --test tests/js/*.test.mjs" in validation
    assert "python -m playwright install --with-deps chromium webkit" in validation
    assert "python -m playwright install chromium webkit" in validation
    assert "python -m pytest tests/browser -q" in validation
    assert 'python -m pipx install "$wheel"' in validation
    assert "git diff --exit-code" in validation
    assert "git diff --cached --exit-code" in validation
