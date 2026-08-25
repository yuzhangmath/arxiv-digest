#!/usr/bin/env python3
"""Privacy release gate for source trees, archives, and Git history.

Diagnostics deliberately identify rules and artifact classes without including
the content that triggered them.
"""

from __future__ import annotations

import ast
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tomllib
import sqlite3
import subprocess
import sys
import tempfile
import tarfile
import zipfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, TextIO
from urllib.parse import quote
from urllib.parse import unquote, urlsplit


MAX_TEXT_BYTES = 8 * 1024 * 1024
_AUDITED_APPLICATION_ICON = "src/arxiv_digest/assets/arxiv-digest.icns"
_AUDITED_APPLICATION_ICON_SHA256 = (
    "bfd9510940f503cd41e96c7f5e082b282529b158f7e4cb92131df80f1851751f"
)

_GENERIC_PRIVATE_PATTERNS = (
    re.compile(
        r"(?<![A-Za-z0-9_])/(?:Users|home)/[A-Za-z0-9._-]+"
        r"(?:/[^\r\n\0]*)?"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_])[A-Z]:\\Users\\[A-Za-z0-9._-]+"
        r"(?:\\[^\r\n\0]*)?"
    ),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?"
        r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|"
        r"AKIA[A-Z0-9]{16}|[\"'][A-Za-z0-9_+/=\-]{24,}[\"'])"
    ),
)


@dataclass(slots=True)
class PrivacyViolation(RuntimeError):
    relative_path: str
    artifact_class: str
    rule_id: str

    def __str__(self) -> str:
        return (
            f"{self.relative_path}: {self.artifact_class} "
            f"[rule:{self.rule_id}]"
        )


def _rule_id(rule: str) -> str:
    return hashlib.sha256(rule.encode("utf-8")).hexdigest()[:12]


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


@dataclass(frozen=True, slots=True)
class DenyRule:
    artifact_class: str
    value: str

    @property
    def rule_id(self) -> str:
        return _rule_id(f"deny:{self.artifact_class}:{_normalized(self.value)}")


@dataclass(frozen=True, slots=True)
class Denylist:
    rules: tuple[DenyRule, ...] = ()

    @classmethod
    def from_mapping(cls, values: Mapping[str, Iterable[str]]) -> "Denylist":
        unique: dict[tuple[str, str], DenyRule] = {}
        for artifact_class, candidates in values.items():
            safe_class = re.sub(r"[^a-z0-9-]+", "-", artifact_class.casefold())
            for candidate in candidates:
                value = str(candidate).strip()
                if not value:
                    continue
                unique[(safe_class, _normalized(value))] = DenyRule(
                    safe_class,
                    value,
                )
        return cls(
            tuple(
                unique[key]
                for key in sorted(unique)
            )
        )

    def values(self, artifact_class: str) -> frozenset[str]:
        return frozenset(
            rule.value
            for rule in self.rules
            if rule.artifact_class == artifact_class
        )

    def all_values(self) -> frozenset[str]:
        return frozenset(rule.value for rule in self.rules)

    def to_mapping(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for rule in self.rules:
            result.setdefault(rule.artifact_class, []).append(rule.value)
        return result


def publish_denylist(denylist: Denylist, destination: Path) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (
        json.dumps(
            {"schema_version": 1, "rules": denylist.to_mapping()},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def load_denylist(path: Path) -> Denylist:
    parsed = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_json_object,
    )
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        raise ValueError("unsupported denylist schema")
    raw_rules = parsed.get("rules")
    if not isinstance(raw_rules, dict):
        raise ValueError("denylist rules must be an object")
    validated: dict[str, list[str]] = {}
    for raw_class, raw_values in raw_rules.items():
        if not isinstance(raw_class, str) or not isinstance(raw_values, list):
            raise ValueError("invalid denylist rule collection")
        if not all(isinstance(value, str) for value in raw_values):
            raise ValueError("invalid denylist rule value")
        validated[raw_class] = raw_values
    return Denylist.from_mapping(validated)


@dataclass(frozen=True, slots=True)
class GitObject:
    oid: str
    object_type: str
    data: bytes
    path: str | None = None


class GitObjectReader(Protocol):
    """Read-only Git surface used by derivation and history auditing."""

    def local_identities(self) -> tuple[str, ...]: ...

    def effective_identities(self) -> tuple[str, ...]: ...

    def reachable_objects(self) -> tuple[GitObject, ...]: ...

    def candidate_paths(self) -> tuple[str, ...]: ...

    def remotes(self) -> Mapping[str, tuple[str, ...]]: ...


class GitReadError(RuntimeError):
    """A sanitized read-only Git plumbing failure."""


class RealGitObjectReader:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        allow_missing: bool = False,
    ) -> bytes:
        environment = dict(os.environ)
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        completed = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "-C",
                str(self.root),
                *arguments,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            if allow_missing and completed.returncode == 1:
                return b""
            raise GitReadError("read-only Git query failed")
        return completed.stdout

    def remotes(self) -> Mapping[str, tuple[str, ...]]:
        names = tuple(
            line
            for line in self._run(("remote",)).decode("utf-8").splitlines()
            if line
        )
        result: dict[str, tuple[str, ...]] = {}
        for name in names:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                raise GitReadError("invalid remote name in Git metadata")
            urls = tuple(
                line
                for line in self._run(
                    ("config", "--get-all", f"remote.{name}.url"),
                    allow_missing=True,
                )
                .decode("utf-8")
                .splitlines()
                if line
            )
            push_urls = tuple(
                line
                for line in self._run(
                    ("config", "--get-all", f"remote.{name}.pushurl"),
                    allow_missing=True,
                )
                .decode("utf-8")
                .splitlines()
                if line
            )
            result[name] = urls + push_urls
        return result

    def candidate_paths(self) -> tuple[str, ...]:
        output = self._run(
            ("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        )
        paths: list[str] = []
        for raw_path in output.split(b"\0"):
            if not raw_path:
                continue
            try:
                path = raw_path.decode("utf-8")
            except UnicodeDecodeError as error:
                raise GitReadError("non-UTF-8 Git candidate path") from error
            paths.append(_safe_archive_member(path))
        return tuple(sorted(set(paths)))

    def local_identities(self) -> tuple[str, ...]:
        name = self._run(
            ("config", "--local", "--get", "user.name"),
            allow_missing=True,
        ).decode("utf-8").strip()
        email = self._run(
            ("config", "--local", "--get", "user.email"),
            allow_missing=True,
        ).decode("utf-8").strip()
        if name and email:
            return (f"{name} <{email}>",)
        return tuple(value for value in (name, email) if value)

    def effective_identities(self) -> tuple[str, ...]:
        return tuple(
            self._run(("var", variable)).decode("utf-8").strip()
            for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")
        )

    def reachable_objects(self) -> tuple[GitObject, ...]:
        listing = self._run(("rev-list", "--objects", "--all"))
        objects: list[GitObject] = []
        commit_tree_paths: set[tuple[str, str]] = set()
        for raw_line in listing.splitlines():
            raw_oid, separator, raw_path = raw_line.partition(b" ")
            try:
                oid = raw_oid.decode("ascii")
            except UnicodeDecodeError as error:
                raise GitReadError("invalid Git object identifier") from error
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", oid):
                raise GitReadError("invalid Git object identifier")
            object_type = self._run(("cat-file", "-t", oid)).decode("ascii").strip()
            if object_type not in {"blob", "commit", "tag", "tree"}:
                raise GitReadError("unsupported reachable Git object type")
            size_text = self._run(("cat-file", "-s", oid)).decode("ascii").strip()
            try:
                size = int(size_text)
            except ValueError as error:
                raise GitReadError("invalid Git object size") from error
            if size < 0 or size > MAX_TEXT_BYTES:
                raise PrivacyViolation(
                    f"git-{object_type}-{oid[:12]}",
                    "history-object-size",
                    _rule_id("history:max-object-bytes"),
                )
            data = self._run(("cat-file", "-p", oid))
            if len(data) != size and object_type != "tree":
                raise GitReadError("Git object size changed during scan")
            path: str | None = None
            if separator and raw_path:
                try:
                    path = raw_path.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise GitReadError("non-UTF-8 Git history path") from error
            objects.append(GitObject(oid, object_type, data, path))
            if object_type == "commit":
                tree_listing = self._run(
                    ("ls-tree", "-r", "-t", "-z", "--full-tree", oid)
                )
                for raw_entry in tree_listing.split(b"\0"):
                    if not raw_entry:
                        continue
                    raw_metadata, marker, raw_tree_path = raw_entry.partition(b"\t")
                    if not marker:
                        raise GitReadError("invalid Git commit tree entry")
                    metadata = raw_metadata.split()
                    if len(metadata) != 3 or metadata[1] not in {
                        b"blob",
                        b"commit",
                        b"tree",
                    }:
                        raise GitReadError("invalid Git commit tree metadata")
                    try:
                        tree_path = raw_tree_path.decode("utf-8")
                        tree_type = metadata[1].decode("ascii")
                    except UnicodeDecodeError as error:
                        raise GitReadError("non-UTF-8 Git tree path") from error
                    commit_tree_paths.add(
                        (_safe_archive_member(tree_path), tree_type)
                    )
            if object_type == "tree":
                prefix = "" if path is None else path.rstrip("/") + "/"
                for index, line in enumerate(data.splitlines()):
                    _, marker, raw_name = line.partition(b"\t")
                    if not marker:
                        continue
                    try:
                        name = raw_name.decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise GitReadError("non-UTF-8 Git tree path") from error
                    objects.append(
                        GitObject(
                            f"{oid}-path-{index}",
                            "tree",
                            b"",
                            prefix + name,
                        )
                    )
        objects.extend(
            GitObject(f"commit-tree-path-{index}", object_type, b"", path)
            for index, (path, object_type) in enumerate(
                sorted(commit_tree_paths)
            )
        )
        return tuple(objects)


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_members: int = 20_000
    max_member_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024


DEFAULT_ARCHIVE_LIMITS = ArchiveLimits()

CANONICAL_REPOSITORY_URL = "https://github.com/yuzhangmath/arxiv-digest"
MAINTAINER_REMOTE = "git@github.com:yuzhangmath/arxiv-digest.git"


_ARXIV_ID = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]+/\d{7})"
    r"(?:v\d+)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_DERIVATION_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        ".venv",
        "venv",
        "node_modules",
    }
)
_AUDITED_PUBLIC_ARTIFACTS = frozenset(
    {
        "docs/superpowers/specs/2026-08-22-public-arxiv-digest-design.md",
        "docs/superpowers/plans/2026-08-22-public-arxiv-digest.md",
        "docs/superpowers/specs/2026-08-25-confirmed-daily-list-review-design.md",
        "docs/superpowers/plans/2026-08-25-confirmed-daily-list-review.md",
        "src/arxiv_digest/reconciliation.py",
        "src/arxiv_digest/storage/migrations/0004_confirmed_daily_list.sql",
    }
)
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9._%+-])"
)
_ABSOLUTE_HOME_PATTERNS = (
    re.compile(
        r"/(?:Users|home)/[A-Za-z0-9._-]+(?:/[^\r\n\0]*)?"
    ),
    re.compile(
        r"(?i)[A-Z]:\\Users\\[A-Za-z0-9._-]+(?:\\[^\r\n\0]*)?"
    ),
)
_IDENTITY_PATTERN = re.compile(r"(?:^|\s)([^\r\n<>]+?)\s*<([^<>\s]+@[^<>\s]+)>")
_NON_AUTHORITATIVE_PARTS = frozenset(
    {"test", "tests", "fixture", "fixtures", "example", "examples", "docs"}
)
_GENERIC_PRIVATE_RELATIVE_PATHS = frozenset(
    {
        ".github",
        ".github/workflows",
        ".github/workflows/tests.yml",
        "data/library.json",
        "data/library.sqlite3",
        "data/state.sqlite3",
        "interests.yaml",
        "profile.toml",
    }
)
_AUTHOR_SEPARATOR = re.compile(r"\s*(?:,|;)\s*|\s+(?:and|&)\s+", re.IGNORECASE)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key in private structured data")
        result[key] = value
    return result


def _key_kind(key: str, parent_kind: str | None = None) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    tokens = frozenset(normalized.split("_"))
    has_seed = "seed" in tokens or normalized.startswith("seed")
    if normalized in {"abstract", "abstracts", "summary", "summaries"}:
        return "excluded"
    if normalized in {"filename", "path"} or normalized.endswith("_path"):
        return "path"
    if has_seed and (
        bool(tokens & {"paper", "papers", "id", "ids", "arxiv"})
        or normalized.startswith(("seedpaper", "seedid", "seedarxiv"))
    ):
        return "seed-id"
    if normalized in {"arxiv_id", "arxiv_ids", "paper_id", "paper_ids"}:
        return "paper-id"
    if (
        "keyword" in normalized
        or "kw" in tokens
        or normalized in {"terms", "interest_terms"}
    ):
        return "keyword"
    if "phrase" in normalized:
        return "phrase"
    if "author" in normalized:
        return "author"
    if normalized in {"title", "titles", "paper_title", "paper_titles"}:
        return "title"
    if "paper" in normalized or "arxiv" in normalized:
        return "paper-id"
    if "preference" in normalized or "interest" in normalized:
        return "keyword"
    if has_seed:
        return "seed-id"
    if normalized == "name" and parent_kind == "author":
        return "author"
    return parent_kind


def _scalar_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _scalar_strings(item)


def _collect_typed_scalar(
    kind: str,
    candidate: str,
    collected: dict[str, set[str]],
) -> None:
    if kind in {"paper-id", "seed-id"}:
        collected[kind].update(_ARXIV_ID.findall(candidate))
    elif kind == "author":
        collected[kind].update(
            name
            for raw_name in _AUTHOR_SEPARATOR.split(candidate)
            if (name := raw_name.strip())
        )
    else:
        collected[kind].add(candidate)


def _collect_structured(
    value: object,
    collected: dict[str, set[str]],
    *,
    parent_kind: str | None = None,
) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            key_ids = _ARXIV_ID.findall(key)
            if key_ids:
                identifier_kind = (
                    "seed-id" if parent_kind == "seed-id" else "paper-id"
                )
                collected[identifier_kind].update(key_ids)
            kind = _key_kind(key, parent_kind)
            if kind == "excluded":
                continue
            if kind is not None:
                for scalar in _scalar_strings(child):
                    candidate = scalar.strip()
                    if not candidate:
                        continue
                    _collect_typed_scalar(kind, candidate, collected)
            _collect_structured(child, collected, parent_kind=kind)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            _collect_structured(child, collected, parent_kind=parent_kind)
    elif isinstance(value, str):
        matches = _ARXIV_ID.findall(value)
        if matches:
            identifier_kind = "seed-id" if parent_kind == "seed-id" else "paper-id"
            collected[identifier_kind].update(matches)


def _is_authoritative_structured(relative: Path) -> bool:
    folded_parts = {part.casefold() for part in relative.parts[:-1]}
    if folded_parts & _NON_AUTHORITATIVE_PARTS:
        return False
    stem = relative.stem.casefold().replace("-", "_")
    return any(
        marker in stem
        for marker in (
            "library",
            "profile",
            "preference",
            "interest",
            "collection",
            "user_data",
            "userdata",
            "paper",
        )
    ) or "data" in folded_parts


def _private_files(root: Path) -> Iterable[tuple[Path, Path]]:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part.casefold() in _DERIVATION_EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        yield relative, path


def _parse_python_preferences(data: bytes) -> dict[str, object]:
    tree = ast.parse(data.decode("utf-8"))
    result: dict[str, object] = {}
    for statement in tree.body:
        target: ast.expr | None = None
        value_node: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value_node = statement.value
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
            value_node = statement.value
        if not isinstance(target, ast.Name) or value_node is None:
            continue
        name = target.id
        normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
        tokens = frozenset(normalized.split("_"))
        kind = _key_kind(name)
        if kind is None or not (
            any(
                marker in normalized
                for marker in (
                    "prefer",
                    "interest",
                    "keyword",
                    "phrase",
                    "author",
                    "paper",
                    "arxiv",
                    "seed",
                )
            )
            or "kw" in tokens
        ):
            continue
        try:
            result[name] = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            continue
    return result


def _yaml_scalar(value: str) -> str:
    candidate = value.strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {
        "'",
        '"',
    }:
        candidate = candidate[1:-1]
    return candidate


def _parse_restricted_yaml(data: bytes) -> dict[str, object]:
    text = data.decode("utf-8")
    if len(data) > MAX_TEXT_BYTES or re.search(
        r"(?:^|\s)(?:!!|!\w|&\w|\*\w)|(?:^|:)\s*[>|](?:\s|$)",
        text,
        re.MULTILINE,
    ):
        raise ValueError("unsupported YAML feature in private structured data")
    result: dict[str, object] = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[:1].isspace():
            stripped = raw_line.strip()
            if current_key is None or not stripped.startswith("- "):
                raise ValueError("only top-level YAML scalar/list values are supported")
            existing = result.setdefault(current_key, [])
            if not isinstance(existing, list):
                raise ValueError("mixed YAML scalar/list value")
            existing.append(_yaml_scalar(stripped[2:]))
            continue
        if ":" not in raw_line:
            raise ValueError("invalid top-level YAML entry")
        key, raw_value = raw_line.split(":", 1)
        key = key.strip()
        if not key or key in result:
            raise ValueError("invalid or duplicate YAML key")
        current_key = key
        scalar = raw_value.strip()
        if not scalar:
            result[key] = []
        elif scalar.startswith("["):
            try:
                parsed = ast.literal_eval(scalar)
            except (ValueError, SyntaxError) as error:
                raise ValueError("invalid YAML inline list") from error
            if not isinstance(parsed, list) or not all(
                isinstance(item, (str, int, float, bool)) or item is None
                for item in parsed
            ):
                raise ValueError("only scalar YAML list entries are supported")
            result[key] = parsed
        else:
            result[key] = _yaml_scalar(scalar)
    return result


def _sqlite_column_kind(table: str, column: str) -> str | None:
    folded_table = table.casefold()
    folded_column = column.casefold()
    if "abstract" in folded_column or "summary" in folded_column:
        return None
    if folded_column in {"hash", "sha256"} or folded_column.endswith(
        ("_hash", "_sha256")
    ):
        return "hash"
    if folded_column in {
        "error_detail",
        "error_message",
        "last_error_message",
        "raw_error",
    }:
        return "free-form-error"
    if folded_column in {"arxiv_id", "paper_id", "seed_id"}:
        return "seed-id" if "seed" in folded_table else "paper-id"
    if "title" in folded_column:
        return "title"
    if "author" in folded_column or (
        folded_column == "name" and "author" in folded_table
    ):
        return "author"
    if "keyword" in folded_column or (
        folded_column in {"term", "value"} and "keyword" in folded_table
    ):
        return "keyword"
    if "phrase" in folded_column or (
        folded_column == "value" and "phrase" in folded_table
    ):
        return "phrase"
    return None


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_snapshot_digests(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for candidate in (path, path.with_name(path.name + "-wal")):
        try:
            digest = hashlib.sha256()
            descriptor = _open_regular_sqlite_member(candidate)
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[candidate.name] = digest.hexdigest()
        except FileNotFoundError:
            if candidate == path:
                raise ValueError(
                    "SQLite source disappeared during inspection"
                ) from None
            continue
    return result


def _open_regular_sqlite_member(path: Path) -> int:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("SQLite snapshot members must be regular files")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    opened = os.fstat(descriptor)
    after = path.stat(follow_symlinks=False)
    identities = (
        (before.st_dev, before.st_ino),
        (opened.st_dev, opened.st_ino),
        (after.st_dev, after.st_ino),
    )
    if len(set(identities)) != 1 or not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise ValueError("SQLite snapshot member changed during inspection")
    return descriptor


def _copy_regular_sqlite_member(source: Path, destination: Path) -> None:
    source_descriptor = _open_regular_sqlite_member(source)
    try:
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.fchmod(destination_descriptor, 0o600)
            with os.fdopen(source_descriptor, "rb", closefd=False) as source_handle:
                with os.fdopen(
                    destination_descriptor,
                    "wb",
                    closefd=False,
                ) as destination_handle:
                    shutil.copyfileobj(source_handle, destination_handle)
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def _copy_sqlite_snapshot(path: Path, destination: Path) -> None:
    for _ in range(3):
        destination.unlink(missing_ok=True)
        destination.with_name(destination.name + "-wal").unlink(missing_ok=True)
        before = _sqlite_snapshot_digests(path)
        try:
            _copy_regular_sqlite_member(path, destination)
            source_wal = path.with_name(path.name + "-wal")
            if source_wal.name in before:
                _copy_regular_sqlite_member(
                    source_wal,
                    destination.with_name(destination.name + "-wal"),
                )
        except FileNotFoundError:
            continue
        after = _sqlite_snapshot_digests(path)
        copied = _sqlite_snapshot_digests(destination)
        if before == after == copied:
            os.chmod(destination, 0o600)
            snapshot_wal = destination.with_name(destination.name + "-wal")
            if snapshot_wal.exists():
                os.chmod(snapshot_wal, 0o600)
            return
    raise ValueError("SQLite source changed during read-only inspection")


def _collect_sqlite(path: Path, collected: dict[str, set[str]]) -> None:
    with tempfile.TemporaryDirectory(prefix="arxiv-digest-sqlite-") as temporary:
        temporary_path = Path(temporary)
        os.chmod(temporary_path, 0o700)
        snapshot = temporary_path / path.name
        _copy_sqlite_snapshot(path, snapshot)
        uri = f"file:{quote(str(snapshot), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            tables = connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for (table,) in tables:
                if not isinstance(table, str):
                    continue
                columns = connection.execute(
                    f"PRAGMA table_info({_quote_identifier(table)})"
                ).fetchall()
                for column_row in columns:
                    column = column_row[1]
                    if not isinstance(column, str):
                        continue
                    kind = _sqlite_column_kind(table, column)
                    if kind is None:
                        continue
                    query = (
                        f"SELECT {_quote_identifier(column)} "
                        f"FROM {_quote_identifier(table)} "
                        f"WHERE {_quote_identifier(column)} IS NOT NULL LIMIT 100000"
                    )
                    for (raw_value,) in connection.execute(query):
                        if not isinstance(raw_value, (str, int, float)):
                            continue
                        candidate = str(raw_value).strip()
                        if not candidate:
                            continue
                        _collect_typed_scalar(kind, candidate, collected)
        finally:
            connection.close()


def _collect_identity(identity: str, collected: dict[str, set[str]]) -> None:
    match = _IDENTITY_PATTERN.search(identity)
    if match is None:
        candidate = identity.strip()
        if _EMAIL_PATTERN.fullmatch(candidate):
            collected["email"].add(candidate)
        elif candidate:
            collected["git-identity"].add(candidate)
        return
    name = re.sub(r"^(?:author|committer)\s+", "", match.group(1).strip())
    if name:
        collected["git-identity"].add(name)
    collected["email"].add(match.group(2))


def _private_relative_filename(path: str) -> str | None:
    candidate = Path(path).as_posix()
    name = Path(candidate).name.casefold()
    if (
        candidate in {"", "."}
        or candidate in _AUDITED_PUBLIC_ARTIFACTS
        or candidate.casefold() in _GENERIC_PRIVATE_RELATIVE_PATHS
        or name in {
            "readme",
            "readme.md",
            "readme.rst",
            "readme.txt",
            "license",
            "license.md",
            "license.rst",
            "license.txt",
            "copying",
            "notice",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "arxiv_digest.py",
            "package.json",
            "package-lock.json",
            "__init__.py",
            ".gitignore",
            ".gitattributes",
            ".ds_store",
        }
    ):
        return None
    return candidate


def _collect_git_derivation(
    reader: GitObjectReader,
    collected: dict[str, set[str]],
) -> None:
    for identity in (*reader.local_identities(), *reader.effective_identities()):
        _collect_identity(identity, collected)
    for item in reader.reachable_objects():
        if item.path is not None and item.object_type != "tree":
            filename = _private_relative_filename(item.path)
            if filename is not None:
                collected["relative-filename"].add(filename)
        if item.object_type not in {"commit", "tag"}:
            continue
        text = item.data[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
        for match in _EMAIL_PATTERN.findall(text):
            collected["email"].add(match)
        for pattern in _ABSOLUTE_HOME_PATTERNS:
            collected["absolute-path"].update(pattern.findall(text))
        for line in text.splitlines():
            if line.startswith(("author ", "committer ", "tagger ")):
                _collect_identity(line, collected)


def derive_denylist(
    private_root: Path,
    *,
    git_reader: GitObjectReader | None = None,
) -> Denylist:
    root = private_root.resolve()
    collected: dict[str, set[str]] = {
        "paper-id": set(),
        "seed-id": set(),
        "title": set(),
        "author": set(),
        "keyword": set(),
        "phrase": set(),
        "git-identity": set(),
        "email": set(),
        "free-form-error": set(),
        "hash": set(),
        "path": set(),
        "absolute-path": {str(root), str(Path.home().resolve())},
        "relative-filename": set(),
    }
    for relative, path in _private_files(root):
        private_filename = _private_relative_filename(relative.as_posix())
        if private_filename is not None:
            collected["relative-filename"].add(private_filename)
        suffix = path.suffix.casefold()
        if suffix in {".sqlite", ".sqlite3", ".db"}:
            _collect_sqlite(path, collected)
            continue
        if suffix not in {".json", ".py", ".toml", ".yaml", ".yml"}:
            continue
        data = path.read_bytes()
        if len(data) > MAX_TEXT_BYTES:
            continue
        if suffix == ".py":
            parsed = _parse_python_preferences(data)
        elif not _is_authoritative_structured(relative):
            continue
        elif suffix == ".json":
            parsed = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}")
                ),
            )
        elif suffix == ".toml":
            parsed = tomllib.loads(data.decode("utf-8"))
        else:
            parsed = _parse_restricted_yaml(data)
        _collect_structured(parsed, collected)
    if git_reader is None and (root / ".git").exists():
        git_reader = RealGitObjectReader(root)
    if git_reader is not None:
        _collect_git_derivation(git_reader, collected)
    return Denylist.from_mapping(collected)


def _classify_path(relative_path: str) -> str | None:
    path = Path(relative_path)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    folded_parts = {part.casefold() for part in path.parts[:-1]}
    if suffix == ".eml":
        return "email-artifact"
    if suffix == ".pdf":
        return "pdf-artifact"
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        return "database-artifact"
    if name.endswith(("-wal", "-shm", "-journal")):
        return "database-journal"
    collection_stem = path.stem.casefold().replace("-", "_")
    if suffix == ".json" and any(
        marker in collection_stem
        for marker in (
            "collection",
            "library",
            "papers",
            "profile",
            "preferences",
            "user_data",
        )
    ):
        return "collection-artifact"
    if (
        name in {"runtime.json", "runtime.lock", "profile.lock"}
        or "runtime" in folded_parts
    ):
        return "runtime-artifact"
    if folded_parts & {"cache", "caches", ".cache"}:
        return "cache-artifact"
    if suffix in {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"}:
        return "archive-artifact"
    if suffix in {".pem", ".key", ".p12", ".pfx"} or name.startswith(
        ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
    ):
        return "private-key-artifact"
    credential_stem = path.stem.casefold().replace("-", "_")
    if name in {".env", ".env.local", ".npmrc", ".pypirc", ".netrc"} or (
        credential_stem
        in {
            "credential",
            "credentials",
            "secret",
            "secrets",
            "token",
            "tokens",
            "api_key",
            "apikey",
        }
    ):
        return "credential-artifact"
    if (
        name in {".ds_store", "thumbs.db"}
        or suffix in {".swp", ".swo", ".sublime-workspace"}
        or any(part.casefold() in {".idea", ".vscode"} for part in path.parts)
    ):
        return "editor-metadata"
    return None


def _remote_violation(rule: str) -> PrivacyViolation:
    return PrivacyViolation(".", "remote-policy", _rule_id(f"remote:{rule}"))


def _canonical_https_repository(value: str) -> tuple[str, str, str]:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise _remote_violation("invalid-port") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.endswith("/")
        or "%" in parsed.path
    ):
        raise _remote_violation("invalid-https")
    path = unquote(parsed.path)
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.strip("/").split("/")
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        raise _remote_violation("invalid-path")
    owner, repository = parts
    return owner, repository, f"https://github.com/{owner}/{repository}"


def validate_public_target(
    root: Path,
    *,
    expect_no_remote: bool = False,
    expected_remote: str | None = None,
    expected_remote_from_project: bool = False,
    git_reader: object | None = None,
) -> None:
    root = root.resolve()
    if root.name != "arxiv-digest":
        raise _remote_violation("repository-basename")
    policy_count = sum(
        (
            bool(expect_no_remote),
            expected_remote is not None,
            bool(expected_remote_from_project),
        )
    )
    if policy_count != 1:
        raise _remote_violation("exactly-one-policy")
    if git_reader is None:
        git_reader = RealGitObjectReader(root)
    remote_method = getattr(git_reader, "remotes", None)
    if not callable(remote_method):
        raise _remote_violation("reader-interface")
    raw_remotes = remote_method()
    remotes = {
        str(name): tuple(str(url) for url in urls)
        for name, urls in dict(raw_remotes).items()
    }
    if expect_no_remote:
        if remotes:
            raise _remote_violation("unexpected-remote")
        return
    if set(remotes) != {"origin"} or len(remotes["origin"]) != 1:
        raise _remote_violation("single-origin")
    actual = remotes["origin"][0]
    if expected_remote_from_project:
        project_path = root / "pyproject.toml"
        try:
            project = tomllib.loads(project_path.read_text(encoding="utf-8"))
            repository_url = project["project"]["urls"]["Repository"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
            raise _remote_violation("project-url") from error
        if not isinstance(repository_url, str):
            raise _remote_violation("project-url")
        owner, repository, normalized = _canonical_https_repository(repository_url)
        if normalized != CANONICAL_REPOSITORY_URL or repository != root.name:
            raise _remote_violation("confirmed-project-url")
        required = f"git@github.com:{owner}/{repository}.git"
        if required != MAINTAINER_REMOTE or actual != required:
            raise _remote_violation("project-ssh-origin")
        return
    assert expected_remote is not None
    _, repository, _ = _canonical_https_repository(expected_remote)
    if repository != root.name:
        raise _remote_violation("expected-repository")
    if actual != expected_remote:
        raise _remote_violation("expected-origin")


def _requires_structured_field_scope(rule: DenyRule) -> bool:
    value = _normalized(rule.value)
    return (
        rule.artifact_class == "keyword" and value.isalpha()
    ) or (
        rule.artifact_class == "author"
        and len(value) <= 3
        and value.isalpha()
    )


def _structured_field_values(
    relative_path: str,
    data: bytes,
) -> dict[str, frozenset[str]]:
    suffix = Path(relative_path).suffix.casefold()
    collected: dict[str, set[str]] = {
        "paper-id": set(),
        "seed-id": set(),
        "title": set(),
        "author": set(),
        "keyword": set(),
        "phrase": set(),
    }
    try:
        if suffix == ".py":
            parsed = _parse_python_preferences(data)
        elif suffix == ".json":
            parsed = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}")
                ),
            )
        elif suffix == ".toml":
            parsed = tomllib.loads(data.decode("utf-8"))
        elif suffix in {".yaml", ".yml"}:
            parsed = _parse_restricted_yaml(data)
        else:
            return {key: frozenset() for key in collected}
    except (SyntaxError, UnicodeDecodeError, ValueError):
        return {key: frozenset() for key in collected}
    _collect_structured(parsed, collected)
    return {
        key: frozenset(_normalized(value) for value in values)
        for key, values in collected.items()
    }


def _scan_bytes(
    relative_path: str,
    data: bytes,
    *,
    denylist: Denylist | None = None,
) -> None:
    if len(data) > MAX_TEXT_BYTES:
        raise PrivacyViolation(
            relative_path,
            "oversized-artifact",
            _rule_id("content:max-text-bytes"),
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PrivacyViolation(
            relative_path,
            "binary-artifact",
            _rule_id("content:utf8"),
        ) from error
    for index, pattern in enumerate(_GENERIC_PRIVATE_PATTERNS, start=1):
        if pattern.search(text):
            raise PrivacyViolation(
                relative_path,
                "private-content",
                _rule_id(f"content:generic:{index}"),
            )
    normalized_text = _normalized(text)
    structured_values: dict[str, frozenset[str]] | None = None
    for rule in (() if denylist is None else denylist.rules):
        value = _normalized(rule.value)
        if _requires_structured_field_scope(rule):
            if structured_values is None:
                structured_values = _structured_field_values(relative_path, data)
            matched = value in structured_values.get(
                rule.artifact_class,
                frozenset(),
            )
        else:
            pattern = re.compile(rf"(?<!\w){re.escape(value)}(?!\w)")
            matched = pattern.search(normalized_text) is not None
        if matched:
            raise PrivacyViolation(
                relative_path,
                f"denylist-{rule.artifact_class}",
                rule.rule_id,
            )


def _safe_archive_member(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = Path(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PrivacyViolation(
            normalized or "<empty>",
            "unsafe-archive-path",
            _rule_id("archive:path-safety"),
        )
    return normalized


def _katex_manifest_hashes(data: bytes) -> dict[str, str]:
    try:
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise PrivacyViolation(
            "katex-manifest.json",
            "invalid-katex-manifest",
            _rule_id("katex:manifest-json"),
        ) from error
    if not isinstance(parsed, dict) or not isinstance(parsed.get("files"), list):
        raise PrivacyViolation(
            "katex-manifest.json",
            "invalid-katex-manifest",
            _rule_id("katex:manifest-schema"),
        )
    result: dict[str, str] = {}
    for entry in parsed["files"]:
        if not isinstance(entry, dict):
            raise PrivacyViolation(
                "katex-manifest.json",
                "invalid-katex-manifest",
                _rule_id("katex:manifest-entry"),
            )
        path = entry.get("path")
        digest = entry.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or _safe_archive_member(path) != path
            or path in result
        ):
            raise PrivacyViolation(
                "katex-manifest.json",
                "invalid-katex-manifest",
                _rule_id("katex:manifest-entry"),
            )
        result[path] = digest
    if list(result) != sorted(result):
        raise PrivacyViolation(
            "katex-manifest.json",
            "invalid-katex-manifest",
            _rule_id("katex:manifest-order"),
        )
    return result


def _katex_relative_path(relative_path: str) -> str | None:
    marker = "vendor/katex/"
    normalized = relative_path.replace("\\", "/")
    if marker not in normalized:
        return None
    return normalized.split(marker, 1)[1]


def _is_audited_katex_font(
    relative_path: str,
    data: bytes,
    katex_hashes: Mapping[str, str],
) -> bool:
    katex_path = _katex_relative_path(relative_path)
    return (
        katex_path is not None
        and katex_path.startswith("fonts/")
        and Path(katex_path).suffix.casefold() in {".ttf", ".woff", ".woff2"}
        and katex_hashes.get(katex_path) == hashlib.sha256(data).hexdigest()
    )


def _is_audited_application_icon(relative_path: str, data: bytes) -> bool:
    return (
        relative_path == _AUDITED_APPLICATION_ICON
        and hashlib.sha256(data).hexdigest() == _AUDITED_APPLICATION_ICON_SHA256
    )


def _is_audited_application_icon_archive_member(
    relative_path: str,
    data: bytes,
) -> bool:
    wheel_path = _AUDITED_APPLICATION_ICON.removeprefix("src/")
    source_path = relative_path
    if relative_path == wheel_path:
        source_path = _AUDITED_APPLICATION_ICON
    else:
        root, separator, nested = relative_path.partition("/")
        if (
            separator
            and re.fullmatch(r"arxiv_digest-[A-Za-z0-9][A-Za-z0-9._+-]*", root)
            and nested == _AUDITED_APPLICATION_ICON
        ):
            source_path = nested
    return _is_audited_application_icon(source_path, data)


def _archive_zip_members(
    path: Path,
    limits: ArchiveLimits,
) -> Iterable[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > limits.max_members:
            raise PrivacyViolation(
                path.name,
                "archive-member-limit",
                _rule_id("archive:max-members"),
            )
        total = 0
        for info in infos:
            name = _safe_archive_member(info.filename.rstrip("/"))
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise PrivacyViolation(
                    name,
                    "archive-link",
                    _rule_id("archive:no-links"),
                )
            if info.file_size > limits.max_member_bytes:
                raise PrivacyViolation(
                    name,
                    "archive-member-limit",
                    _rule_id("archive:max-member-bytes"),
                )
            total += info.file_size
            if total > limits.max_total_bytes:
                raise PrivacyViolation(
                    name,
                    "archive-size-limit",
                    _rule_id("archive:max-total-bytes"),
                )
            with archive.open(info, "r") as member:
                data = member.read(limits.max_member_bytes + 1)
            if len(data) != info.file_size:
                raise PrivacyViolation(
                    name,
                    "archive-size-mismatch",
                    _rule_id("archive:size-mismatch"),
                )
            yield name, data


def _archive_tar_members(
    path: Path,
    limits: ArchiveLimits,
) -> Iterable[tuple[str, bytes]]:
    with tarfile.open(path, "r:*") as archive:
        count = 0
        total = 0
        for info in archive:
            count += 1
            if count > limits.max_members:
                raise PrivacyViolation(
                    path.name,
                    "archive-member-limit",
                    _rule_id("archive:max-members"),
                )
            name = _safe_archive_member(info.name.rstrip("/"))
            if info.isdir():
                continue
            if not info.isfile():
                raise PrivacyViolation(
                    name,
                    "archive-link",
                    _rule_id("archive:no-links"),
                )
            if info.size > limits.max_member_bytes:
                raise PrivacyViolation(
                    name,
                    "archive-member-limit",
                    _rule_id("archive:max-member-bytes"),
                )
            total += info.size
            if total > limits.max_total_bytes:
                raise PrivacyViolation(
                    name,
                    "archive-size-limit",
                    _rule_id("archive:max-total-bytes"),
                )
            member = archive.extractfile(info)
            if member is None:
                raise PrivacyViolation(
                    name,
                    "archive-read-error",
                    _rule_id("archive:read"),
                )
            data = member.read(limits.max_member_bytes + 1)
            if len(data) != info.size:
                raise PrivacyViolation(
                    name,
                    "archive-size-mismatch",
                    _rule_id("archive:size-mismatch"),
                )
            yield name, data


def scan_archive(
    path: Path,
    *,
    denylist: Denylist | None = None,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> None:
    path = path.resolve()
    member_reader = _archive_zip_members if zipfile.is_zipfile(path) else _archive_tar_members
    manifest_data: bytes | None = None
    for name, data in member_reader(path, limits):
        if name.endswith("/vendor/katex/katex-manifest.json"):
            manifest_data = data
            break
    katex_hashes = {} if manifest_data is None else _katex_manifest_hashes(manifest_data)
    for name, data in member_reader(path, limits):
        artifact_class = _classify_path(name)
        if artifact_class is not None:
            raise PrivacyViolation(
                name,
                artifact_class,
                _rule_id(f"path:{artifact_class}"),
            )
        if _is_audited_application_icon_archive_member(name, data):
            continue
        if _is_audited_katex_font(name, data, katex_hashes):
            continue
        _scan_bytes(name, data, denylist=denylist)


def scan_history(
    root: Path,
    *,
    denylist: Denylist | None = None,
    expect_no_remote: bool = False,
    expected_remote: str | None = None,
    expected_remote_from_project: bool = False,
    git_reader: object | None = None,
) -> None:
    root = root.resolve()
    if git_reader is None:
        git_reader = RealGitObjectReader(root)
    validate_public_target(
        root,
        expect_no_remote=expect_no_remote,
        expected_remote=expected_remote,
        expected_remote_from_project=expected_remote_from_project,
        git_reader=git_reader,
    )
    identity_method = getattr(git_reader, "effective_identities", None)
    object_method = getattr(git_reader, "reachable_objects", None)
    if not callable(identity_method) or not callable(object_method):
        raise PrivacyViolation(
            ".",
            "history-reader",
            _rule_id("history:reader-interface"),
        )
    for index, identity in enumerate(identity_method(), start=1):
        _scan_bytes(
            f"git-effective-identity-{index}",
            str(identity).encode("utf-8"),
            denylist=denylist,
        )
    objects = tuple(object_method())
    katex_hashes: dict[str, str] = {}
    for item in objects:
        if (
            isinstance(item, GitObject)
            and item.object_type == "blob"
            and item.path is not None
            and item.path.endswith("/vendor/katex/katex-manifest.json")
        ):
            katex_hashes = _katex_manifest_hashes(item.data)
            break
    for item in objects:
        if not isinstance(item, GitObject) or item.object_type not in {
            "blob",
            "commit",
            "tag",
            "tree",
        }:
            raise PrivacyViolation(
                "git-object",
                "history-object",
                _rule_id("history:object-interface"),
            )
        label = f"git-{item.object_type}-{item.oid[:12]}"
        if item.path is not None:
            artifact_class = _classify_path(item.path)
            if artifact_class is not None:
                raise PrivacyViolation(
                    item.path,
                    artifact_class,
                    _rule_id(f"path:{artifact_class}"),
                )
            _scan_bytes(item.path, item.path.encode("utf-8"), denylist=denylist)
        if (
            item.object_type == "blob"
            and item.path is not None
            and _is_audited_application_icon(item.path, item.data)
        ):
            continue
        if (
            item.object_type == "blob"
            and item.path is not None
            and _is_audited_katex_font(item.path, item.data, katex_hashes)
        ):
            continue
        _scan_bytes(label if item.path is None else item.path, item.data, denylist=denylist)


def scan_tree(
    root: Path,
    *,
    denylist: Denylist | None = None,
    git_reader: object | None = None,
) -> None:
    root = root.resolve()
    if git_reader is None and (root / ".git").is_dir():
        git_reader = RealGitObjectReader(root)
    if git_reader is not None:
        candidate_method = getattr(git_reader, "candidate_paths", None)
        if not callable(candidate_method):
            raise PrivacyViolation(
                ".",
                "tree-reader",
                _rule_id("tree:reader-interface"),
            )
        relatives = tuple(str(path) for path in candidate_method())
    else:
        relatives = tuple(
            path.relative_to(root).as_posix()
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and ".git" not in path.relative_to(root).parts
            and not (
                set(part.casefold() for part in path.relative_to(root).parts)
                & _DERIVATION_EXCLUDED_PARTS
            )
        )
    manifest_data: bytes | None = None
    for relative in relatives:
        if relative.endswith("/vendor/katex/katex-manifest.json"):
            path = root / relative
            if path.is_file() and not path.is_symlink():
                manifest_data = path.read_bytes()
            break
    katex_hashes = {} if manifest_data is None else _katex_manifest_hashes(manifest_data)
    for relative in sorted(set(relatives)):
        relative = _safe_archive_member(relative)
        path = root / relative
        if path.is_symlink():
            raise PrivacyViolation(
                relative,
                "symlink-artifact",
                _rule_id("tree:no-symlinks"),
            )
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise PrivacyViolation(
                relative,
                "missing-tree-candidate",
                _rule_id("tree:candidate"),
            )
        artifact_class = _classify_path(relative)
        if artifact_class is not None:
            raise PrivacyViolation(
                relative,
                artifact_class,
                _rule_id(f"path:{artifact_class}"),
            )
        data = path.read_bytes()
        if _is_audited_application_icon(relative, data):
            continue
        if _is_audited_katex_font(relative, data, katex_hashes):
            continue
        _scan_bytes(relative, data, denylist=denylist)


def audit_private(
    private_root: Path,
    public_root: Path,
    *,
    scan_tree_target: bool = False,
    archives: Iterable[Path] = (),
    scan_history_target: bool = False,
    expect_no_remote: bool = False,
    expected_remote: str | None = None,
    expected_remote_from_project: bool = False,
    private_git_reader: GitObjectReader | None = None,
    public_git_reader: object | None = None,
) -> None:
    archive_paths = tuple(archives)
    if not scan_tree_target and not archive_paths and not scan_history_target:
        raise ValueError("audit-private requires at least one scan target")
    denylist = derive_denylist(private_root, git_reader=private_git_reader)
    if scan_tree_target:
        validate_public_target(
            public_root,
            expect_no_remote=expect_no_remote,
            expected_remote=expected_remote,
            expected_remote_from_project=expected_remote_from_project,
            git_reader=public_git_reader,
        )
        scan_tree(public_root, denylist=denylist, git_reader=public_git_reader)
    for archive in archive_paths:
        scan_archive(archive, denylist=denylist)
    if scan_history_target:
        scan_history(
            public_root,
            denylist=denylist,
            expect_no_remote=expect_no_remote,
            expected_remote=expected_remote,
            expected_remote_from_project=expected_remote_from_project,
            git_reader=public_git_reader,
        )


def _add_remote_policy(parser: argparse.ArgumentParser) -> None:
    policies = parser.add_mutually_exclusive_group()
    policies.add_argument("--expect-no-remote", action="store_true")
    policies.add_argument("--expected-remote")
    policies.add_argument(
        "--expected-remote-from-project",
        action="store_true",
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="privacy_scan.py", allow_abbrev=False)
    commands = parser.add_subparsers(dest="mode", required=True)

    derive = commands.add_parser("derive-denylist", allow_abbrev=False)
    derive.add_argument("private_root", type=Path)
    derive.add_argument("output", type=Path)

    tree = commands.add_parser("tree", allow_abbrev=False)
    tree.add_argument("root", type=Path)
    tree.add_argument("--denylist", type=Path)
    _add_remote_policy(tree)

    archive = commands.add_parser("archive", allow_abbrev=False)
    archive.add_argument("archive", type=Path)
    archive.add_argument("--denylist", type=Path)

    history = commands.add_parser("history", allow_abbrev=False)
    history.add_argument("root", type=Path)
    history.add_argument("--denylist", type=Path)
    _add_remote_policy(history)

    audit = commands.add_parser("audit-private", allow_abbrev=False)
    audit.add_argument("private_root", type=Path)
    audit.add_argument("public_root", type=Path)
    audit.add_argument("--tree", dest="scan_tree_target", action="store_true")
    audit.add_argument("--archive", action="append", default=[], type=Path)
    audit.add_argument("--history", dest="scan_history_target", action="store_true")
    _add_remote_policy(audit)
    return parser


def _policy_arguments(arguments: argparse.Namespace) -> dict[str, object]:
    return {
        "expect_no_remote": bool(arguments.expect_no_remote),
        "expected_remote": arguments.expected_remote,
        "expected_remote_from_project": bool(
            arguments.expected_remote_from_project
        ),
    }


def main(
    argv: Iterable[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    git_reader_factory: Callable[[Path], object] = RealGitObjectReader,
) -> int:
    arguments = _argument_parser().parse_args(None if argv is None else tuple(argv))
    try:
        if arguments.mode == "derive-denylist":
            private_root = arguments.private_root.resolve()
            output = arguments.output.resolve()
            if output == private_root or output.is_relative_to(private_root):
                raise PrivacyViolation(
                    output.name,
                    "denylist-location",
                    _rule_id("derive:outside-private-root"),
                )
            private_reader = (
                git_reader_factory(private_root)
                if (private_root / ".git").is_dir()
                else None
            )
            publish_denylist(
                derive_denylist(private_root, git_reader=private_reader),
                output,
            )
            stdout.write("denylist written\n")
            return 0
        if arguments.mode == "archive":
            denylist = (
                None
                if arguments.denylist is None
                else load_denylist(arguments.denylist)
            )
            scan_archive(arguments.archive, denylist=denylist)
            stdout.write("privacy scan passed\n")
            return 0
        if arguments.mode in {"tree", "history"}:
            root = arguments.root.resolve()
            reader = git_reader_factory(root)
            denylist = (
                None
                if arguments.denylist is None
                else load_denylist(arguments.denylist)
            )
            policy = _policy_arguments(arguments)
            if arguments.mode == "tree":
                validate_public_target(root, git_reader=reader, **policy)
                scan_tree(root, denylist=denylist, git_reader=reader)
            else:
                scan_history(
                    root,
                    denylist=denylist,
                    git_reader=reader,
                    **policy,
                )
            stdout.write("privacy scan passed\n")
            return 0
        if arguments.mode == "audit-private":
            public_root = arguments.public_root.resolve()
            private_root = arguments.private_root.resolve()
            scans_public_git = (
                arguments.scan_tree_target or arguments.scan_history_target
            )
            private_reader = (
                git_reader_factory(private_root)
                if (private_root / ".git").is_dir()
                else None
            )
            public_reader = (
                git_reader_factory(public_root) if scans_public_git else None
            )
            audit_private(
                private_root,
                public_root,
                scan_tree_target=arguments.scan_tree_target,
                archives=arguments.archive,
                scan_history_target=arguments.scan_history_target,
                private_git_reader=private_reader,
                public_git_reader=public_reader,
                **_policy_arguments(arguments),
            )
            stdout.write("private-derived audit passed\n")
            return 0
        raise AssertionError("argparse accepted an unsupported mode")
    except PrivacyViolation as error:
        stderr.write(f"{error}\n")
        return 2
    except (GitReadError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile):
        stderr.write(
            "privacy scan failed "
            f"[rule:{_rule_id('scanner:sanitized-failure')}]\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
