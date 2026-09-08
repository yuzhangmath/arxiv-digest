from __future__ import annotations

from pathlib import Path
import json

import pytest

from tests.update_installation_factory import synthetic_pipx_installation


def _detect(fixture, **overrides):
    from arxiv_digest.update_installation import detect_pipx_installation

    arguments = dict(
        current_version="0.3.0", running_command=fixture.exposed_command,
        running_interpreter=fixture.interpreter, running_module=fixture.module_path,
        environ=fixture.environ, paths=fixture.paths, maintenance=fixture.maintenance,
        run_command=fixture.run, inspect_backup_source=fixture.inspect_backup_source,
    )
    arguments.update(overrides)
    return detect_pipx_installation(**arguments)


def test_exact_per_user_pipx_installation_is_detected(tmp_path: Path) -> None:
    from arxiv_digest.update_installation import detect_pipx_installation

    fixture = synthetic_pipx_installation(tmp_path)
    detected = detect_pipx_installation(
        current_version="0.3.0",
        running_command=fixture.exposed_command,
        running_interpreter=fixture.interpreter,
        running_module=fixture.module_path,
        environ=fixture.environ,
        paths=fixture.paths,
        maintenance=fixture.maintenance,
        run_command=fixture.run,
        inspect_backup_source=fixture.inspect_backup_source,
    )
    assert detected.reason is None
    assert detected.installation is not None
    assert detected.installation.version == "0.3.0"
    assert detected.installation.source_kind == "canonical_tag"
    assert detected.installation.venv == fixture.venv
    assert detected.installation.snapshot_root == fixture.pipx_home / "arxiv-digest-update-snapshots"
    assert not detected.installation.snapshot_root.exists()


def test_installed_console_script_accepts_pips_exact_space_path_wrapper(tmp_path: Path) -> None:
    fixture = synthetic_pipx_installation(tmp_path / "Application Support")
    fixture.entry_point.write_text(
        '#!/bin/sh\n' + "'''exec' " + f'"{fixture.interpreter}" "$0" "$@"\n'
        + "' '''\nfrom arxiv_digest.cli import main\nraise SystemExit(main())\n"
    )
    assert _detect(fixture).installation is not None


@pytest.mark.parametrize("flags,accepted", [(" -E", True), (" -I", False), (" -E -S", False), (" -S", False)])
def test_console_script_accepts_only_pips_exact_ignore_environment_flag(tmp_path, flags, accepted):
    fixture = synthetic_pipx_installation(tmp_path)
    text = fixture.entry_point.read_text()
    fixture.entry_point.write_text(text.replace(f"#!{fixture.interpreter}\n", f"#!{fixture.interpreter}{flags}\n"))
    assert (_detect(fixture).installation is not None) is accepted


def test_original_interpreter_alias_directory_can_differ_from_resolved_base(tmp_path: Path) -> None:
    fixture = synthetic_pipx_installation(tmp_path)
    alias = tmp_path / "alias" / "python3"
    alias.parent.mkdir(mode=0o700)
    alias.symlink_to(fixture.base_interpreter)
    metadata_path = fixture.venv / "pipx_metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["source_interpreter"]["__Path__"] = str(alias)
    metadata_path.write_text(json.dumps(metadata))
    config = fixture.venv / "pyvenv.cfg"
    config.write_text(config.read_text().replace(
        f"home = {fixture.base_interpreter.parent}", f"home = {alias.parent}",
    ))
    assert _detect(fixture).installation is not None


def test_absent_snapshot_still_requires_its_parent_on_the_environment_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    import arxiv_digest.update_installation as installation

    fixture = synthetic_pipx_installation(tmp_path)
    original = installation._capture

    def another_device(path, **kwargs):
        result = original(path, **kwargs)
        return replace(result, device=result.device + 1) if path == fixture.venv else result

    monkeypatch.setattr(installation, "_capture", another_device)
    detected = _detect(fixture)
    assert detected.reason is installation.InstallationUnavailableReason.UNSUPPORTED_LAYOUT
    assert fixture.requests == []


def test_empty_console_script_is_an_ordinary_ineligible_installation(tmp_path: Path) -> None:
    from arxiv_digest.update_installation import InstallationUnavailableReason

    fixture = synthetic_pipx_installation(tmp_path)
    fixture.entry_point.write_bytes(b"")
    assert _detect(fixture).reason is InstallationUnavailableReason.COMMAND_MISMATCH


@pytest.mark.parametrize("platform", ["win32", "freebsd", "linux-extra"])
def test_unsupported_platform_is_rejected_without_probing(tmp_path: Path, platform: str) -> None:
    from arxiv_digest.update_installation import InstallationUnavailableReason
    fixture = synthetic_pipx_installation(tmp_path)
    assert _detect(fixture, platform=platform).reason is InstallationUnavailableReason.UNSUPPORTED_PLATFORM
    assert fixture.requests == []


def test_pre_bootstrap_version_remains_manual_without_probing(tmp_path: Path) -> None:
    from arxiv_digest.update_installation import InstallationUnavailableReason
    fixture = synthetic_pipx_installation(tmp_path)
    assert _detect(fixture, current_version="0.2.1").reason is InstallationUnavailableReason.UNSUPPORTED_VERSION
    assert fixture.requests == []


def test_resolved_desktop_entry_corresponds_to_the_exposed_pipx_link(tmp_path: Path) -> None:
    fixture = synthetic_pipx_installation(tmp_path)
    assert _detect(fixture, running_command=fixture.entry_point).installation is not None


@pytest.mark.parametrize("member", ["pipx_executable", "venv", "exposed_parent", "distribution", "metadata", "script"])
def test_unsafe_installation_permissions_are_never_repaired(tmp_path: Path, member: str) -> None:
    fixture = synthetic_pipx_installation(tmp_path)
    path = {
        "pipx_executable": fixture.pipx_executable, "venv": fixture.venv,
        "exposed_parent": fixture.exposed_command.parent, "distribution": fixture.distribution,
        "metadata": fixture.distribution / "METADATA", "script": fixture.entry_point,
    }[member]
    path.chmod(0o777 if path.is_dir() else 0o666)
    before = path.stat().st_mode
    assert _detect(fixture).installation is None
    assert path.stat().st_mode == before
    assert fixture.requests == []


@pytest.mark.parametrize("replacement", ["regular", "wrong_target"])
def test_exposed_command_requires_the_exact_owned_symlink(tmp_path: Path, replacement: str) -> None:
    fixture = synthetic_pipx_installation(tmp_path)
    fixture.exposed_command.unlink()
    if replacement == "regular":
        fixture.exposed_command.write_bytes(fixture.entry_point.read_bytes())
        fixture.exposed_command.chmod(0o700)
    else:
        fixture.exposed_command.symlink_to(fixture.base_interpreter)
    assert _detect(fixture).installation is None
    assert fixture.requests == []


@pytest.mark.parametrize("change", ["editable", "extra", "wrong_tag", "wrong_commit", "wrong_url", "duplicate"])
def test_only_the_closed_canonical_tag_provenance_is_accepted(tmp_path: Path, change: str) -> None:
    fixture = synthetic_pipx_installation(tmp_path)
    path = fixture.distribution / "direct_url.json"
    data = json.loads(path.read_bytes())
    if change == "editable":
        data = {"url": fixture.root.as_uri(), "dir_info": {"editable": True}}
    elif change == "extra":
        data["subdirectory"] = "src"
    elif change == "wrong_tag":
        data["vcs_info"]["requested_revision"] = "v0.2.1"
    elif change == "wrong_commit":
        data["vcs_info"]["commit_id"] = "A" * 40
    elif change == "wrong_url":
        data["url"] = "https://example.invalid/project.git"
    path.write_text(json.dumps(data))
    if change == "duplicate":
        path.write_text(path.read_text().replace('"url":', '"url":"duplicate","url":', 1))
    assert _detect(fixture).installation is None


def test_archive_source_requires_the_future_authoritative_provenance_validator(tmp_path: Path) -> None:
    from arxiv_digest.update_installation import InstallationUnavailableReason

    fixture = synthetic_pipx_installation(tmp_path)
    source = fixture.paths.update_recovery_dir / "arxiv_digest-0.3.0-py3-none-any.whl"
    path = fixture.distribution / "direct_url.json"
    path.write_text(json.dumps({"url": source.as_uri(), "archive_info": {"hash": "sha256=" + "a" * 64}}))
    metadata = fixture.venv / "pipx_metadata.json"
    data = json.loads(metadata.read_bytes())
    data["main_package"]["package_or_url"] = str(source)
    metadata.write_text(json.dumps(data))
    assert _detect(fixture).reason is InstallationUnavailableReason.PROTECTED_PROVENANCE_REQUIRED
    assert not fixture.paths.update_provenance_path.exists()


@pytest.mark.parametrize("change", ["version", "entrypoint", "module", "duplicate_dist", "symlink_metadata", "system_site"])
def test_local_distribution_and_interpreter_must_correspond(tmp_path: Path, change: str) -> None:
    fixture = synthetic_pipx_installation(tmp_path)
    overrides = {}
    if change == "version":
        path = fixture.distribution / "METADATA"
        path.write_text(path.read_text().replace("Version: 0.3.0", "Version: 0.2.1"))
    elif change == "entrypoint":
        (fixture.distribution / "entry_points.txt").write_text("[console_scripts]\narxiv-digest = unrelated:main\n")
    elif change == "module":
        overrides["running_module"] = fixture.root / "editable" / "__init__.py"
    elif change == "duplicate_dist":
        (fixture.distribution.parent / "arxiv_digest-0.2.1.dist-info").mkdir()
    elif change == "symlink_metadata":
        path = fixture.distribution / "METADATA"
        target = fixture.root / "metadata-copy"
        path.rename(target)
        path.symlink_to(target)
    else:
        path = fixture.venv / "pyvenv.cfg"
        path.write_text(path.read_text().replace("packages = false", "packages = true"))
    assert _detect(fixture, **overrides).installation is None
    assert fixture.requests == []


@pytest.mark.parametrize("path_name", ["profile", "metadata", "entry_point"])
def test_external_change_after_probe_invalidates_the_proof(tmp_path: Path, path_name: str) -> None:
    from arxiv_digest.update_installation import InstallationUnavailableReason

    fixture = synthetic_pipx_installation(tmp_path)
    def inspect(paths, **kwargs):
        if path_name == "profile":
            raise RuntimeError("backup source changed")
        path = fixture.distribution / "METADATA" if path_name == "metadata" else fixture.entry_point
        path.write_bytes(path.read_bytes() + b"\n")
        return fixture.inspect_backup_source(paths, **kwargs)
    result = _detect(fixture, inspect_backup_source=inspect)
    expected = (
        InstallationUnavailableReason.BACKUP_UNAVAILABLE if path_name == "profile"
        else InstallationUnavailableReason.EXTERNAL_CHANGE_DETECTED
    )
    assert result.reason is expected


def test_expired_detection_starts_no_subprocess_and_returns_a_closed_reason(tmp_path: Path) -> None:
    from arxiv_digest.update_installation import InstallationUnavailableReason
    fixture = synthetic_pipx_installation(tmp_path)
    assert _detect(fixture, deadline_at=1, monotonic=lambda: 2).reason is InstallationUnavailableReason.DEADLINE_EXPIRED
    assert fixture.requests == []


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_CLOEXEC"])
def test_missing_required_file_open_flags_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    import os
    fixture = synthetic_pipx_installation(tmp_path)
    monkeypatch.delattr(os, flag)
    assert _detect(fixture).installation is None
    assert fixture.requests == []


def test_selected_pipx_symlink_is_revalidated_after_probe(tmp_path: Path) -> None:
    from arxiv_digest.update_installation import InstallationUnavailableReason

    fixture = synthetic_pipx_installation(tmp_path)
    selector = tmp_path / "tools" / "bin" / "pipx"
    selector.parent.mkdir(mode=0o700, parents=True)
    selector.symlink_to(fixture.pipx_executable)
    fixture.environ["PATH"] = str(selector.parent)
    replacement = tmp_path / "another-pipx"
    replacement.write_bytes(fixture.pipx_executable.read_bytes())
    replacement.chmod(0o700)

    def inspect(paths, **kwargs):
        selector.unlink()
        selector.symlink_to(replacement)
        return fixture.inspect_backup_source(paths, **kwargs)

    assert _detect(fixture, inspect_backup_source=inspect).reason is InstallationUnavailableReason.EXTERNAL_CHANGE_DETECTED


def test_detection_result_is_closed_and_proof_is_immutable(tmp_path: Path) -> None:
    from dataclasses import FrozenInstanceError
    from arxiv_digest.update_installation import InstallationDetection, InstallationUnavailableReason

    fixture = synthetic_pipx_installation(tmp_path)
    proof = _detect(fixture).installation
    assert proof is not None
    with pytest.raises(ValueError):
        InstallationDetection()
    with pytest.raises(ValueError):
        InstallationDetection(proof, InstallationUnavailableReason.COMMAND_MISMATCH)
    with pytest.raises(TypeError):
        InstallationDetection(reason="untrusted detail")
    with pytest.raises(FrozenInstanceError):
        proof.version = "0.3.1"
    with pytest.raises(FrozenInstanceError):
        proof.distribution.version = "0.3.1"


@pytest.mark.parametrize("authenticated_command", [None, Path("/private/tmp/verified/bin/arxiv-digest")])
def test_runtime_defers_detection_to_the_checker_with_its_own_paths_and_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authenticated_command,
) -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.profile import ProfileRepository
    from arxiv_digest.web.lifecycle import LifecycleController
    from arxiv_digest.update_installation import InstallationDetection, InstallationUnavailableReason

    fixture = synthetic_pipx_installation(tmp_path)
    calls = []

    class Checker:
        def __init__(self, *, detect_installation):
            self.detect = detect_installation

    result = InstallationDetection(reason=InstallationUnavailableReason.UNSUPPORTED_VERSION)
    monkeypatch.setattr("arxiv_digest.application.UpdateChecker", Checker)
    monkeypatch.setattr("arxiv_digest.update_installation.detect_pipx_installation",
                        lambda **kwargs: calls.append(kwargs) or result)
    runtime = _DefaultRuntime(
        fixture.paths, ProfileRepository(fixture.paths.profile_path, fixture.paths.profile_lock_path),
        fixture.maintenance, LifecycleController(), output=lambda message: None,
        update_running_command=authenticated_command,
    )
    assert calls == []
    clock = lambda: 10
    assert runtime.update_checker.detect(deadline_at=20, monotonic=clock) is result
    assert calls == [{
        "paths": fixture.paths, "maintenance": fixture.maintenance,
        "deadline_at": 20, "monotonic": clock, "running_command": authenticated_command,
    }]


@pytest.mark.parametrize("accepted", [False, True])
def test_exact_offline_metadata_is_admitted_only_after_protected_provenance(tmp_path, accepted):
    fixture = synthetic_pipx_installation(tmp_path)
    source = fixture.paths.update_recovery_dir / "arxiv_digest-0.3.0-py3-none-any.whl"
    direct_path = fixture.distribution / "direct_url.json"
    direct_path.write_text(json.dumps({"url": source.as_uri(), "archive_info": {
        "hash": "sha256=" + "a" * 64, "hashes": {"sha256": "a" * 64}}}))
    metadata = fixture.venv / "pipx_metadata.json"
    raw = json.loads(metadata.read_bytes())
    raw["main_package"].update(package_or_url=str(source), pip_args=["--no-deps", "--no-index"], expected_apps=["arxiv-digest"])
    metadata.write_text(json.dumps(raw))
    calls = []
    def validate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert fixture.requests == []
        return accepted
    result = _detect(fixture, validate_protected_provenance=validate)
    if accepted:
        assert result.installation is not None
        assert result.installation.source_kind == "updater_provenance"
        assert len(calls) == 2
    else:
        assert result.installation is None
        assert fixture.requests == []


def test_canonical_source_cannot_use_updater_metadata_even_with_accepting_validator(tmp_path):
    fixture = synthetic_pipx_installation(tmp_path)
    metadata = fixture.venv / "pipx_metadata.json"
    raw = json.loads(metadata.read_bytes())
    raw["main_package"].update(pip_args=["--no-deps", "--no-index"], expected_apps=["arxiv-digest"])
    metadata.write_text(json.dumps(raw))
    result = _detect(fixture, validate_protected_provenance=lambda **kwargs: pytest.fail("canonical source asked for wheel provenance"))
    assert result.installation is None
    assert fixture.requests == []
