from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, ParseError, fromstring

from arxiv_digest.models import (
    OaiArticle,
    OaiTombstone,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.rate_limit import ArxivHttpClient, Interface

from .xml import element_text, parse_arxiv_id, parse_utc_datetime


class OaiError(Exception):
    """Base class for deterministic OAI response failures."""


class OaiParseError(OaiError, ValueError):
    """The OAI response is not structurally valid for the requested verb."""


class OaiProtocolError(OaiError):
    def __init__(self, code: str, description: str) -> None:
        self.code = code
        self.description = description
        super().__init__(f"OAI protocol error {code}: {description}")


_DURABLE_PROTOCOL_CODES = {
    "badArgument": "oai_bad_argument",
    "badResumptionToken": "oai_bad_resumption_token",
    "bad_resumption_token": "oai_bad_resumption_token",
    "badVerb": "oai_bad_verb",
    "cannotDisseminateFormat": "oai_cannot_disseminate_format",
    "idDoesNotExist": "oai_id_does_not_exist",
    "noMetadataFormats": "oai_no_metadata_formats",
    "noRecordsMatch": "oai_no_records_match",
    "noSetHierarchy": "oai_no_set_hierarchy",
}
DURABLE_PROTOCOL_ERROR_CODES = frozenset(
    (*_DURABLE_PROTOCOL_CODES.values(), "oai_protocol_error")
)


def durable_protocol_error_code(code: str) -> str:
    """Map untrusted OAI attributes to a bounded diagnostic code."""

    return _DURABLE_PROTOCOL_CODES.get(code, "oai_protocol_error")


@dataclass(frozen=True, slots=True)
class OaiIdentify:
    response_date: datetime
    earliest_datestamp: date
    granularity: str


@dataclass(frozen=True, slots=True)
class OaiSet:
    set_spec: str
    display_name: str


@dataclass(frozen=True, slots=True)
class OaiPage:
    response_date: datetime
    records: tuple[OaiArticle | OaiTombstone, ...]
    resumption_token: str | None
    raw_sha256: str


def _parse_xml(payload: bytes) -> Element:
    try:
        return fromstring(payload)
    except ParseError as error:
        raise OaiParseError("malformed OAI XML") from error


def _required_text(parent: Element | None, path: str, field: str) -> str:
    value = element_text(None if parent is None else parent.find(path))
    if not value:
        raise OaiParseError(f"missing required OAI field: {field}")
    return value


def _protocol_error(root: Element) -> OaiProtocolError | None:
    error = root.find("{*}error")
    if error is None:
        return None
    return OaiProtocolError(error.get("code", ""), element_text(error))


def _parse_oai_datestamp(value: str) -> date:
    if "T" in value:
        return parse_utc_datetime(value).date()
    return date.fromisoformat(value)


def _parse_version(element: Element) -> PaperVersion:
    raw_number = element.get("version", "")
    if not raw_number.startswith("v"):
        raise ValueError("invalid arXiv version number")
    submitted_at = parsedate_to_datetime(
        _required_text(element, "{*}date", "version date")
    )
    if submitted_at.tzinfo is None or submitted_at.utcoffset() is None:
        raise ValueError("version date must include a timezone")
    source_type = element_text(element.find("{*}source_type"))
    if not source_type:
        source_type = element_text(element.find("{*}source-type"))
    return PaperVersion(
        number=int(raw_number[1:]),
        submitted_at=submitted_at.astimezone(timezone.utc),
        size=element_text(element.find("{*}size")) or None,
        source_type=source_type or None,
    )


def _parse_author(element: Element) -> str:
    parts = (
        element_text(element.find("{*}forenames")),
        _required_text(element, "{*}keyname", "author keyname"),
        element_text(element.find("{*}suffix")),
    )
    return " ".join(part for part in parts if part)


def _parse_article(record: Element) -> OaiArticle:
    header = record.find("{*}header")
    raw = record.find("{*}metadata/{*}arXivRaw")
    if raw is None:
        raise OaiParseError("missing required OAI field: arXivRaw metadata")
    arxiv_id = _required_text(raw, "{*}id", "arXiv ID")
    base_id, version = parse_arxiv_id(arxiv_id)
    if version is not None:
        raise ValueError("arXivRaw ID must be unversioned")
    categories = tuple(
        _required_text(raw, "{*}categories", "categories").split()
    )
    authors_parent = raw.find("{*}authors")
    author_elements = (
        () if authors_parent is None else authors_parent.findall("{*}author")
    )
    if not author_elements:
        raise OaiParseError("missing required OAI field: authors")
    authors = tuple(_parse_author(author) for author in author_elements)
    metadata = PaperMetadata(
        arxiv_id=base_id,
        title=_required_text(raw, "{*}title", "title"),
        authors=authors,
        abstract=_required_text(raw, "{*}abstract", "abstract"),
        primary_category=categories[0] if categories else None,
        categories=categories,
        comments=element_text(raw.find("{*}comments")),
        journal_ref=element_text(raw.find("{*}journal-ref")),
        doi=element_text(raw.find("{*}doi")) or None,
    )
    version_elements = raw.findall("{*}version")
    if not version_elements:
        raise OaiParseError("missing required OAI field: version history")
    versions = tuple(_parse_version(value) for value in version_elements)
    version_numbers = tuple(version.number for version in versions)
    if len(set(version_numbers)) != len(version_numbers):
        raise OaiParseError("duplicate version number")
    return OaiArticle(
        oai_identifier=_required_text(header, "{*}identifier", "identifier"),
        oai_datestamp=_parse_oai_datestamp(
            _required_text(header, "{*}datestamp", "datestamp")
        ),
        set_specs=tuple(
            element_text(element)
            for element in (() if header is None else header.findall("{*}setSpec"))
        ),
        metadata=metadata,
        versions=versions,
    )


def _parse_record(record: Element) -> OaiArticle | OaiTombstone:
    header = record.find("{*}header")
    if header is not None and header.get("status") == "deleted":
        return OaiTombstone(
            oai_identifier=_required_text(
                header,
                "{*}identifier",
                "identifier",
            ),
            oai_datestamp=_parse_oai_datestamp(
                _required_text(header, "{*}datestamp", "datestamp")
            ),
            set_specs=tuple(
                element_text(element)
                for element in header.findall("{*}setSpec")
            ),
        )
    return _parse_article(record)


def parse_identify(payload: bytes) -> OaiIdentify:
    root = _parse_xml(payload)
    if error := _protocol_error(root):
        raise error
    identify = root.find("{*}Identify")
    response_date = _required_text(root, "{*}responseDate", "response date")
    earliest_datestamp = _required_text(
        identify,
        "{*}earliestDatestamp",
        "earliest datestamp",
    )
    granularity = _required_text(
        identify,
        "{*}granularity",
        "granularity",
    )
    try:
        parsed_response_date = parse_utc_datetime(response_date)
        parsed_earliest_datestamp = date.fromisoformat(earliest_datestamp)
    except ValueError as error:
        raise OaiParseError("invalid OAI Identify date") from error
    return OaiIdentify(
        response_date=parsed_response_date,
        earliest_datestamp=parsed_earliest_datestamp,
        granularity=granularity,
    )


def parse_list_sets(payload: bytes) -> tuple[tuple[OaiSet, ...], str | None]:
    root = _parse_xml(payload)
    if error := _protocol_error(root):
        raise error
    _required_text(root, "{*}responseDate", "response date")
    list_sets = root.find("{*}ListSets")
    if list_sets is None:
        raise OaiParseError("missing required OAI field: ListSets")
    values = tuple(
        OaiSet(
            set_spec=_required_text(element, "{*}setSpec", "set specification"),
            display_name=_required_text(element, "{*}setName", "set name"),
        )
        for element in list_sets.findall("{*}set")
    )
    token = element_text(list_sets.find("{*}resumptionToken"))
    return values, token or None


def parse_list_records(payload: bytes) -> OaiPage:
    root = _parse_xml(payload)
    error = _protocol_error(root)
    if error is not None and error.code != "noRecordsMatch":
        raise error
    records_parent = root.find("{*}ListRecords")
    if records_parent is None and error is None:
        raise OaiParseError("missing required OAI field: ListRecords")
    records = tuple(
        _parse_record(record)
        for record in (
            ()
            if records_parent is None
            else records_parent.findall("{*}record")
        )
    )
    token = element_text(
        None
        if records_parent is None
        else records_parent.find("{*}resumptionToken")
    )
    return OaiPage(
        response_date=parse_utc_datetime(
            _required_text(root, "{*}responseDate", "response date")
        ),
        records=records,
        resumption_token=token or None,
        raw_sha256=sha256(payload).hexdigest(),
    )


def parse_get_record(payload: bytes) -> OaiArticle | OaiTombstone:
    root = _parse_xml(payload)
    if error := _protocol_error(root):
        raise error
    record = root.find("{*}GetRecord/{*}record")
    if record is None:
        raise OaiParseError("missing required OAI field: GetRecord record")
    return _parse_record(record)


class OaiSource:
    def __init__(
        self,
        client: ArxivHttpClient,
        *,
        base_url: str = "https://oaipmh.arxiv.org/oai",
        max_page_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.client = client
        self.base_url = base_url
        self.max_page_bytes = max_page_bytes

    def _get(
        self,
        parameters: dict[str, str],
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> bytes:
        request_options = {
            "interface": Interface.OAI,
            "accept": "application/xml",
            "max_bytes": self.max_page_bytes,
        }
        if cancelled is not None:
            request_options["cancelled"] = cancelled
        response = self.client.get(
            f"{self.base_url}?{urlencode(parameters)}",
            **request_options,
        )
        return response.body

    def identify(
        self, *, cancelled: Callable[[], bool] | None = None
    ) -> OaiIdentify:
        return parse_identify(
            self._get({"verb": "Identify"}, cancelled=cancelled)
        )

    def list_sets(
        self, *, cancelled: Callable[[], bool] | None = None
    ) -> tuple[OaiSet, ...]:
        values: list[OaiSet] = []
        parameters = {"verb": "ListSets"}
        while True:
            page, token = parse_list_sets(
                self._get(parameters, cancelled=cancelled)
            )
            values.extend(page)
            if token is None:
                return tuple(values)
            parameters = {
                "verb": "ListSets",
                "resumptionToken": token,
            }

    def first_page(
        self,
        set_spec: str,
        from_date: date,
        metadata_prefix: str = "arXivRaw",
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> OaiPage:
        return parse_list_records(
            self._get(
                {
                    "verb": "ListRecords",
                    "metadataPrefix": metadata_prefix,
                    "set": set_spec,
                    "from": from_date.isoformat(),
                },
                cancelled=cancelled,
            )
        )

    def sample_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        metadata_prefix: str = "arXivRaw",
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> OaiPage:
        return self._bounded_first_page(
            set_spec,
            from_date,
            until_date,
            metadata_prefix,
            cancelled=cancelled,
        )

    def backfill_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        metadata_prefix: str = "arXivRaw",
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> OaiPage:
        return self._bounded_first_page(
            set_spec,
            from_date,
            until_date,
            metadata_prefix,
            cancelled=cancelled,
        )

    def _bounded_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        metadata_prefix: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> OaiPage:
        return parse_list_records(
            self._get(
                {
                    "verb": "ListRecords",
                    "metadataPrefix": metadata_prefix,
                    "set": set_spec,
                    "from": from_date.isoformat(),
                    "until": until_date.isoformat(),
                },
                cancelled=cancelled,
            )
        )

    def next_page(
        self,
        resumption_token: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> OaiPage:
        return parse_list_records(
            self._get(
                {
                    "verb": "ListRecords",
                    "resumptionToken": resumption_token,
                },
                cancelled=cancelled,
            )
        )

    def get_record(
        self,
        arxiv_id: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> OaiArticle | OaiTombstone:
        base_id, version = parse_arxiv_id(arxiv_id)
        if version is not None:
            raise ValueError("get_record requires an unversioned arXiv ID")
        return parse_get_record(
            self._get(
                {
                    "verb": "GetRecord",
                    "metadataPrefix": "arXivRaw",
                    "identifier": f"oai:arXiv.org:{base_id}",
                },
                cancelled=cancelled,
            )
        )
