from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Event

import pytest

from arxiv_digest.models import AnnounceType, EvidenceSource
from arxiv_digest.rate_limit import HttpResponse, Interface
from arxiv_digest.sources.atom import (
    AtomParseError,
    AtomSource,
    atom_observations,
    parse_atom_batch,
)


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
    assert entry.announced_version == 1
    assert entry.published_at == datetime(
        2026,
        8,
        22,
        0,
        0,
        tzinfo=timezone.utc,
    )


def test_atom_batch_normalizes_globally_keyed_hidden_observations() -> None:
    batch = parse_atom_batch(fixture("current-mixed.xml"), "cs.CL")

    observations = atom_observations(batch)

    first = observations[0]
    assert first.source_key == "atom:cs.CL:2608.10001:v1"
    assert first.arxiv_id == "2608.10001"
    assert first.source is EvidenceSource.ATOM
    assert first.category == "cs.CL"
    assert first.announce_type is AnnounceType.NEW
    assert first.daily_list_date == batch.mailing_date
    assert first.announced_version == 1
    assert first.list_position == 0
    assert first.oai_datestamp is None
    assert first.response_sha256 == batch.raw_sha256
    assert first.observed_at == batch.fetched_at
    assert atom_observations(batch) == observations


def test_production_atom_contract_is_normalized() -> None:
    payload = fixture("current-production.xml")

    batch = parse_atom_batch(payload, "math.AC")

    assert batch.mailing_date == date(2026, 8, 24)
    assert batch.fetched_at == datetime(
        2026,
        8,
        24,
        4,
        32,
        tzinfo=timezone.utc,
    )
    assert len(batch.entries) == 1
    entry = batch.entries[0]
    assert entry.metadata.arxiv_id == "2608.18078"
    assert entry.announced_version == 2
    assert entry.published_at == datetime(
        2026,
        8,
        24,
        4,
        tzinfo=timezone.utc,
    )
    assert entry.metadata.title == "Production-Shaped Synthetic Example"
    assert entry.metadata.authors == ("Ada Fixture", "Ben Example")
    assert entry.metadata.abstract == (
        "A synthetic production-shaped abstract."
    )
    assert entry.metadata.primary_category == "math.AC"
    assert entry.metadata.categories == ("math.AC", "math.AT", "math.RT")
    assert entry.metadata.journal_ref == "Synthetic Journal 1 (2026)"
    assert entry.metadata.doi == "10.0000/production.fixture"
    assert entry.announce_type is AnnounceType.NEW


def test_dc_creator_keeps_commas_inside_affiliations() -> None:
    payload = fixture("current-production.xml").replace(
        b"Ada Fixture, Ben Example",
        b"Ada Fixture (Fixture Lab, Example University), Ben Example",
    )

    batch = parse_atom_batch(payload, "math.AC")

    assert batch.entries[0].metadata.authors == (
        "Ada Fixture (Fixture Lab, Example University)",
        "Ben Example",
    )


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (b"arXiv:2608.18078v2", b"arXiv:2608.18079v2"),
        (b"Announce Type: new", b"Announce Type: cross"),
    ),
)
def test_production_summary_header_must_match_structured_fields(
    old: bytes,
    new: bytes,
) -> None:
    payload = fixture("current-production.xml").replace(old, new, 1)

    with pytest.raises(AtomParseError, match="summary header"):
        parse_atom_batch(payload, "math.AC")


def test_production_feed_rejects_mixed_published_dates() -> None:
    payload = fixture("current-production.xml")
    second = (
        b"<entry>"
        b"<id>oai:arXiv.org:2608.18079v1</id>"
        b"<published>2026-08-25T04:00:00Z</published>"
        b"<title>Second synthetic entry</title>"
        b"<summary>arXiv:2608.18079v1 Announce Type: new "
        b"Abstract: Another synthetic abstract.</summary>"
        b"<dc:creator xmlns:dc=\"http://purl.org/dc/elements/1.1/\">"
        b"Cora Fixture</dc:creator>"
        b"<category term=\"math.AC\" />"
        b"<arxiv:announce_type "
        b"xmlns:arxiv=\"http://arxiv.org/schemas/atom\">"
        b"new</arxiv:announce_type>"
        b"</entry>"
    )
    payload = payload.replace(b"</feed>", second + b"</feed>")

    with pytest.raises(AtomParseError, match="mixed Atom published dates"):
        parse_atom_batch(payload, "math.AC")


@pytest.mark.parametrize(
    ("line", "message"),
    (
        (b"    <dc:creator>Ada Fixture, Ben Example</dc:creator>\n", "authors"),
        (b"    <category term=\"math.AC\" />\n", "categories"),
    ),
)
def test_production_feed_requires_authors_and_categories(
    line: bytes,
    message: str,
) -> None:
    payload = fixture("current-production.xml").replace(line, b"", 1)
    if message == "categories":
        payload = payload.replace(
            b"    <category term=\"math.AT\" />\n",
            b"",
        ).replace(
            b"    <category term=\"math.RT\" />\n",
            b"",
        )

    with pytest.raises(AtomParseError, match=message):
        parse_atom_batch(payload, "math.AC")


def test_production_feed_rejects_unknown_action() -> None:
    payload = fixture("current-production.xml").replace(b"new", b"surprise")

    with pytest.raises(AtomParseError, match="unknown announcement type"):
        parse_atom_batch(payload, "math.AC")


def test_production_absonly_action_maps_to_replacement() -> None:
    payload = fixture("current-production.xml").replace(b"new", b"absonly")

    batch = parse_atom_batch(payload, "math.AC")

    assert batch.entries[0].announce_type is AnnounceType.REPLACE


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
    assert entry.announced_version == 2


def test_legacy_later_version_uses_entry_updated_timestamp() -> None:
    batch = parse_atom_batch(fixture("current-mixed.xml"), "cs.CL")

    assert batch.entries[2].published_at == datetime(
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
    assert entry.announced_version == 3


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
    assert batch.fetched_at == datetime(
        2026, 8, 23, 4, tzinfo=timezone.utc
    )
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


def test_atom_source_rejects_a_redirect_to_a_different_category() -> None:
    class RedirectingClient:
        def get(self, url: str, **_options: object) -> HttpResponse:
            return HttpResponse(
                status=200,
                final_url="https://rss.arxiv.org/atom/math.AG",
                headers={"content-type": "application/atom+xml"},
                body=fixture("current-production.xml"),
                observed_at=datetime(2026, 8, 24, 5, tzinfo=timezone.utc),
            )

    with pytest.raises(AtomParseError, match="requested category"):
        AtomSource(RedirectingClient()).fetch("math.AC")


def test_versioned_legacy_entry_id_is_preserved() -> None:
    payload = fixture("current-mixed.xml").replace(
        b"2608.10001v1",
        b"hep-th/9901001v1",
        1,
    )

    batch = parse_atom_batch(payload, "cs.CL")

    assert batch.entries[0].metadata.arxiv_id == "hep-th/9901001"
    assert batch.entries[0].announced_version == 1


def test_invalid_feed_timestamp_is_rejected_with_typed_error() -> None:
    payload = fixture("current-empty.xml").replace(
        b"2026-08-23T03:59:59Z",
        b"not-a-timestamp",
    )

    with pytest.raises(AtomParseError, match="invalid Atom updated"):
        parse_atom_batch(payload, "stat.ML")
