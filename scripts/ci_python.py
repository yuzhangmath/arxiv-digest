#!/usr/bin/env python3
"""Prepare a private native-test Python environment on ephemeral hosted runners."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


class CiPythonError(ValueError):
    pass


def _identity(path: Path, *, directory: bool = False, executable: bool = True) -> os.stat_result:
    info = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_type(info.st_mode) or info.st_uid not in {0, os.getuid()}
        or info.st_mode & 0o7000
        or (not directory and (info.st_nlink != 1 or (executable and not info.st_mode & 0o111)))
    ):
        raise CiPythonError("hosted Python has an unsupported filesystem identity")
    return info


def _report(label: str, info: os.stat_result) -> None:
    owner = "root" if info.st_uid == 0 else "current"
    print(f"{label}: owner={owner} mode={stat.S_IMODE(info.st_mode):04o} links={info.st_nlink}")


def _hosted_path(path: Path, cache: Path, *, platform: str) -> Path:
    path = path.resolve(strict=True)
    cache = cache.resolve(strict=True)
    roots = [cache]
    if platform == "darwin":
        roots.append(Path("/Library/Frameworks/Python.framework/Versions"))
    if (
        cache == Path("/") or not cache.is_dir()
        or not any(path != root and path.is_relative_to(root) for root in roots)
    ):
        raise CiPythonError("selected Python is outside the hosted toolchain")
    return path


def _activation_templates(cache: Path, *, platform: str) -> list[Path]:
    scripts = _hosted_path(Path(venv.__file__).parent / "scripts", cache, platform=platform)
    _identity(scripts, directory=True)
    # Fish moved from posix to common in newer Python. No other copied files
    # or nested directories are part of the supported activation templates.
    allowed = {
        "common": {"activate", "Activate.ps1", "activate.fish"},
        "posix": {"activate.csh", "activate.fish"},
    }
    selected = []
    for folder, names in allowed.items():
        directory = scripts / folder
        _identity(directory, directory=True)
        for template in sorted(directory.iterdir()):
            if template.name not in names:
                raise CiPythonError("hosted Python has an unsupported activation template")
            selected.append(template)
    if sorted(path.name for path in selected) != ["Activate.ps1", "activate", "activate.csh", "activate.fish"]:
        raise CiPythonError("hosted Python has an unsupported activation template layout")
    return selected


def secure_interpreter(base: Path, cache: Path, *, platform: str) -> Path:
    base = _hosted_path(base, cache, platform=platform)
    # Validate every selected identity before mutation. Nested pipx venvs use
    # the original interpreter and copy activation template modes, ignoring
    # umask, so both must meet the production updater's safety requirements.
    selected = [(base, False, True, "interpreter"), (base.parent, True, False, "interpreter parent")]
    selected.extend((path, False, False, "activation template") for path in _activation_templates(cache, platform=platform))
    before = [_identity(path, directory=directory, executable=executable) for path, directory, executable, _ in selected]
    for (path, directory, executable, label), old in zip(selected, before):
        _report(f"{label} before", old)
        if old.st_mode & 0o022:
            subprocess.run(("sudo", "chmod", "go-w", str(path)), check=True)
        current = _identity(path, directory=directory, executable=executable)
        if (
            current.st_mode & 0o022
            or (old.st_dev, old.st_ino, old.st_uid, old.st_nlink)
            != (current.st_dev, current.st_ino, current.st_uid, current.st_nlink)
        ):
            raise CiPythonError("hosted Python identity changed during preparation")
        _report(f"{label} after", current)
    return base


def create_validation_venv(base: Path, destination: Path) -> Path:
    destination.mkdir(mode=0o700)
    previous_umask = os.umask(0o022)
    try:
        subprocess.run((str(base), "-I", "-m", "venv", "--copies", str(destination)), check=True)
        python = destination / "bin/python"
        _identity(python)
        if python.is_symlink() or python.stat().st_mode & 0o022:
            raise CiPythonError("validation Python must be a private regular executable")
        # Exercise the same nested-venv/base-interpreter relationship used by
        # real pipx fixtures without installing or modifying application data.
        with tempfile.TemporaryDirectory(prefix="nested-", dir=destination) as temporary:
            nested = Path(temporary) / "venv"
            subprocess.run((str(python), "-I", "-m", "venv", "--without-pip", str(nested)), check=True)
            result = subprocess.run(
                (str(nested / "bin/python"), "-I", "-c",
                 "import json,ssl,sqlite3,sys; print(json.dumps(sys._base_executable))"),
                env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=True,
            )
            nested_base = Path(json.loads(result.stdout)).resolve(strict=True)
            configuration = dict(line.split(" = ", 1) for line in (nested / "pyvenv.cfg").read_text().splitlines())
            configured_home = Path(configuration["home"])
            if nested_base != base or configured_home.resolve(strict=True) != base.parent:
                raise CiPythonError("nested validation Python changed the selected interpreter")
            if _identity(nested_base).st_mode & 0o022 or _identity(configured_home, directory=True).st_mode & 0o022:
                raise CiPythonError("nested validation Python is not safe for native updater checks")
    finally:
        os.umask(previous_umask)
    print("Private validation Python and closed-environment nested venv verified.")
    return python


def prepare() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise CiPythonError("toolchain preparation requires an ephemeral GitHub-hosted runner")
    cache = Path(os.environ["RUNNER_TOOL_CACHE"])
    temporary = Path(os.environ["RUNNER_TEMP"])
    github_path = Path(os.environ["GITHUB_PATH"])
    if not all(path.is_absolute() for path in (cache, temporary, github_path)):
        raise CiPythonError("hosted runner paths must be absolute")
    base = secure_interpreter(Path(sys._base_executable), cache, platform=sys.platform)
    python = create_validation_venv(base, temporary.resolve(strict=True) / "native-validation-python")
    with github_path.open("a", encoding="utf-8") as output:
        output.write(f"{python.parent}\n")


if __name__ == "__main__":
    try:
        prepare()
    except CiPythonError as error:
        print(f"Native validation Python preparation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        print("Native validation Python preparation failed.", file=sys.stderr)
        raise SystemExit(1)
