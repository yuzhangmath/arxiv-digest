from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from arxiv_digest.models import AnnounceType
from arxiv_digest.rate_limit import HttpResponse, Interface
from arxiv_digest.sources.atom import AtomParseError, AtomSource, parse_atom_batch


FIXTURES = Path(__file__).parents[2] / "fixtures" / "atom"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_atom_batch_normalizes_a_new_announcement() -> None:
    payload = fixture("current-mixed.xml")

    batch = parse_atom_batch(payload, "cs.CL")

    assert batch.category == "cs.CL"
    assert batch.mailing_date == date(2026, 8, 21)
    assert batch.fetched_at == datetime(
        2026,
        8,
        22,
        1,
        30,
        tzinfo=timezone.utc,
    )
    assert batch.raw_sha256 == hashlib.sha256(payload).hexdigest()
    assert len(batch.entries) == 4
    entry = batch.entries[0]
    assert entry.position == 0
    assert entry.announce_type is AnnounceType.NEW
    assert entry.mailing_date == batch.mailing_date
    assert entry.metadata.arxiv_id == "2608.10001"
    assert entry.metadata.title == "Lantern Protocols for Quiet Networks"
    assert entry.metadata.authors == ("Nora Example", "Quinn Sample")
    assert entry.metadata.abstract == (
        "A fictional abstract about reliable lantern protocols."
    )
    assert entry.metadata.primary_category == "cs.CL"
    assert entry.metadata.categories == ("cs.CL", "stat.ML")
    assert entry.metadata.comments == "12 synthetic pages"
    assert entry.metadata.journal_ref == (
        "Journal of Fictional Systems 1 (2026)"
    )
    assert entry.metadata.doi == "10.0000/example.10001"
    assert entry.version.number == 1
    assert entry.version.submitted_at == datetime(
        2026,
        8,
        22,
        0,
        0,
        tzinfo=timezone.utc,
    )


def test_cross_announcement_follows_new_in_source_order() -> None:
    batch = parse_atom_batch(fixture("current-mixed.xml"), "cs.CL")

    entry = batch.entries[1]
    assert entry.position == 1
    assert entry.announce_type is AnnounceType.CROSS
    assert entry.metadata.arxiv_id == "2608.10002"
    assert entry.metadata.primary_category == "stat.ML"


def test_replacement_announcement_is_preserved() -> None:
    batch = parse_atom_batch(fixture("current-mixed.xml"), "cs.CL")

    entry = batch.entries[2]
    assert entry.position == 2
    assert entry.announce_type is AnnounceType.REPLACE
    assert entry.metadata.arxiv_id == "2608.10003"
    assert entry.version.number == 2


def test_later_version_uses_entry_updated_timestamp() -> None:
    batch = parse_atom_batch(fixture("current-mixed.xml"), "cs.CL")

    assert batch.entries[2].version.submitted_at == datetime(
        2026,
        8,
        22,
        0,
        40,
        tzinfo=timezone.utc,
    )


def test_source_provided_replace_cross_is_preserved() -> None:
    batch = parse_atom_batch(fixture("current-mixed.xml"), "cs.CL")

    entry = batch.entries[3]
    assert entry.position == 3
    assert entry.announce_type is AnnounceType.REPLACE_CROSS
    assert entry.metadata.arxiv_id == "2608.10004"
    assert entry.version.number == 3


def test_empty_current_feed_is_a_normal_batch() -> None:
    payload = fixture("current-empty.xml")

    batch = parse_atom_batch(payload, "stat.ML")

    assert batch.category == "stat.ML"
    assert batch.mailing_date == date(2026, 8, 22)
    assert batch.entries == ()
    assert batch.raw_sha256 == hashlib.sha256(payload).hexdigest()


def test_namespace_prefix_changes_do_not_change_parsing() -> None:
    payload = fixture("current-mixed.xml").replace(
        b"xmlns:arxiv=",
        b"xmlns:paper=",
    ).replace(b"arxiv:", b"paper:")

    batch = parse_atom_batch(payload, "cs.CL")

    assert tuple(entry.announce_type for entry in batch.entries) == (
        AnnounceType.NEW,
        AnnounceType.CROSS,
        AnnounceType.REPLACE,
        AnnounceType.REPLACE_CROSS,
    )


def test_missing_announcement_type_is_rejected() -> None:
    payload = fixture("current-mixed.xml").replace(
        b"    <arxiv:announce_type>New</arxiv:announce_type>\n",
        b"",
        1,
    )

    with pytest.raises(AtomParseError, match="missing announcement type"):
        parse_atom_batch(payload, "cs.CL")


def test_invalid_versioned_entry_id_is_rejected() -> None:
    payload = fixture("current-mixed.xml").replace(
        b"2608.10001v1",
        b"2608.10001v0",
        1,
    )

    with pytest.raises(AtomParseError, match="invalid Atom entry ID"):
        parse_atom_batch(payload, "cs.CL")


def test_new_york_mailing_date_changes_at_local_midnight() -> None:
    payload = fixture("current-empty.xml").replace(
        b"2026-08-23T03:59:59Z",
        b"2026-08-23T04:00:00Z",
    )

    batch = parse_atom_batch(payload, "stat.ML")

    assert batch.mailing_date == date(2026, 8, 23)


def test_unknown_announcement_type_is_rejected() -> None:
    payload = fixture("current-mixed.xml").replace(
        b">New</arxiv:announce_type>",
        b">Surprise</arxiv:announce_type>",
        1,
    )

    with pytest.raises(AtomParseError, match="unknown announcement type"):
        parse_atom_batch(payload, "cs.CL")


def test_malformed_atom_xml_is_rejected_with_typed_error() -> None:
    with pytest.raises(AtomParseError, match="malformed Atom XML"):
        parse_atom_batch(b"<feed>", "cs.CL")


def test_atom_source_requests_xml_through_shared_client() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Interface, str, int | None]] = []

        def get(
            self,
            url: str,
            *,
            interface: Interface,
            accept: str,
            max_bytes: int | None = None,
        ) -> HttpResponse:
            self.calls.append((url, interface, accept, max_bytes))
            return HttpResponse(
                status=200,
                final_url=url,
                headers={"content-type": "application/atom+xml"},
                body=fixture("current-empty.xml"),
                observed_at=datetime(2026, 8, 23, 4, tzinfo=timezone.utc),
            )

    client = RecordingClient()
    source = AtomSource(
        client,
        base_url="https://rss.arxiv.org/atom",
        max_feed_bytes=123_456,
    )

    batch = source.fetch("cs.CL")

    assert batch.entries == ()
    assert client.calls == [
        (
            "https://rss.arxiv.org/atom/cs.CL",
            Interface.ATOM,
            "application/atom+xml, application/xml;q=0.9, text/xml;q=0.8",
            123_456,
        )
    ]


def test_atom_source_forwards_cooperative_cancellation() -> None:
    cancellation = Event()

    class RecordingClient:
        def __init__(self) -> None:
            self.cancelled = None

        def get(self, url: str, **options: object) -> HttpResponse:
            self.cancelled = options.get("cancelled")
            return HttpResponse(
                status=200,
                final_url=url,
                headers={"content-type": "application/atom+xml"},
                body=fixture("current-empty.xml"),
                observed_at=datetime(2026, 8, 23, 4, tzinfo=timezone.utc),
            )

    client = RecordingClient()
    cancelled = cancellation.is_set
    AtomSource(client).fetch("cs.CL", cancelled=cancelled)

    assert client.cancelled is cancelled


def test_versioned_legacy_entry_id_is_preserved() -> None:
    payload = fixture("current-mixed.xml").replace(
        b"2608.10001v1",
        b"hep-th/9901001v1",
        1,
    )

    batch = parse_atom_batch(payload, "cs.CL")

    assert batch.entries[0].metadata.arxiv_id == "hep-th/9901001"
    assert batch.entries[0].version.number == 1


def test_invalid_feed_timestamp_is_rejected_with_typed_error() -> None:
    payload = fixture("current-empty.xml").replace(
        b"2026-08-23T03:59:59Z",
        b"not-a-timestamp",
    )

    with pytest.raises(AtomParseError, match="invalid Atom updated"):
        parse_atom_batch(payload, "stat.ML")
