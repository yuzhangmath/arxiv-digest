from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parents[1] / "fixtures/update"


def test_native_metadata_has_exact_typed_package_schema() -> None:
    from arxiv_digest.update_pipx import parse_pipx_metadata

    result = parse_pipx_metadata((FIXTURES / "pipx-1.16.7-metadata.json").read_bytes())
    assert result.environment == "arxiv-digest"
    assert result.main_package.package_version == "0.3.0"
    assert result.main_package.apps == ("arxiv-digest",)
    assert result.main_package.app_paths == (
        Path("/synthetic/pipx/venvs/arxiv-digest/bin/arxiv-digest"),
    )
    assert result.source_interpreter == Path("/synthetic/python/bin/python3")


def test_bounded_runner_preserves_exact_env_cwd_stdin_and_both_streams(tmp_path: Path) -> None:
    from arxiv_digest.update_pipx import CommandRequest, run_bounded_command

    result = run_bounded_command(CommandRequest(
        argv=(sys.executable, "-c", "import os,sys; assert sys.stdin.read()==''; print(os.environ['SYNTHETIC']); print(os.getcwd(),file=sys.stderr)"),
        environ={"SYNTHETIC": "exact"}, cwd=tmp_path,
        deadline_at=time.monotonic() + 2, stdout_limit=1024, stderr_limit=4096,
    ))
    assert result.returncode == 0
    assert result.stdout == b"exact\n"
    assert result.stderr == (str(tmp_path) + "\n").encode()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_bounded_runner_rejects_output_overflow(tmp_path: Path, stream: str) -> None:
    from arxiv_digest.update_pipx import CommandOutputLimitError, CommandRequest, run_bounded_command

    with pytest.raises(CommandOutputLimitError):
        run_bounded_command(CommandRequest(
            argv=(sys.executable, "-c", f"import sys; sys.{stream}.write('x'*8192)"),
            environ={}, cwd=tmp_path, deadline_at=time.monotonic() + 2,
            stdout_limit=100, stderr_limit=100,
        ))


def test_bounded_runner_times_out_and_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import arxiv_digest.update_pipx as pipx

    monkeypatch.setattr(pipx, "TERM_GRACE_SECONDS", 0.05)
    pid_path = tmp_path / "pid"
    with pytest.raises(pipx.CommandTimeoutError):
        pipx.run_bounded_command(pipx.CommandRequest(
            argv=(sys.executable, "-c", "import os,signal,time,pathlib; pathlib.Path('pid').write_text(str(os.getpid())); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(20)"),
            environ={}, cwd=tmp_path, deadline_at=time.monotonic() + 0.2,
            stdout_limit=100, stderr_limit=100,
        ))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


def test_shadow_probe_uses_fixed_commands_and_never_mutates_live_layout(tmp_path: Path) -> None:
    from arxiv_digest.update_pipx import probe_pipx
    from tests.update_installation_factory import synthetic_pipx_installation

    installation = synthetic_pipx_installation(tmp_path)
    trash = installation.pipx_home / ".trash"
    trash.mkdir()
    sentinel = trash / "preserve"
    sentinel.write_bytes(b"synthetic live trash")
    observed = []

    def runner(request):
        shadow = Path(request.environ["PIPX_HOME"])
        assert shadow != installation.pipx_home
        assert (shadow / "venvs/arxiv-digest").is_symlink()
        assert os.readlink(shadow / "venvs/arxiv-digest") == str(installation.venv)
        assert Path(request.environ["HOME"]).is_relative_to(request.cwd)
        assert request.environ["PIPX_FETCH_PYTHON"] == "never"
        assert request.environ["PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE"] == "1"
        assert request.environ["PIP_CONFIG_FILE"] == os.devnull
        assert "PYTHONPATH" not in request.environ
        assert "CONDA_PREFIX" not in request.environ
        assert "UV_INDEX_URL" not in request.environ
        assert "PIP_INDEX_URL" not in request.environ
        observed.append(request.cwd)
        return installation.run(request)

    result = probe_pipx(
        pipx_executable=installation.pipx_executable,
        venv=installation.venv, pipx_home=installation.pipx_home,
        exposed_command=installation.exposed_command,
        environ={**installation.environ, "PYTHONPATH": "/bad", "CONDA_PREFIX": "/bad", "UV_INDEX_URL": "bad", "PIP_INDEX_URL": "bad"},
        deadline_at=time.monotonic() + 2, run_command=runner,
    )
    assert result.metadata.main_package.package_version == "0.3.0"
    assert len(installation.requests) == 4
    assert sentinel.read_bytes() == b"synthetic live trash"
    assert all(not shadow.exists() for shadow in observed)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(unknown=True),
    lambda value: value.update(pipx_metadata_version="0.11"),
    lambda value: value.update(backend="uv"),
    lambda value: value.update(exposure_enabled=1),
    lambda value: value.update(venv_args=["--system-site-packages"]),
    lambda value: value["main_package"].update(pinned=True),
    lambda value: value["main_package"].update(include_dependencies=0),
    lambda value: value["main_package"].update(pip_args=["--editable"]),
    lambda value: value["main_package"].pop("completion_paths"),
    lambda value: value.update(source_interpreter={"__type__": "Path", "__Path__": "relative"}),
])
def test_invalid_native_metadata_is_rejected_before_any_command(tmp_path: Path, mutation) -> None:
    from arxiv_digest.update_pipx import PipxProbeError, probe_pipx
    from tests.update_installation_factory import synthetic_pipx_installation

    installation = synthetic_pipx_installation(tmp_path)
    path = installation.venv / "pipx_metadata.json"
    value = json.loads(path.read_bytes())
    mutation(value)
    path.write_text(json.dumps(value))
    with pytest.raises(PipxProbeError):
        probe_pipx(
            pipx_executable=installation.pipx_executable,
            venv=installation.venv, pipx_home=installation.pipx_home,
            exposed_command=installation.exposed_command,
            environ=installation.environ, deadline_at=time.monotonic() + 2,
            run_command=installation.run,
        )
    assert not installation.requests


@pytest.mark.parametrize("invalid_output,expected_count", [
    ("version", 1), ("list-help", 2), ("install-help", 3), ("list-mismatch", 4),
])
def test_probe_stops_at_first_unsupported_command_contract(
    tmp_path: Path, invalid_output: str, expected_count: int,
) -> None:
    from arxiv_digest.update_pipx import CommandResult, PipxProbeError, probe_pipx
    from tests.update_installation_factory import synthetic_pipx_installation

    installation = synthetic_pipx_installation(tmp_path)

    def command(request):
        result = installation.run(request)
        if len(installation.requests) != expected_count:
            return result
        if invalid_output == "version":
            return CommandResult(0, b"1.16.6\n", b"")
        if invalid_output.endswith("help"):
            return CommandResult(0, b"usage: incompatible", b"")
        data = json.loads(result.stdout)
        data["venvs"]["arxiv-digest"]["metadata"]["main_package"]["package_version"] = "0.4.0"
        return CommandResult(0, json.dumps(data).encode(), b"")

    with pytest.raises(PipxProbeError):
        probe_pipx(
            pipx_executable=installation.pipx_executable,
            venv=installation.venv, pipx_home=installation.pipx_home,
            exposed_command=installation.exposed_command,
            environ=installation.environ, deadline_at=time.monotonic() + 2,
            run_command=command,
        )
    assert len(installation.requests) == expected_count


@pytest.mark.parametrize("payload", [
    b'{"environment":null,"environment":null}', b'{"a":NaN}',
    b'{"a":Infinity}', b'\xff', b'[]', b'x' * (128 * 1024 + 1),
])
def test_metadata_rejects_duplicate_nonfinite_invalid_and_oversized_json(payload: bytes) -> None:
    from arxiv_digest.update_pipx import PipxProbeError, parse_pipx_metadata

    with pytest.raises(PipxProbeError):
        parse_pipx_metadata(payload)


@pytest.mark.parametrize("field", tuple(json.loads((FIXTURES / "pipx-1.16.7-metadata.json").read_bytes())["main_package"]))
def test_every_native_package_field_is_required(field: str) -> None:
    from arxiv_digest.update_pipx import PipxProbeError, parse_pipx_metadata

    value = json.loads((FIXTURES / "pipx-1.16.7-metadata.json").read_bytes())
    del value["main_package"][field]
    with pytest.raises(PipxProbeError):
        parse_pipx_metadata(json.dumps(value).encode())


@pytest.mark.parametrize("mutation", ["extra-environment", "renamed-environment", "unknown-wrapper", "spec-version"])
def test_named_list_projection_is_closed(mutation: str) -> None:
    from arxiv_digest.update_pipx import PipxProbeError, parse_pipx_list

    value = json.loads((FIXTURES / "pipx-1.16.7-list.json").read_bytes())
    if mutation == "extra-environment":
        value["venvs"]["other"] = value["venvs"]["arxiv-digest"]
    elif mutation == "renamed-environment":
        value["venvs"]["arxiv-digest-suffix"] = value["venvs"].pop("arxiv-digest")
    elif mutation == "unknown-wrapper":
        value["venvs"]["arxiv-digest"]["unknown"] = True
    else:
        value["pipx_spec_version"] = "0.2"
    with pytest.raises(PipxProbeError):
        parse_pipx_list(json.dumps(value).encode())


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "writable", "missing"])
def test_unsafe_native_metadata_is_rejected_before_subprocess(tmp_path: Path, unsafe: str) -> None:
    from arxiv_digest.update_pipx import PipxProbeError, probe_pipx
    from tests.update_installation_factory import synthetic_pipx_installation

    installation = synthetic_pipx_installation(tmp_path)
    path = installation.venv / "pipx_metadata.json"
    if unsafe == "symlink":
        target = tmp_path / "metadata"
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "metadata")
    elif unsafe == "writable":
        path.chmod(0o666)
    else:
        path.unlink()
    with pytest.raises(PipxProbeError):
        probe_pipx(
            pipx_executable=installation.pipx_executable,
            venv=installation.venv, pipx_home=installation.pipx_home,
            exposed_command=installation.exposed_command,
            environ=installation.environ, deadline_at=time.monotonic() + 2,
            run_command=installation.run,
        )
    assert not installation.requests


@pytest.mark.parametrize("replacement", ["metadata", "executable", "entry-point", "exposed-link"])
def test_probe_rejects_sensitive_file_changes_after_each_command(tmp_path: Path, replacement: str) -> None:
    from arxiv_digest.update_pipx import PipxProbeError, probe_pipx
    from tests.update_installation_factory import synthetic_pipx_installation

    installation = synthetic_pipx_installation(tmp_path)
    shadows = []

    def command(request):
        shadows.append(request.cwd)
        result = installation.run(request)
        target = {
            "metadata": installation.venv / "pipx_metadata.json",
            "executable": installation.pipx_executable,
            "entry-point": installation.entry_point,
            "exposed-link": installation.exposed_command,
        }[replacement]
        if target.is_symlink():
            target.unlink()
            target.symlink_to(installation.base_interpreter)
        else:
            target.write_bytes(target.read_bytes() + b" ")
        return result

    with pytest.raises(PipxProbeError, match="changed"):
        probe_pipx(
            pipx_executable=installation.pipx_executable,
            venv=installation.venv, pipx_home=installation.pipx_home,
            exposed_command=installation.exposed_command,
            environ=installation.environ, deadline_at=time.monotonic() + 2,
            run_command=command,
        )
    assert len(installation.requests) == 1
    assert all(not shadow.exists() for shadow in shadows)


@pytest.mark.parametrize("replacement", ["link", "root", "unexpected-file"])
def test_shadow_cleanup_refuses_replacements_and_preserves_other_data(tmp_path: Path, replacement: str) -> None:
    from arxiv_digest.update_pipx import PipxProbeError, probe_pipx
    from tests.update_installation_factory import synthetic_pipx_installation

    installation = synthetic_pipx_installation(tmp_path)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"preserve")
    roots = []

    def command(request):
        roots.append(request.cwd)
        result = installation.run(request)
        if replacement == "link":
            link = Path(request.environ["PIPX_HOME"]) / "venvs/arxiv-digest"
            link.unlink()
            link.symlink_to(tmp_path)
        elif replacement == "root":
            original = request.cwd.with_name(request.cwd.name + "-original")
            roots.append(original)
            request.cwd.rename(original)
            request.cwd.mkdir(mode=0o700)
            (request.cwd / "preserve").write_bytes(b"replacement")
        else:
            (request.cwd / "unexpected").write_bytes(b"preserve unexpected data")
        return result

    try:
        with pytest.raises(PipxProbeError, match="shadow"):
            probe_pipx(
                pipx_executable=installation.pipx_executable,
                venv=installation.venv, pipx_home=installation.pipx_home,
                exposed_command=installation.exposed_command,
                environ=installation.environ, deadline_at=time.monotonic() + 2,
                run_command=command,
            )
        assert sentinel.read_bytes() == b"preserve"
        assert roots and all(root.exists() for root in roots)
        if replacement == "root":
            assert (roots[0] / "preserve").read_bytes() == b"replacement"
    finally:
        # Only these exclusively synthetic roots belong to the test.
        for root in roots:
            shutil.rmtree(root)


def test_runner_closes_inheritable_descriptors_and_creates_a_new_session(tmp_path: Path) -> None:
    from arxiv_digest.update_pipx import CommandRequest, CommandResult, run_bounded_command

    descriptor = os.open(tmp_path / "not-inherited", os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(descriptor, True)
    try:
        result = run_bounded_command(CommandRequest(
            argv=(sys.executable, "-c", "import os,sys; assert os.getpid()==os.getsid(0)==os.getpgrp();\ntry: os.fstat(int(sys.argv[1]))\nexcept OSError: print('closed')\nelse: raise AssertionError('descriptor leaked')", str(descriptor)),
            environ={}, cwd=tmp_path, deadline_at=time.monotonic() + 2,
            stdout_limit=100, stderr_limit=1000,
        ))
    finally:
        os.close(descriptor)
    assert result == CommandResult(0, b"closed\n", b"")


def test_expired_runner_deadline_does_not_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import arxiv_digest.update_pipx as pipx

    monkeypatch.setattr(pipx.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("spawned after deadline"))
    with pytest.raises(pipx.CommandTimeoutError):
        pipx.run_bounded_command(pipx.CommandRequest(
            argv=(sys.executable, "-c", "pass"), environ={}, cwd=tmp_path,
            deadline_at=time.monotonic() - 1, stdout_limit=10, stderr_limit=10,
        ))


def test_runner_terminates_descendants_that_hold_output_pipes_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.update_pipx as pipx

    monkeypatch.setattr(pipx, "TERM_GRACE_SECONDS", 0.05)
    script = (
        "import os,signal,sys,time\n"
        "pid=os.fork()\n"
        "if pid == 0:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        " with open('heartbeat','w') as handle:\n"
        "  while True:\n"
        "   handle.write('x'); handle.flush(); time.sleep(0.01)\n"
        "else:\n"
        " with open('child-pid','w') as handle: handle.write(str(pid))\n"
        " sys.exit(0)\n"
    )
    with pytest.raises(pipx.CommandTimeoutError):
        pipx.run_bounded_command(pipx.CommandRequest(
            argv=(sys.executable, "-c", script), environ={}, cwd=tmp_path,
            deadline_at=time.monotonic() + 0.2, stdout_limit=100, stderr_limit=100,
        ))
    heartbeat = tmp_path / "heartbeat"
    assert heartbeat.exists()
    before = heartbeat.read_bytes()
    time.sleep(0.05)
    assert heartbeat.read_bytes() == before


def test_runner_rejects_and_stops_descendants_after_leader_and_pipes_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import arxiv_digest.update_pipx as pipx

    monkeypatch.setattr(pipx, "TERM_GRACE_SECONDS", 0.05)
    script = (
        "import os,signal,sys,time\n"
        "pid=os.fork()\n"
        "if pid == 0:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        " descriptor=os.open(os.devnull,os.O_WRONLY)\n"
        " os.dup2(descriptor,1); os.dup2(descriptor,2); os.close(descriptor)\n"
        " with open('heartbeat','w') as handle:\n"
        "  while True:\n"
        "   handle.write('x'); handle.flush(); time.sleep(0.01)\n"
        "else:\n"
        " while not os.path.exists('heartbeat'): time.sleep(0.005)\n"
        " sys.exit(0)\n"
    )
    with pytest.raises(pipx.CommandProcessGroupError):
        pipx.run_bounded_command(pipx.CommandRequest(
            argv=(sys.executable, "-c", script), environ={}, cwd=tmp_path,
            deadline_at=time.monotonic() + 2, stdout_limit=100, stderr_limit=100,
        ))
    before = (tmp_path / "heartbeat").read_bytes()
    time.sleep(0.05)
    assert (tmp_path / "heartbeat").read_bytes() == before


@pytest.mark.parametrize("arguments", [["--no-deps", "--no-index"], ["--no-index", "--no-deps"], ["--no-deps"], ["--no-deps", "--no-index", "--upgrade"]])
def test_updater_pip_arguments_require_protected_provenance_and_exact_order(tmp_path, arguments):
    from arxiv_digest.update_pipx import PipxProbeError, parse_pipx_metadata
    from tests.update_installation_factory import synthetic_pipx_installation
    fixture = synthetic_pipx_installation(tmp_path)
    raw = json.loads((fixture.venv / "pipx_metadata.json").read_bytes())
    raw["main_package"]["pip_args"] = arguments
    raw["main_package"]["expected_apps"] = ["arxiv-digest"]
    payload = json.dumps(raw).encode()
    with pytest.raises(PipxProbeError):
        parse_pipx_metadata(payload)
    if arguments == ["--no-deps", "--no-index"]:
        parsed = parse_pipx_metadata(payload, updater_provenance=True)
        assert parsed.main_package.pip_args == ("--no-deps", "--no-index")
    else:
        with pytest.raises(PipxProbeError):
            parse_pipx_metadata(payload, updater_provenance=True)
