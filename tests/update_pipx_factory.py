"""Real offline pipx experiments using production snapshot and journal replay.

These fixtures initialize independent real environments, never a probe shadow
or a copy of the developer's application. Faults run in pinned pipx itself.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import textwrap
import time
import zipfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from arxiv_digest.update_manifest import inspect_update_wheel
from arxiv_digest.update_pipx import CommandRequest, CommandResult, run_bounded_command
from tests.update_wheel_factory import default_metadata, default_wheel_metadata, record_digest, regular_zip_info


def inventory(root: Path) -> dict[str, tuple]:
    """Content, permissions and symlink targets, without following directory links."""
    result = {}
    for path in sorted(root.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            value = ("link", mode, os.readlink(path))
        elif path.is_file():
            value = ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            assert path.is_dir()
            value = ("directory", mode)
        result[path.relative_to(root).as_posix()] = value
    return result


def _wheel(root: Path, name: str, version: str, *, app: bool = False, missing_app: bool = False) -> Path:
    normalized = name.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    module = "bs4" if name == "beautifulsoup4" else normalized
    members = {
        f"{module}/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{dist_info}/METADATA": default_metadata(version) if app else (
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nRequires-Python: >=3.11\n"
            + ("Requires-Dist: synthetic-transitive==1.0\n" if name == "beautifulsoup4" else "") + "\n"
        ).encode(),
        f"{dist_info}/WHEEL": default_wheel_metadata(),
    }
    if app:
        members[f"{module}/cli.py"] = (
            "import json, sys\nfrom importlib.metadata import version\n"
            "def main():\n"
            "    print(json.dumps({'version': version('arxiv-digest'), "
            "'python': sys.executable, 'dependency': version('beautifulsoup4')}))\n"
        ).encode()
        if not missing_app:
            members[f"{dist_info}/entry_points.txt"] = b"[console_scripts]\narxiv-digest = arxiv_digest.cli:main\n"
    elif name == "unrelated-tool":
        members[f"{module}/cli.py"] = b"def main():\n    print('unrelated synthetic command')\n"
        members[f"{dist_info}/entry_points.txt"] = b"[console_scripts]\nunrelated-tool = unrelated_tool.cli:main\n"
    rows = io.StringIO(newline="")
    writer = csv.writer(rows, lineterminator="\n")
    for path, payload in members.items():
        writer.writerow((path, record_digest(payload), len(payload)))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    members[f"{dist_info}/RECORD"] = rows.getvalue().encode()
    path = root / f"{normalized}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for member, payload in members.items():
            archive.writestr(regular_zip_info(member), payload)
    if app:
        inspect_update_wheel(path, expected_version=version)
    return path


# A controlled test-only sitecustomize audits pinned pipx. Its loader in the
# temporary environment site-packages also reaches pip children: pipx deliberately
# removes PYTHONPATH from their environment. Installed pipx is never edited.
_SITECUSTOMIZE = r'''
import json, os, pathlib, shutil, signal, sys

root = pathlib.Path(__file__).resolve().parent.parent
venv = root / 'pipx/venvs/arxiv-digest'
exposed = root / 'bin/arxiv-digest'
config = json.loads((root / 'fault.json').read_text())
(root / 'hook-processes' / str(os.getpid())).write_text(json.dumps({
    'executable': sys.executable, 'argv': sys.orig_argv,
}))

def audit(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname'):
        with (root / 'network-attempts').open('a') as stream:
            stream.write(event + '\n')
        raise RuntimeError('network disabled in pipx capability experiment')
    path = None
    if event == 'open' and isinstance(args[0], (str, bytes)):
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            path = args[0]
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime'):
        path = args[0]
    elif event in ('os.rename', 'os.symlink', 'os.link'):
        path = args[1]
    if isinstance(path, (str, bytes)):
        candidate = pathlib.Path(os.fsdecode(path))
        # shutil.rmtree removes relative to an opened fixture directory.
        if not candidate.is_absolute() and event != 'open':
            return
        candidate = candidate.absolute()
        if candidate == pathlib.Path(os.devnull):
            return
        if not candidate.is_relative_to(root):
            raise RuntimeError('write outside temporary pipx capability root')

sys.addaudithook(audit)
original_rename = pathlib.Path.rename
original_unlink = pathlib.Path.unlink
original_rmtree = shutil.rmtree

def pause(phase):
    if config.get('phase') != phase:
        return
    # TERM refusal requires supervisor escalation instead of pipx cleanup.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    marker = root / 'phase.partial'
    marker.write_text(json.dumps({'phase': phase, 'pid': os.getpid(), 'pgid': os.getpgrp()}))
    marker.rename(root / 'phase.json')
    while True:
        signal.pause()

def rename(self, target):
    result = original_rename(self, target)
    if self == venv:
        pause('environment_renamed')
    return result

def unlink(self, *args, **kwargs):
    result = original_unlink(self, *args, **kwargs)
    if self == exposed:
        pause('exposed_link_unlinked')
    return result

def rmtree(path, *args, **kwargs):
    if config.get('phase') == 'environment_renamed' and pathlib.Path(path) == venv:
        # Simulate a directory deletion failure; real pipx util.rmdir then
        # performs its destructive fallback rename into its own trash.
        return None
    return original_rmtree(path, *args, **kwargs)

pathlib.Path.rename = rename
pathlib.Path.unlink = unlink
shutil.rmtree = rmtree
'''


def assert_process_group_dead(pgid: int) -> None:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return
    raise AssertionError("installer process group is still alive; recovery refused")


def require_empty_safe_trash(path: Path) -> None:
    """Test-owned admission rule; the future helper must integrate/revalidate it.

    pipx 1.16.7 unconditionally clears trash even with skipped maintenance.
    Refuse populated/unsafe trash; never relocate or erase it.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
        or info.st_mode & 0o022 or any(path.iterdir())
    ):
        raise ValueError("pipx trash is populated or unsafe")


@dataclass
class InstallationSnapshot:
    __test__ = False
    path: Path
    token: dict
    journal_store: object
    journal_snapshot: object

    @classmethod
    def capture(cls, fixture: "RealPipx") -> "InstallationSnapshot":
        from arxiv_digest.update_snapshot import create_snapshot
        from tests.update_protocol_factory import ATTEMPT, admit_journal, next_record

        root = fixture.root / "pipx/arxiv-digest-update-snapshots"
        root.mkdir(mode=0o700)
        snapshot = root / ATTEMPT
        external = {
            path.relative_to(fixture.venv).as_posix(): os.readlink(path)
            for path in (fixture.venv / "bin").iterdir()
            if path.is_symlink() and path.name.startswith("python")
            and path.resolve() == fixture.source_python.resolve()
        }
        token = create_snapshot(fixture.venv, snapshot, fixture.exposed,
                                fixture.source_python.resolve(), allowed_external_symlinks=external)
        store, current = admit_journal(fixture.root / "recovery", old_token=token)
        current = store.transition(current, next_record(current, "committed"))
        return cls(snapshot, token, store, current)

    def restore(self, fixture: "RealPipx", *, pgid: int) -> None:
        from arxiv_digest.update_runtime import protocol, recovery
        from tests.update_protocol_factory import next_record

        assert_process_group_dead(pgid)
        core = self.token["core"]
        replay = {
            "snapshot_path": str(self.path), "live_path": str(fixture.venv),
            "forensic_path": str(self.path.with_name(self.path.name + ".failed")),
            "old_token": core,
            "partial_token": recovery.capture_partial_environment(fixture.venv, allowed_external_symlinks=recovery._external_links(core)),
            "partial_exposed": recovery.capture_exposed_link(fixture.exposed, allow_missing=True),
            "partial_provenance": None, "prior_provenance": None,
            "target_started": False, "process_group_ids": [pgid], "processes_dead": True,
        }
        current = self.journal_snapshot
        installer = {"guard_pid": os.getpid(), "process_group_id": pgid}
        current = self.journal_store.transition(
            current, next_record(current, "installing", installer=installer),
            authorization=protocol.InstallerAuthorization(current.sha256, current.record["attempt_id"], installer),
        )
        authorization = protocol.RecoveryAuthorization(current.sha256, current.record["attempt_id"], replay)
        self.journal_snapshot = self.journal_store.transition(
            current, next_record(current, "rolling_back", subphase="package_restore_pending", replay=replay),
            authorization=authorization,
        )
        def proved_dead(groups):
            for group in groups:
                assert_process_group_dead(group)
            return True
        recovery.replay_snapshot(journal_store=self.journal_store,
                                 journal_snapshot=self.journal_snapshot, process_death=proved_dead)
        assert recovery.scan_environment(fixture.venv, allowed_external_symlinks=recovery._external_links(core)) == core["inventory"]
        assert os.readlink(fixture.exposed) == core["exposed_link"]["target"]


@dataclass
class RealPipx:
    root: Path
    environ: dict[str, str]
    pipx: Path
    source_python: Path
    old: Path
    target: Path
    missing_app: Path

    @property
    def venv(self) -> Path:
        return self.root / "pipx/venvs/arxiv-digest"

    @property
    def exposed(self) -> Path:
        return self.root / "bin/arxiv-digest"

    @property
    def trash(self) -> Path:
        return self.root / "pipx/.trash"

    def request(self, argv: tuple[str, ...], *, timeout: float = 60) -> CommandRequest:
        return CommandRequest(argv, self.environ, self.root, time.monotonic() + timeout, 256 * 1024, 256 * 1024)

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        result = run_bounded_command(self.request(argv))
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return result

    def install_argv(self, wheel: Path) -> tuple[str, ...]:
        return (
            str(self.pipx), "install", "--force", "--app", "arxiv-digest",
            "--python", str(self.source_python), "--fetch-python=never",
            "--skip-maintenance", "--backend=pip", "--pip-args=--no-deps --no-index", str(wheel),
        )

    def install(self, wheel: Path) -> CommandResult:
        require_empty_safe_trash(self.trash)
        return run_bounded_command(self.request(self.install_argv(wheel)))

    def facts(self) -> dict:
        result = self.run((str(self.venv / "bin/python"), "-c", (
            "import json, sys; from importlib import metadata; "
            "print(json.dumps({'executable': sys.executable, 'base_executable': sys._base_executable, "
            "'prefix': sys.prefix, 'version': sys.version, 'distributions': sorted("
            "(d.metadata['Name'], d.version) for d in metadata.distributions())}))"
        )))
        return json.loads(result.stdout)

    def assert_offline(self) -> None:
        assert not (self.root / "network-attempts").exists()

    def nonapplication_files(self) -> dict:
        result = self.run((str(self.venv / "bin/python"), "-c", (
            "import json; from importlib import metadata; "
            "print(json.dumps({d.metadata['Name']: [str(d.locate_file(f)) for f in d.files or [] "
            "if '__pycache__' not in f.parts] for d in metadata.distributions() "
            "if d.metadata['Name'] != 'arxiv-digest'}))"
        )))
        values = {}
        for name, files in json.loads(result.stdout).items():
            values[name] = {}
            for filename in files:
                path = Path(filename)
                # Resolve dot segments while retaining final symlink identity.
                path = path.parent.resolve() / path.name
                relative = path.relative_to(self.root).as_posix()
                mode = stat.S_IMODE(path.lstat().st_mode)
                values[name][relative] = (
                    ("link", mode, os.readlink(path)) if path.is_symlink()
                    else ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
                )
        return values

    def sentinels(self) -> dict:
        return {name: inventory(self.root / name) for name in (
            "pipx/venvs/unrelated-tool", "unrelated-bin", "unrelated-pipx", "shared",
        )} | {"unrelated-link": os.readlink(self.root / "bin/unrelated-tool")}


def real_pipx_installation(root: Path) -> RealPipx:
    assert metadata.version("pipx") == "1.16.7"
    root = root.resolve()
    for name in (
        "home", "pipx", "bin", "man", "completion", "shared", "tmp", "wheelhouse", "hooks",
        "xdg-data", "xdg-cache", "xdg-config", "xdg-state", "xdg-runtime", "unrelated-bin", "unrelated-pipx/.trash",
        "hook-processes",
    ):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "fault.json").write_text("{}")
    (root / "hooks/sitecustomize.py").write_text(
        "import sys\nif not getattr(sys, '_arxiv_digest_test_hook', False):\n"
        "    sys._arxiv_digest_test_hook = True\n" + textwrap.indent(_SITECUSTOMIZE, "    ")
    )
    # Allowlist excludes inherited Python/pip/pipx/uv/conda settings, shell
    # configuration, proxies and credentials. PYTHONPATH is test instrumentation.
    environ = {
        "PATH": os.pathsep.join((str(root / "bin"), str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "HOME": str(root / "home"), "PIPX_HOME": str(root / "pipx"),
        "PIPX_BIN_DIR": str(root / "bin"), "PIPX_MAN_DIR": str(root / "man"),
        "PIPX_COMPLETION_DIR": str(root / "completion"), "PIPX_SHARED_LIBS": str(root / "shared"),
        "PIPX_DEFAULT_PYTHON": str(Path(sys._base_executable)),
        "PIPX_DEFAULT_BACKEND": "pip", "PIPX_FETCH_PYTHON": "never", "PIPX_USE_EMOJI": "0",
        "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1", "PIPX_MAX_LOGS": "100",
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_CACHE_DIR": str(root / "xdg-cache"),
        "PIP_NO_COMPILE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(root / "hooks"), "TMPDIR": str(root / "tmp"),
        "XDG_DATA_HOME": str(root / "xdg-data"), "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"), "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_RUNTIME_DIR": str(root / "xdg-runtime"),
        "LANG": "C", "LC_ALL": "C", "TERM": "dumb", "NO_COLOR": "1",
    }
    wheelhouse = root / "wheelhouse"
    old = _wheel(wheelhouse, "arxiv-digest", "0.3.0", app=True)
    target = _wheel(wheelhouse, "arxiv-digest", "0.3.1", app=True)
    missing = _wheel(wheelhouse, "arxiv-digest", "0.3.2", app=True, missing_app=True)
    _wheel(wheelhouse, "beautifulsoup4", "4.14.0")
    _wheel(wheelhouse, "packaging", "26.0")
    _wheel(wheelhouse, "synthetic-transitive", "1.0")
    unrelated = _wheel(wheelhouse, "unrelated-tool", "1.0")
    fixture = RealPipx(root, environ, Path(sys.executable).with_name("pipx"), Path(sys._base_executable), old, target, missing)
    # Shared pip comes from bundled ensurepip, with no developer packages/state.
    fixture.run((str(fixture.source_python), "-m", "venv", str(root / "shared")))
    shared_site = next((root / "shared/lib").glob("python*/site-packages"))
    hook_loader = f"import runpy; runpy.run_path({str(root / 'hooks/sitecustomize.py')!r})\n"
    (shared_site / "arxiv_digest_test_hooks.pth").write_text(hook_loader)
    fixture.run((str(fixture.pipx), "install", "--python", str(fixture.source_python),
                 "--fetch-python=never", "--skip-maintenance", "--backend=pip",
                 f"--pip-args=--no-index --find-links={wheelhouse}", str(old)))
    app_site = next((fixture.venv / "lib").glob("python*/site-packages"))
    (app_site / "arxiv_digest_test_hooks.pth").write_text(hook_loader)
    fixture.run((str(fixture.pipx), "install", "--python", str(fixture.source_python),
                 "--fetch-python=never", "--skip-maintenance", "--backend=pip",
                 "--pip-args=--no-deps --no-index", str(unrelated)))
    (root / "unrelated-pipx/.trash/sentinel").write_bytes(b"preserve unrelated pipx trash")
    (root / "unrelated-bin/sentinel").write_bytes(b"preserve unrelated commands")
    assert fixture.venv.is_dir() and not fixture.venv.is_symlink()
    assert not fixture.venv.parent.is_symlink()
    fixture.assert_offline()
    return fixture
