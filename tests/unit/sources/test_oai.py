from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Event
from urllib.request import Request

import pytest

from arxiv_digest.models import (
    OaiArticle,
    OaiTombstone,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.rate_limit import (
    ArxivHttpClient,
    ArxivRequestCancelled,
    Interface,
    RequestPolicy,
)
from arxiv_digest.sources.oai import (
    OaiParseError,
    OaiProtocolError,
    OaiSet,
    OaiSource,
    parse_identify,
    parse_list_records,
    parse_list_sets,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "oai"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FixtureResponse:
    status = 200
    headers = {"Content-Type": "application/xml"}

    def __init__(self, body: bytes, final_url: str) -> None:
        self.body = body
        self.final_url = final_url
        self.offset = 0

    def geturl(self) -> str:
        return self.final_url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def __enter__(self) -> FixtureResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class FixtureOpener:
    def __init__(self, *responses: bytes) -> None:
        self.responses = list(responses)
        self.requests: list[Request] = []

    def open(
        self,
        request: Request,
        timeout: float | None = None,
    ) -> FixtureResponse:
        self.requests.append(request)
        return FixtureResponse(self.responses.pop(0), request.full_url)


def source_with_responses(*responses: bytes) -> tuple[OaiSource, FixtureOpener]:
    opener = FixtureOpener(*responses)
    client = ArxivHttpClient(
        user_agent="arxiv-digest/0.1",
        contact_url="https://example.invalid/contact",
        opener=opener,
        monotonic=lambda: 0.0,
        wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc),
        sleeper=lambda _: None,
        policies={
            Interface.OAI: RequestPolicy(
                0,
                1,
                0,
                1024 * 1024,
                ("application/xml",),
            )
        },
    )
    return OaiSource(client, max_page_bytes=1024 * 1024), opener


def test_parse_identify_returns_response_date_granularity_and_earliest_date() -> None:
    identify = parse_identify(fixture("identify.xml"))

    assert identify.response_date == datetime(
        2026,
        8,
        22,
        4,
        5,
        6,
        tzinfo=timezone.utc,
    )
    assert identify.earliest_datestamp == date(2007, 5, 23)
    assert identify.granularity == "YYYY-MM-DD"


def test_parse_list_sets_preserves_exact_specs_labels_and_opaque_token() -> None:
    sets, token = parse_list_sets(fixture("list-sets.xml"))

    assert sets == (
        OaiSet(
            set_spec="synthetic:orbital-dynamics",
            display_name="Synthetic Orbital Dynamics",
        ),
        OaiSet(
            set_spec="synthetic:quantum-gardens",
            display_name="Synthetic Quantum Gardens",
        ),
    )
    assert token == "opaque+/token=="


def test_parse_list_records_normalizes_current_state_and_keeps_oai_state_separate(
) -> None:
    payload = fixture("page-1.xml")

    page = parse_list_records(payload)

    assert page.response_date == datetime(
        2026,
        8,
        22,
        4,
        7,
        tzinfo=timezone.utc,
    )
    assert page.resumption_token == "opaque+/second=="
    assert page.raw_sha256 == sha256(payload).hexdigest()
    assert page.records == (
        OaiArticle(
            oai_identifier="oai:arXiv.org:2608.90001",
            oai_datestamp=date(2026, 8, 21),
            set_specs=(
                "synthetic:orbital-dynamics",
                "synthetic:quantum-gardens",
            ),
            metadata=PaperMetadata(
                arxiv_id="2608.90001",
                title="Phase Gardens Across Imaginary Orbits",
                authors=("Iona Quasar", "Sol Ember III"),
                abstract=(
                    "We examine phase gardens whose orbits exist only in this "
                    "fixture."
                ),
                primary_category="synthetic.orbital",
                categories=("synthetic.orbital", "synthetic.quantum"),
                comments="Eleven fictional pages",
                journal_ref="Journal of Imaginary Dynamics 1 (2026)",
                doi="10.0000/fictional.2608.90001",
            ),
            versions=(
                PaperVersion(
                    number=1,
                    submitted_at=datetime(
                        2025,
                        8,
                        1,
                        10,
                        tzinfo=timezone.utc,
                    ),
                    size="7kb",
                    source_type="D",
                ),
                PaperVersion(
                    number=2,
                    submitted_at=datetime(
                        2026,
                        8,
                        22,
                        3,
                        4,
                        5,
                        tzinfo=timezone.utc,
                    ),
                    size="9kb",
                    source_type="I",
                ),
            ),
        ),
    )


def test_deleted_record_becomes_a_tombstone_without_article_metadata() -> None:
    page = parse_list_records(fixture("deleted.xml"))

    assert page.records == (
        OaiTombstone(
            oai_identifier="oai:arXiv.org:2608.90002",
            oai_datestamp=date(2026, 8, 20),
            set_specs=("synthetic:orbital-dynamics",),
        ),
    )
    assert not hasattr(page.records[0], "metadata")


def test_nonempty_oai_error_is_a_typed_protocol_failure() -> None:
    with pytest.raises(OaiProtocolError) as caught:
        parse_list_records(fixture("bad-resumption-token.xml"))

    assert caught.value.code == "badResumptionToken"
    assert caught.value.description == "The synthetic token is no longer valid."


@pytest.mark.parametrize("parser", [parse_identify, parse_list_sets])
def test_every_oai_verb_surfaces_protocol_errors_as_typed_failures(
    parser: Callable[[bytes], object],
) -> None:
    with pytest.raises(OaiProtocolError) as caught:
        parser(fixture("bad-resumption-token.xml"))

    assert caught.value.code == "badResumptionToken"


def test_no_records_match_is_an_empty_successful_page() -> None:
    payload = fixture("no-records.xml")

    page = parse_list_records(payload)

    assert page.response_date == datetime(
        2026,
        8,
        22,
        4,
        10,
        tzinfo=timezone.utc,
    )
    assert page.records == ()
    assert page.resumption_token is None
    assert page.raw_sha256 == sha256(payload).hexdigest()


@pytest.mark.parametrize(
    "payload",
    [
        b"<OAI-PMH>",
        b"""\
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-08-22T04:11:00Z</responseDate>
  <ListRecords>
    <record>
      <header>
        <identifier>oai:arXiv.org:2608.90003</identifier>
        <datestamp>2026-08-21</datestamp>
      </header>
    </record>
  </ListRecords>
</OAI-PMH>
""",
    ],
)
def test_malformed_xml_and_missing_required_fields_are_typed_parse_failures(
    payload: bytes,
) -> None:
    with pytest.raises(OaiParseError):
        parse_list_records(payload)


def test_identify_missing_required_fields_is_a_typed_parse_failure() -> None:
    payload = b"""\
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-08-22T04:11:00Z</responseDate>
  <Identify>
    <granularity>YYYY-MM-DD</granularity>
  </Identify>
</OAI-PMH>
"""

    with pytest.raises(OaiParseError, match="earliest datestamp"):
        parse_identify(payload)


def test_list_sets_missing_required_fields_is_a_typed_parse_failure() -> None:
    payload = b"""\
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-08-22T04:11:00Z</responseDate>
  <ListSets>
    <set><setSpec>synthetic:incomplete</setSpec></set>
  </ListSets>
</OAI-PMH>
"""

    with pytest.raises(OaiParseError, match="set name"):
        parse_list_sets(payload)


def test_list_records_missing_container_is_a_typed_parse_failure() -> None:
    payload = b"""\
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-08-22T04:11:00Z</responseDate>
</OAI-PMH>
"""

    with pytest.raises(OaiParseError, match="ListRecords"):
        parse_list_records(payload)


def test_missing_author_list_is_a_typed_parse_failure() -> None:
    payload = fixture("page-1.xml").replace(
        b"""\
          <raw:authors>
            <raw:author>
              <raw:keyname>Quasar</raw:keyname>
              <raw:forenames>Iona</raw:forenames>
            </raw:author>
            <raw:author>
              <raw:keyname>Ember</raw:keyname>
              <raw:forenames>Sol</raw:forenames>
              <raw:suffix>III</raw:suffix>
            </raw:author>
          </raw:authors>
""",
        b"",
    )

    with pytest.raises(OaiParseError, match="authors"):
        parse_list_records(payload)


def test_missing_version_history_is_a_typed_parse_failure() -> None:
    payload = b"""\
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"
         xmlns:raw="http://arxiv.org/OAI/arXivRaw/">
  <responseDate>2026-08-22T04:11:00Z</responseDate>
  <ListRecords><record>
    <header>
      <identifier>oai:arXiv.org:2608.90006</identifier>
      <datestamp>2026-08-21</datestamp>
    </header>
    <metadata><raw:arXivRaw>
      <raw:id>2608.90006</raw:id>
      <raw:title>A Versionless Synthetic Record</raw:title>
      <raw:authors>
        <raw:author><raw:keyname>Example</raw:keyname></raw:author>
      </raw:authors>
      <raw:categories>synthetic.example</raw:categories>
      <raw:abstract>This fictional record intentionally has no history.</raw:abstract>
    </raw:arXivRaw></metadata>
  </record></ListRecords>
</OAI-PMH>
"""

    with pytest.raises(OaiParseError, match="version history"):
        parse_list_records(payload)


def test_duplicate_version_numbers_are_rejected() -> None:
    payload = fixture("page-1.xml").replace(
        b'<raw:version version="v2">',
        b'<raw:version version="v1">',
    )

    with pytest.raises(OaiParseError, match="duplicate version number"):
        parse_list_records(payload)


def test_identify_uses_the_exact_oai_verb_through_the_http_client() -> None:
    source, opener = source_with_responses(fixture("identify.xml"))

    identify = source.identify()

    assert identify.earliest_datestamp == date(2007, 5, 23)
    assert [request.full_url for request in opener.requests] == [
        "https://export.arxiv.org/oai2?verb=Identify"
    ]
    assert opener.requests[0].get_header("Accept") == "application/xml"


def test_list_sets_follows_opaque_tokens_without_repeating_parameters() -> None:
    second_page = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<alt:OAI-PMH xmlns:alt="http://www.openarchives.org/OAI/2.0/">
  <alt:responseDate>2026-08-22T04:06:01Z</alt:responseDate>
  <alt:ListSets>
    <alt:set>
      <alt:setSpec>synthetic:spectral-forests</alt:setSpec>
      <alt:setName>Synthetic Spectral Forests</alt:setName>
    </alt:set>
    <alt:resumptionToken />
  </alt:ListSets>
</alt:OAI-PMH>
"""
    source, opener = source_with_responses(
        fixture("list-sets.xml"),
        second_page,
    )

    sets = source.list_sets()

    assert tuple(value.set_spec for value in sets) == (
        "synthetic:orbital-dynamics",
        "synthetic:quantum-gardens",
        "synthetic:spectral-forests",
    )
    assert [request.full_url for request in opener.requests] == [
        "https://export.arxiv.org/oai2?verb=ListSets",
        (
            "https://export.arxiv.org/oai2?"
            "verb=ListSets&resumptionToken=opaque%2B%2Ftoken%3D%3D"
        ),
    ]


def test_first_page_sends_exact_unbounded_list_records_parameters() -> None:
    source, opener = source_with_responses(fixture("page-1.xml"))

    page = source.first_page(
        "synthetic:orbital-dynamics",
        date(2026, 8, 1),
    )

    assert page.resumption_token == "opaque+/second=="
    assert [request.full_url for request in opener.requests] == [
        (
            "https://export.arxiv.org/oai2?verb=ListRecords&"
            "metadataPrefix=arXivRaw&set=synthetic%3Aorbital-dynamics&"
            "from=2026-08-01"
        )
    ]


def test_first_page_forwards_cooperative_cancellation_to_the_http_client() -> None:
    source, opener = source_with_responses(fixture("page-1.xml"))
    cancellation = Event()
    cancellation.set()

    with pytest.raises(ArxivRequestCancelled):
        source.first_page(
            "synthetic:orbital-dynamics",
            date(2026, 8, 1),
            cancelled=cancellation.is_set,
        )

    assert opener.requests == []


def test_sample_first_page_sends_an_inclusive_until_bound() -> None:
    source, opener = source_with_responses(fixture("page-1.xml"))

    source.sample_first_page(
        "synthetic:quantum-gardens",
        date(2026, 8, 1),
        date(2026, 8, 8),
        metadata_prefix="syntheticRaw",
    )

    assert [request.full_url for request in opener.requests] == [
        (
            "https://export.arxiv.org/oai2?verb=ListRecords&"
            "metadataPrefix=syntheticRaw&set=synthetic%3Aquantum-gardens&"
            "from=2026-08-01&until=2026-08-08"
        )
    ]


def test_backfill_first_page_has_its_own_bounded_request_method() -> None:
    source, opener = source_with_responses(fixture("page-1.xml"))

    page = source.backfill_first_page(
        "synthetic:orbital-dynamics",
        date(2025, 8, 1),
        date(2025, 8, 31),
    )

    assert isinstance(page.records[0], OaiArticle)
    assert [request.full_url for request in opener.requests] == [
        (
            "https://export.arxiv.org/oai2?verb=ListRecords&"
            "metadataPrefix=arXivRaw&set=synthetic%3Aorbital-dynamics&"
            "from=2025-08-01&until=2025-08-31"
        )
    ]


def test_next_page_sends_only_the_opaque_token_and_parses_a_legacy_id() -> None:
    source, opener = source_with_responses(fixture("page-2.xml"))

    page = source.next_page("opaque+/second==")

    assert page.resumption_token is None
    assert len(page.records) == 1
    record = page.records[0]
    assert isinstance(record, OaiArticle)
    assert record.metadata.arxiv_id == "astro-ph/9912345"
    assert record.metadata.authors == ("Mira Nightjar",)
    assert record.versions == (
        PaperVersion(
            number=1,
            submitted_at=datetime(
                1999,
                12,
                14,
                9,
                30,
                tzinfo=timezone.utc,
            ),
            size="4kb",
            source_type="D",
        ),
    )
    assert [request.full_url for request in opener.requests] == [
        (
            "https://export.arxiv.org/oai2?"
            "verb=ListRecords&resumptionToken=opaque%2B%2Fsecond%3D%3D"
        )
    ]


def test_get_record_uses_the_oai_identifier_and_parses_the_record() -> None:
    source, opener = source_with_responses(fixture("get-record.xml"))

    record = source.get_record("2608.90004")

    assert isinstance(record, OaiArticle)
    assert record.metadata.arxiv_id == "2608.90004"
    assert record.metadata.title == "Clockwork Petals in an Invented Vacuum"
    assert [request.full_url for request in opener.requests] == [
        (
            "https://export.arxiv.org/oai2?verb=GetRecord&"
            "metadataPrefix=arXivRaw&"
            "identifier=oai%3AarXiv.org%3A2608.90004"
        )
    ]


def test_administrative_datestamp_stays_separate_from_version_history() -> None:
    page = parse_list_records(fixture("administrative-update.xml"))

    record = page.records[0]
    assert isinstance(record, OaiArticle)
    assert record.oai_datestamp == date(2026, 8, 22)
    assert record.versions[-1].submitted_at == datetime(
        2026,
        1,
        8,
        5,
        tzinfo=timezone.utc,
    )
    assert record.oai_datestamp != record.versions[-1].submitted_at.date()
