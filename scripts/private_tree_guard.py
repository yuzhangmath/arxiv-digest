from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class FileState:
    kind: Literal["file", "symlink"]
    mode: int
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class GitState:
    head: str
    refs_sha256: str
    index_sha256: str
    status_sha256: str
    local_config_sha256: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    files: dict[str, FileState]
    git: GitState | None

    def to_json(self) -> dict[str, object]:
        return {
            "files": {name: asdict(value) for name, value in self.files.items()},
            "git": None if self.git is None else asdict(self.git),
        }


@dataclass(frozen=True, slots=True)
class Verification:
    changed: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    git_changed: bool

    @property
    def ok(self) -> bool:
        return not (self.changed or self.added or self.removed or self.git_changed)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _git_state(root: Path) -> GitState:
    return GitState(
        head=_git(root, "rev-parse", "HEAD").decode("ascii").strip(),
        refs_sha256=_sha256(_git(root, "show-ref")),
        index_sha256=_sha256(_git(root, "ls-files", "--stage", "-z")),
        status_sha256=_sha256(_git(root, "status", "--porcelain=v1", "-z")),
        local_config_sha256=_sha256(
            _git(root, "config", "--local", "--null", "--list")
        ),
    )


def _file_state(path: Path) -> FileState:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        target = os.fsencode(os.readlink(path))
        return FileState("symlink", mode, len(target), hashlib.sha256(target).hexdigest())
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("unsupported non-regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return FileState("file", mode, size, digest.hexdigest())


def _paths_without_following_symlinks(root: Path):
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        names.sort()
        filenames.sort()
        if ".git" in names:
            names.remove(".git")
        for name in tuple(names):
            path = directory_path / name
            if path.is_symlink():
                names.remove(name)
                yield path
        for name in filenames:
            yield directory_path / name


def create_snapshot(root: Path, *, include_git: bool = True) -> Snapshot:
    root = root.resolve()
    files: dict[str, FileState] = {}
    for path in _paths_without_following_symlinks(root):
        relative = path.relative_to(root).as_posix()
        try:
            files[relative] = _file_state(path)
        except ValueError:
            continue
    return Snapshot(files, _git_state(root) if include_git else None)


def verify_snapshot(
    root: Path, before: Snapshot, *, include_git: bool = True
) -> Verification:
    after = create_snapshot(root, include_git=include_git)
    before_names = set(before.files)
    after_names = set(after.files)
    return Verification(
        changed=tuple(
            sorted(
                name
                for name in before_names & after_names
                if before.files[name] != after.files[name]
            )
        ),
        added=tuple(sorted(after_names - before_names)),
        removed=tuple(sorted(before_names - after_names)),
        git_changed=include_git and before.git != after.git,
    )


def _write_manifest_atomic(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path) -> Snapshot:
    value = json.loads(path.read_text(encoding="utf-8"))
    files = {
        name: FileState(
            kind=state["kind"],
            mode=state["mode"],
            size=state["size"],
            sha256=state["sha256"],
        )
        for name, state in value["files"].items()
    }
    raw_git = value["git"]
    git = None if raw_git is None else GitState(**raw_git)
    return Snapshot(files=files, git=git)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("snapshot", "verify"))
    parser.add_argument("root", type=Path)
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args()
    if arguments.mode == "snapshot":
        value = create_snapshot(arguments.root)
        _write_manifest_atomic(
            arguments.manifest,
            (json.dumps(value.to_json(), sort_keys=True) + "\n").encode("utf-8"),
        )
        return 0
    result = verify_snapshot(arguments.root, _load(arguments.manifest))
    if result.ok:
        return 0
    print(json.dumps(asdict(result), sort_keys=True))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
