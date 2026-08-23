from __future__ import annotations

from pathlib import Path
import json
import hashlib
import sqlite3
import stat
import subprocess
import tarfile
import zipfile
from contextlib import contextmanager
from io import StringIO
import tomllib
from dataclasses import dataclass

import pytest


def _mac_home(*parts: str) -> str:
    return "/" + "/".join(("Users", *parts))


def _linux_home(*parts: str) -> str:
    return "/" + "/".join(("home", *parts))


def _windows_home(*parts: str) -> str:
    return "C:" + "\\" + "\\".join(("Users", *parts))


def test_tree_scan_rejects_email_artifact_without_echoing_contents(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import PrivacyViolation, scan_tree

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    message = public / "inbox.eml"
    secret = "private-message-body"
    message.write_text(secret, encoding="utf-8")

    with pytest.raises(PrivacyViolation) as caught:
        scan_tree(public)

    rendered = str(caught.value)
    assert "inbox.eml" in rendered
    assert "email-artifact" in rendered
    assert "rule:" in rendered
    assert secret not in rendered


@pytest.mark.parametrize(
    ("name", "artifact_class"),
    [
        ("paper.pdf", "pdf-artifact"),
        ("state.sqlite3", "database-artifact"),
        ("state.sqlite3-wal", "database-journal"),
        ("collection.json", "collection-artifact"),
        ("reading-collection.json", "collection-artifact"),
        ("runtime.json", "runtime-artifact"),
        ("cache/raw-response.xml", "cache-artifact"),
        ("runtime/session.lock", "runtime-artifact"),
        ("bundle.zip", "archive-artifact"),
        ("id_ed25519", "private-key-artifact"),
        ("credentials.toml", "credential-artifact"),
        (".DS_Store", "editor-metadata"),
    ],
)
def test_tree_scan_rejects_disallowed_artifact_classes(
    tmp_path: Path,
    name: str,
    artifact_class: str,
) -> None:
    from scripts.privacy_scan import PrivacyViolation, scan_tree

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    artifact = public / name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"synthetic")

    with pytest.raises(PrivacyViolation, match=artifact_class):
        scan_tree(public)


@pytest.mark.parametrize(
    "private_text",
    [
        _mac_home("example", "Library", "Application Support", "private", "state.json"),
        _linux_home("example", ".config", "private", "state.json"),
        _windows_home("example", "Documents", "private", "state.json"),
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
        "github_token = " + "ghp_" + "1234567890abcdefghijklmnopqrstuv",
    ],
)
def test_tree_scan_rejects_generic_private_content(
    tmp_path: Path,
    private_text: str,
) -> None:
    from scripts.privacy_scan import PrivacyViolation, scan_tree

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    (public / "module.py").write_text(private_text, encoding="utf-8")

    with pytest.raises(PrivacyViolation, match="private-content"):
        scan_tree(public)


def test_external_denylist_match_is_casefolded_bounded_and_redacted(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import Denylist, PrivacyViolation, scan_tree

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    private_author = "Synthetic Private Author"
    source = public / "notes.txt"
    source.write_text(private_author.swapcase(), encoding="utf-8")
    denylist = Denylist.from_mapping({"author": [private_author]})

    with pytest.raises(PrivacyViolation) as caught:
        scan_tree(public, denylist=denylist)

    rendered = str(caught.value)
    assert "notes.txt" in rendered
    assert "denylist-author" in rendered
    assert private_author.casefold() not in rendered.casefold()

    source.write_text("Synthetic Private Authorship", encoding="utf-8")
    scan_tree(public, denylist=denylist)


def test_derivation_reads_authoritative_json_and_ignores_fixtures_and_abstracts(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import derive_denylist

    private = tmp_path / "private"
    (private / "data").mkdir(parents=True)
    (private / "tests/fixtures").mkdir(parents=True)
    (private / "data/library.json").write_text(
        json.dumps(
            {
                "papers": [
                    {
                        "arxiv_id": "2401.12345",
                        "title": "A Synthetic Private Result",
                        "authors": ["Ada Synthetic"],
                        "abstract": "abstract-only-secret",
                    }
                ],
                "keywords": ["private keyword"],
                "phrases": ["private phrase"],
                "seed_papers": ["hep-th/9901001"],
                "records": {
                    "2102.00001": {"related": "see also 2103.00002"}
                },
            }
        ),
        encoding="utf-8",
    )
    (private / "tests/fixtures/example.json").write_text(
        json.dumps({"arxiv_id": "2501.99999"}),
        encoding="utf-8",
    )
    (private / "personal-reading-list.tsv").write_text(
        "synthetic\n",
        encoding="utf-8",
    )

    denylist = derive_denylist(private)

    assert "2401.12345" in denylist.values("paper-id")
    assert "hep-th/9901001" in denylist.values("seed-id")
    assert "2102.00001" in denylist.values("paper-id")
    assert "2103.00002" in denylist.values("paper-id")
    assert "A Synthetic Private Result" in denylist.values("title")
    assert "Ada Synthetic" in denylist.values("author")
    assert "private keyword" in denylist.values("keyword")
    assert "private phrase" in denylist.values("phrase")
    assert "abstract-only-secret" not in denylist.all_values()
    assert "2501.99999" not in denylist.all_values()
    assert "personal-reading-list.tsv" in denylist.values("relative-filename")
    assert "data/library.json" not in denylist.values("relative-filename")


def test_derivation_splits_scalar_author_lists_into_exact_names(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import derive_denylist

    private = tmp_path / "private"
    (private / "data").mkdir(parents=True)
    (private / "data/library.json").write_text(
        json.dumps(
            {
                "papers": [
                    {
                        "arxiv_id": "2608.23456",
                        "authors": "Ada Synthetic, Bert Synthetic",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    denylist = derive_denylist(private)

    assert "Ada Synthetic" in denylist.values("author")
    assert "Bert Synthetic" in denylist.values("author")


def test_derivation_reads_committed_live_wal_without_mutating_private_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.privacy_scan as privacy_scan

    real_temporary_directory = privacy_scan.tempfile.TemporaryDirectory
    temporary_paths: list[Path] = []
    temporary_modes: list[tuple[int, dict[str, int]]] = []

    class ObservedTemporaryDirectory:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._delegate = real_temporary_directory(*args, **kwargs)

        def __enter__(self) -> str:
            temporary = Path(self._delegate.__enter__())
            temporary_paths.append(temporary)
            return str(temporary)

        def __exit__(self, *args: object) -> object:
            temporary = temporary_paths[-1]
            temporary_modes.append(
                (
                    stat.S_IMODE(temporary.stat().st_mode),
                    {
                        path.name: stat.S_IMODE(path.stat().st_mode)
                        for path in temporary.iterdir()
                        if path.is_file()
                    },
                )
            )
            return self._delegate.__exit__(*args)

    monkeypatch.setattr(
        privacy_scan.tempfile,
        "TemporaryDirectory",
        ObservedTemporaryDirectory,
    )

    private = tmp_path / "private"
    database = private / "data/library.sqlite3"
    database.parent.mkdir(parents=True)
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute(
            "CREATE TABLE papers(arxiv_id TEXT, title TEXT, authors TEXT)"
        )
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute(
            "INSERT INTO papers VALUES (?, ?, ?)",
            ("2608.12345", "Committed WAL Title", "WAL Author"),
        )
        writer.commit()
        wal = database.with_name(database.name + "-wal")
        assert wal.stat().st_size > 0
        before = {
            path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in database.parent.iterdir()
        }

        denylist = privacy_scan.derive_denylist(private)

        after = {
            path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in database.parent.iterdir()
        }
        assert "2608.12345" in denylist.values("paper-id")
        assert "Committed WAL Title" in denylist.values("title")
        assert "data/library.sqlite3" not in denylist.values(
            "relative-filename"
        )
        assert before == after
        assert temporary_modes == [
            (
                0o700,
                {
                    "library.sqlite3": 0o600,
                    "library.sqlite3-shm": 0o600,
                    "library.sqlite3-wal": 0o600,
                },
            )
        ]
        assert all(not temporary.exists() for temporary in temporary_paths)
    finally:
        writer.close()


def test_derivation_parses_literal_python_toml_and_restricted_yaml(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import derive_denylist

    private = tmp_path / "private"
    (private / "ignored").mkdir(parents=True)
    (private / "ignored/preferences.py").write_text(
        "PREFERRED_AUTHORS = ['Python Author']\n"
        "SEED_PAPERS = {'2301.00001'}\n"
        "PREFERRED_PAPERS = ['2301.00002']\n"
        "INTERESTS = ['python interest']\n"
        "EXAMPLE_IDS = ['2301.99999']\n",
        encoding="utf-8",
    )
    (private / "profile.toml").write_text(
        'keywords = ["toml keyword"]\nphrases = ["toml phrase"]\n',
        encoding="utf-8",
    )
    (private / "interests.yaml").write_text(
        "authors:\n  - YAML Author\nkeywords: yaml keyword\n",
        encoding="utf-8",
    )

    denylist = derive_denylist(private)

    assert "Python Author" in denylist.values("author")
    assert "2301.00001" in denylist.values("seed-id")
    assert "2301.00002" in denylist.values("paper-id")
    assert "python interest" in denylist.values("keyword")
    assert "2301.99999" not in denylist.all_values()
    assert "toml keyword" in denylist.values("keyword")
    assert "toml phrase" in denylist.values("phrase")
    assert "YAML Author" in denylist.values("author")
    assert "yaml keyword" in denylist.values("keyword")
    assert "profile.toml" not in denylist.values("relative-filename")
    assert "interests.yaml" not in denylist.values("relative-filename")


def test_derivation_covers_key_scoped_python_preferences_without_seed_misclassification(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import derive_denylist

    private = tmp_path / "private"
    private.mkdir()
    (private / "arxiv_digest.py").write_text(
        "IMPORTANT_AUTHOR_SURNAMES = {'Noether', 'Serre'}\n"
        "SEED_HIGH_KW = ['chromatic homotopy']\n"
        "SEED_MED_KW = ['motivic spectra']\n"
        "SEED_PAPER_IDS = ['2608.34567']\n",
        encoding="utf-8",
    )

    denylist = derive_denylist(private)

    assert {"Noether", "Serre"} <= denylist.values("author")
    assert {"chromatic homotopy", "motivic spectra"} <= denylist.values(
        "keyword"
    )
    assert "2608.34567" in denylist.values("seed-id")
    assert "chromatic homotopy" not in denylist.values("seed-id")
    assert "motivic spectra" not in denylist.values("seed-id")
    assert "arxiv_digest.py" not in denylist.values("relative-filename")


def test_derivation_inspects_sqlite_read_only_by_schema(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import derive_denylist

    private = tmp_path / "private"
    database = private / "data/state.sqlite3"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE articles(arxiv_id TEXT, title TEXT, abstract TEXT);"
        "CREATE TABLE article_authors(arxiv_id TEXT, name TEXT);"
        "CREATE TABLE profile_keywords(keyword TEXT);"
    )
    connection.execute(
        "INSERT INTO articles VALUES (?, ?, ?)",
        ("2201.01234", "SQLite Private Title", "sqlite abstract secret"),
    )
    connection.execute(
        "INSERT INTO article_authors VALUES (?, ?)",
        ("2201.01234", "SQLite Author"),
    )
    connection.execute(
        "INSERT INTO profile_keywords VALUES (?)",
        ("sqlite keyword",),
    )
    connection.commit()
    connection.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    denylist = derive_denylist(private)

    assert "2201.01234" in denylist.values("paper-id")
    assert "SQLite Private Title" in denylist.values("title")
    assert "SQLite Author" in denylist.values("author")
    assert "sqlite keyword" in denylist.values("keyword")
    assert "sqlite abstract secret" not in denylist.all_values()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not database.with_name(database.name + "-wal").exists()
    assert "data/state.sqlite3" not in denylist.values("relative-filename")


def test_derivation_collects_injected_git_identities_metadata_and_paths(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import GitObject, derive_denylist

    @dataclass
    class FakeGitReader:
        def local_identities(self) -> tuple[str, ...]:
            return ("Local Person <local@example.invalid>",)

        def effective_identities(self) -> tuple[str, ...]:
            return ("Environment Person <environment@example.invalid> 1 +0000",)

        def reachable_objects(self) -> tuple[GitObject, ...]:
            return (
                GitObject(
                    oid="a" * 40,
                    object_type="commit",
                    data=(
                        "author Historic Person <historic@example.invalid> 1 +0000\n"
                        f"message mentions {_mac_home('history', 'private', 'location')}\n"
                    ).encode("utf-8"),
                ),
                GitObject(
                    oid="b" * 40,
                    object_type="blob",
                    path="personal-library-export.json",
                    data=b"{}",
                ),
            )

    private = tmp_path / "private"
    private.mkdir()

    denylist = derive_denylist(private, git_reader=FakeGitReader())

    assert "Local Person" in denylist.values("git-identity")
    assert "local@example.invalid" in denylist.values("email")
    assert "Environment Person" in denylist.values("git-identity")
    assert "historic@example.invalid" in denylist.values("email")
    assert _mac_home("history", "private", "location") in denylist.values(
        "absolute-path"
    )
    assert "personal-library-export.json" in denylist.values("relative-filename")


def test_derivation_collects_every_non_generic_distinctive_relative_path(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import GitObject, derive_denylist

    @dataclass
    class ConventionalWorkflowHistory:
        def local_identities(self) -> tuple[str, ...]:
            return ()

        def effective_identities(self) -> tuple[str, ...]:
            return ()

        def reachable_objects(self) -> tuple[GitObject, ...]:
            return (
                *(
                    GitObject(str(index) * 40, "tree", b"", path)
                    for index, path in enumerate(
                        (
                            ".github",
                            ".github/workflows",
                            ".github/workflows/tests.yml",
                            "",
                        ),
                        start=1,
                    )
                ),
                GitObject(
                    "5" * 40,
                    "blob",
                    b"",
                    "docs/superpowers/plans/2026-08-22-public-arxiv-digest.md",
                ),
            )

    private = tmp_path / "private"
    distinctive = private / "research-notes/elliptic-roadmap.tsv"
    distinctive.parent.mkdir(parents=True)
    distinctive.write_text("synthetic\n", encoding="utf-8")
    (private / "README.md").write_text("generic\n", encoding="utf-8")
    (private / ".DS_Store").write_bytes(b"generic operating-system metadata")
    workflow = private / ".github/workflows/tests.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: tests\n", encoding="utf-8")

    denylist = derive_denylist(
        private,
        git_reader=ConventionalWorkflowHistory(),
    )

    assert (
        "research-notes/elliptic-roadmap.tsv"
        in denylist.values("relative-filename")
    )
    assert "README.md" not in denylist.values("relative-filename")
    assert ".DS_Store" not in denylist.values("relative-filename")
    assert not (
        {".github", ".github/workflows", ".github/workflows/tests.yml"}
        & denylist.values("relative-filename")
    )
    assert "." not in denylist.values("relative-filename")
    assert not any(
        "2026-08-22-public-arxiv-digest" in value
        for value in denylist.values("relative-filename")
    )


def test_denylist_publication_is_atomic_mode_0600_and_round_trips(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import (
        Denylist,
        load_denylist,
        publish_denylist,
    )

    destination = tmp_path / "external/denylist.json"
    denylist = Denylist.from_mapping(
        {"author": ["Round Trip Author"], "paper-id": ["2101.12345"]}
    )

    publish_denylist(denylist, destination)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert load_denylist(destination) == denylist
    assert list(destination.parent.iterdir()) == [destination]


def test_ambiguous_single_word_rules_match_only_their_structured_fields(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import Denylist, PrivacyViolation, scan_tree

    denylist = Denylist.from_mapping(
        {
            "keyword": ["local", "categories"],
            "author": ["May"],
        }
    )
    prose = tmp_path / "prose"
    prose.mkdir()
    (prose / "README.md").write_text(
        "Local categories may be reviewed safely.\n",
        encoding="utf-8",
    )

    scan_tree(prose, denylist=denylist)

    keyword_record = tmp_path / "keyword-record"
    keyword_record.mkdir()
    (keyword_record / "fixture.json").write_text(
        '{"keywords":["local"]}',
        encoding="utf-8",
    )
    with pytest.raises(PrivacyViolation, match="denylist-keyword"):
        scan_tree(keyword_record, denylist=denylist)

    author_record = tmp_path / "author-record"
    author_record.mkdir()
    (author_record / "fixture.json").write_text(
        '{"authors":["May"]}',
        encoding="utf-8",
    )
    with pytest.raises(PrivacyViolation, match="denylist-author"):
        scan_tree(author_record, denylist=denylist)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_archive_scan_streams_members_and_applies_path_rules(
    tmp_path: Path,
    kind: str,
) -> None:
    from scripts.privacy_scan import PrivacyViolation, scan_archive

    if kind == "wheel":
        archive = tmp_path / "package.whl"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("arxiv_digest/private.eml", b"synthetic")
    else:
        archive = tmp_path / "package.tar.gz"
        payload = tmp_path / "private.eml"
        payload.write_bytes(b"synthetic")
        with tarfile.open(archive, "w:gz") as output:
            output.add(payload, arcname="package/private.eml")

    with pytest.raises(PrivacyViolation, match="email-artifact"):
        scan_archive(archive)


def test_project_remote_policy_accepts_only_exact_confirmed_ssh_origin(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import validate_public_target

    @dataclass
    class FakeRemoteReader:
        urls: dict[str, tuple[str, ...]]

        def remotes(self) -> dict[str, tuple[str, ...]]:
            return self.urls

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    (public / "pyproject.toml").write_text(
        "[project]\nname = 'arxiv-digest'\n"
        "[project.urls]\n"
        "Repository = 'https://github.com/yuzhangmath/arxiv-digest'\n",
        encoding="utf-8",
    )

    validate_public_target(
        public,
        expected_remote_from_project=True,
        git_reader=FakeRemoteReader(
            {"origin": ("git@github.com:yuzhangmath/arxiv-digest.git",)}
        ),
    )


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:yuzhangmath/arxiv-digest",
        "ssh://git@github.com/yuzhangmath/arxiv-digest.git",
        "root@github.com:yuzhangmath/arxiv-digest.git",
        "git@example.com:yuzhangmath/arxiv-digest.git",
        "git@github.com:yuzhangmath/../arxiv-digest.git",
        "git@github.com:someone/arxiv-digest.git",
        "git@github.com:yuzhangmath/another.git",
    ],
)
def test_project_remote_policy_rejects_noncanonical_ssh_forms(
    tmp_path: Path,
    remote: str,
) -> None:
    from scripts.privacy_scan import PrivacyViolation, validate_public_target

    @dataclass
    class FakeRemoteReader:
        def remotes(self) -> dict[str, tuple[str, ...]]:
            return {"origin": (remote,)}

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    (public / "pyproject.toml").write_text(
        "[project]\nname='arxiv-digest'\n"
        "[project.urls]\n"
        "Repository='https://github.com/yuzhangmath/arxiv-digest'\n",
        encoding="utf-8",
    )

    with pytest.raises(PrivacyViolation, match="remote-policy"):
        validate_public_target(
            public,
            expected_remote_from_project=True,
            git_reader=FakeRemoteReader(),
        )


def test_remote_policy_requires_exactly_one_policy_and_only_origin(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import PrivacyViolation, validate_public_target

    @dataclass
    class FakeRemoteReader:
        urls: dict[str, tuple[str, ...]]

        def remotes(self) -> dict[str, tuple[str, ...]]:
            return self.urls

    public = tmp_path / "arxiv-digest"
    public.mkdir()

    with pytest.raises(PrivacyViolation, match="remote-policy"):
        validate_public_target(public, git_reader=FakeRemoteReader({}))
    with pytest.raises(PrivacyViolation, match="remote-policy"):
        validate_public_target(
            public,
            expect_no_remote=True,
            expected_remote="https://github.com/yuzhangmath/arxiv-digest.git",
            git_reader=FakeRemoteReader({}),
        )
    with pytest.raises(PrivacyViolation, match="remote-policy"):
        validate_public_target(
            public,
            expected_remote="https://github.com/yuzhangmath/arxiv-digest.git",
            git_reader=FakeRemoteReader(
                {
                    "origin": (
                        "https://github.com/yuzhangmath/arxiv-digest.git",
                    ),
                    "upstream": ("https://example.invalid/upstream.git",),
                }
            ),
        )
    validate_public_target(
        public,
        expect_no_remote=True,
        git_reader=FakeRemoteReader({}),
    )
    validate_public_target(
        public,
        expected_remote="https://github.com/yuzhangmath/arxiv-digest.git",
        git_reader=FakeRemoteReader(
            {"origin": ("https://github.com/yuzhangmath/arxiv-digest.git",)}
        ),
    )
    with pytest.raises(PrivacyViolation, match="remote-policy"):
        validate_public_target(
            public,
            expected_remote="https://github.com/yuzhangmath/different.git",
            git_reader=FakeRemoteReader(
                {"origin": ("https://github.com/yuzhangmath/different.git",)}
            ),
        )


@pytest.mark.parametrize(
    ("object_type", "path", "data"),
    [
        ("blob", "src/module.py", b"History Private Value"),
        ("commit", None, b"committer Safe <safe@example.invalid>\nHistory Private Value"),
        ("tag", None, b"tag release\nHistory Private Value"),
        ("tree", "History Private Value/file.py", b""),
    ],
)
def test_history_scan_checks_every_reachable_object_surface(
    tmp_path: Path,
    object_type: str,
    path: str | None,
    data: bytes,
) -> None:
    from scripts.privacy_scan import (
        Denylist,
        GitObject,
        PrivacyViolation,
        scan_history,
    )

    @dataclass
    class FakeGitReader:
        def remotes(self) -> dict[str, tuple[str, ...]]:
            return {}

        def effective_identities(self) -> tuple[str, ...]:
            return ("Safe Person <safe@example.invalid> 1 +0000",)

        def reachable_objects(self) -> tuple[GitObject, ...]:
            return (GitObject("a" * 40, object_type, data, path),)

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    denylist = Denylist.from_mapping({"private-value": ["History Private Value"]})

    with pytest.raises(PrivacyViolation, match="denylist-private-value"):
        scan_history(
            public,
            denylist=denylist,
            expect_no_remote=True,
            git_reader=FakeGitReader(),
        )


def test_history_scan_checks_every_commit_tree_path_alias(tmp_path: Path) -> None:
    from scripts.privacy_scan import Denylist, PrivacyViolation, scan_history

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    (public / "a-copy").mkdir()
    (public / "z-distinctive-copy").mkdir()
    for directory in ("a-copy", "z-distinctive-copy"):
        (public / directory / "notes.txt").write_text("public", encoding="utf-8")
    subprocess.run(("git", "init", "-q"), cwd=public, check=True)
    for key, value in (
        ("user.name", "Public Fixture"),
        ("user.email", "public@example.invalid"),
    ):
        subprocess.run(
            ("git", "config", "--local", key, value),
            cwd=public,
            check=True,
        )
    subprocess.run(("git", "add", "."), cwd=public, check=True)
    subprocess.run(
        ("git", "commit", "-qm", "fixture"),
        cwd=public,
        check=True,
    )

    with pytest.raises(PrivacyViolation, match="denylist-relative-filename"):
        scan_history(
            public,
            denylist=Denylist.from_mapping(
                {"relative-filename": ["z-distinctive-copy/notes.txt"]}
            ),
            expect_no_remote=True,
        )


def test_history_scan_checks_effective_identity_with_empty_history(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import Denylist, PrivacyViolation, scan_history

    @dataclass
    class FakeGitReader:
        def remotes(self) -> dict[str, tuple[str, ...]]:
            return {}

        def effective_identities(self) -> tuple[str, ...]:
            return ("Private Environment Person <safe@example.invalid> 1 +0000",)

        def reachable_objects(self) -> tuple[object, ...]:
            return ()

    public = tmp_path / "arxiv-digest"
    public.mkdir()

    with pytest.raises(PrivacyViolation, match="denylist-git-identity"):
        scan_history(
            public,
            denylist=Denylist.from_mapping(
                {"git-identity": ["Private Environment Person"]}
            ),
            expect_no_remote=True,
            git_reader=FakeGitReader(),
        )


def test_tree_scan_uses_injected_git_candidate_set(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import scan_tree

    @dataclass
    class FakeGitReader:
        def candidate_paths(self) -> tuple[str, ...]:
            return ("safe.txt",)

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    (public / "safe.txt").write_text("public", encoding="utf-8")
    (public / "ignored.eml").write_text("ignored", encoding="utf-8")

    scan_tree(public, git_reader=FakeGitReader())


def test_generic_secret_rule_allows_source_code_that_generates_a_token(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import scan_tree

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    (public / "server.py").write_text(
        "token = secrets.token_urlsafe(32)\n"
        "assert '/Users/' not in diagnostic\n",
        encoding="utf-8",
    )

    scan_tree(public)


def test_privacy_violation_propagates_through_context_managers() -> None:
    from scripts.privacy_scan import PrivacyViolation

    @contextmanager
    def boundary():
        yield

    with pytest.raises(PrivacyViolation):
        with boundary():
            raise PrivacyViolation("file", "artifact", "opaque")


def test_artifact_rules_allow_protocol_fixture_named_for_a_token_error(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import scan_tree

    public = tmp_path / "arxiv-digest"
    fixture = public / "tests/fixtures/bad-resumption-token.xml"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("<error code='badResumptionToken'/>", encoding="utf-8")

    scan_tree(public)


@pytest.mark.parametrize("contains_private_value", [False, True])
def test_audit_private_keeps_derived_values_in_memory_on_success_and_failure(
    tmp_path: Path,
    contains_private_value: bool,
) -> None:
    from scripts.privacy_scan import PrivacyViolation, audit_private

    @dataclass
    class FakePublicGitReader:
        public_file: str

        def remotes(self) -> dict[str, tuple[str, ...]]:
            return {}

        def candidate_paths(self) -> tuple[str, ...]:
            return (self.public_file,)

    private = tmp_path / "private"
    private.mkdir()
    private_value = "In Memory Private Author"
    (private / "profile.json").write_text(
        json.dumps({"authors": [private_value]}),
        encoding="utf-8",
    )
    public = tmp_path / "arxiv-digest"
    public.mkdir()
    public_file = "README.md"
    (public / public_file).write_text(
        private_value if contains_private_value else "public text",
        encoding="utf-8",
    )
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    if contains_private_value:
        with pytest.raises(PrivacyViolation) as caught:
            audit_private(
                private,
                public,
                scan_tree_target=True,
                expect_no_remote=True,
                public_git_reader=FakePublicGitReader(public_file),
            )
        assert private_value not in str(caught.value)
    else:
        audit_private(
            private,
            public,
            scan_tree_target=True,
            expect_no_remote=True,
            public_git_reader=FakePublicGitReader(public_file),
        )

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert after == before
    assert not list(tmp_path.rglob("*denylist*"))


def test_command_modes_are_explicit_and_failure_output_is_redacted(
    tmp_path: Path,
) -> None:
    from scripts.privacy_scan import main

    private = tmp_path / "private"
    private.mkdir()
    private_value = "CLI Private Author"
    (private / "profile.json").write_text(
        json.dumps({"authors": [private_value]}),
        encoding="utf-8",
    )
    output = tmp_path / "external/denylist.json"
    stdout = StringIO()
    stderr = StringIO()

    assert main(
        ["derive-denylist", str(private), str(output)],
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert output.exists()
    assert private_value not in stdout.getvalue()
    assert private_value not in stderr.getvalue()

    public = tmp_path / "arxiv-digest"
    public.mkdir()
    (public / "README.md").write_text(_mac_home("leak", "file"), encoding="utf-8")
    stderr = StringIO()
    assert main(
        ["tree", str(public), "--expect-no-remote"],
        stdout=StringIO(),
        stderr=stderr,
        git_reader_factory=lambda _: type(
            "Reader",
            (),
            {
                "remotes": lambda self: {},
                "candidate_paths": lambda self: ("README.md",),
            },
        )(),
    ) == 2
    assert "README.md" in stderr.getvalue()
    assert _mac_home("leak", "file") not in stderr.getvalue()


def test_public_documentation_uses_confirmed_install_identity_and_no_email_import() -> None:
    project_root = Path(__file__).resolve().parents[2]
    readme = (project_root / "README.md").read_text(encoding="utf-8")
    project = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    expected_commands = (
        "pipx install git+https://github.com/yuzhangmath/arxiv-digest.git\n"
        "arxiv-digest init"
    )

    assert readme.index(expected_commands) < readme.index("## Contents")
    install_url = expected_commands.splitlines()[0].removeprefix("pipx install git+")
    assert install_url.removesuffix(".git") == project["project"]["urls"][
        "Repository"
    ]
    assert project["project"]["urls"]["Repository"] == (
        "https://github.com/yuzhangmath/arxiv-digest"
    )
    assert "There is no `.eml` import." in readme
    assert "import-eml" not in readme


def test_ci_privacy_scan_uses_checkout_origin_exactly() -> None:
    project_root = Path(__file__).resolve().parents[2]
    workflow = (project_root / ".github/workflows/tests.yml").read_text(
        encoding="utf-8"
    )

    assert (
        'python scripts/privacy_scan.py tree . --expected-remote '
        '"${{ github.server_url }}/${{ github.repository }}"'
    ) in workflow
