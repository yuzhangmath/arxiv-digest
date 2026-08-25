from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import date, datetime
from types import MappingProxyType
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    AtomEntry,
    EvidenceSource,
    PaperMetadata,
    SourceObservation,
)
from arxiv_digest.rate_limit import ArxivHttpClient, Interface
from arxiv_digest.sources.xml import (
    element_text,
    normalize_space,
    parse_arxiv_id,
    parse_utc_datetime,
)


_ATOM = "http://www.w3.org/2005/Atom"
_ARXIV = "http://arxiv.org/schemas/atom"
_DC = "http://purl.org/dc/elements/1.1/"
_NEW_YORK = ZoneInfo("America/New_York")
_OAI_ARXIV_PREFIX = "oai:arxiv.org:"
_SUMMARY_HEADER_RE = re.compile(
    r"^arXiv:(?P<identifier>\S+)\s+Announce Type:\s*"
    r"(?P<announce>[a-z-]+)\s+Abstract:\s*(?P<abstract>.*)$",
    re.IGNORECASE | re.DOTALL,
)

ANNOUNCE_TYPES = MappingProxyType(
    {
        "new": AnnounceType.NEW,
        "cross": AnnounceType.CROSS,
        "replace": AnnounceType.REPLACE,
        "replace-cross": AnnounceType.REPLACE_CROSS,
        "absonly": AnnounceType.REPLACE,
    }
)


class AtomParseError(ValueError):
    pass


class AtomSource:
    def __init__(
        self,
        client: ArxivHttpClient,
        *,
        base_url: str = "https://rss.arxiv.org/atom",
        max_feed_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.max_feed_bytes = max_feed_bytes

    def fetch(
        self,
        category: str,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> AtomBatch:
        request_options = {
            "interface": Interface.ATOM,
            "accept": (
                "application/atom+xml, application/xml;q=0.9, "
                "text/xml;q=0.8"
            ),
            "max_bytes": self.max_feed_bytes,
        }
        if cancelled is not None:
            request_options["cancelled"] = cancelled
        request_url = f"{self.base_url}/{quote(category, safe='')}"
        response = self.client.get(
            request_url,
            **request_options,
        )
        requested = urlsplit(request_url)
        final = urlsplit(response.final_url)
        if final.path != requested.path or final.query:
            raise AtomParseError(
                "Atom response does not match the requested category"
            )
        return parse_atom_batch(
            response.body,
            category,
            observed_at=response.observed_at,
        )


def atom_observations(batch: AtomBatch) -> tuple[SourceObservation, ...]:
    """Normalize an Atom batch into hidden, globally keyed evidence."""

    return tuple(
        SourceObservation(
            source_key=(
                f"atom:{batch.category}:{entry.metadata.arxiv_id}:"
                f"v{entry.announced_version}"
            ),
            arxiv_id=entry.metadata.arxiv_id,
            source=EvidenceSource.ATOM,
            category=batch.category,
            announce_type=entry.announce_type,
            daily_list_date=entry.mailing_date,
            announced_version=entry.announced_version,
            list_position=entry.position,
            oai_datestamp=None,
            response_sha256=batch.raw_sha256,
            observed_at=batch.fetched_at,
        )
        for entry in batch.entries
    )


def parse_announce_type(value: str) -> AnnounceType:
    normalized = normalize_space(value).casefold()
    if not normalized:
        raise AtomParseError("missing announcement type")
    try:
        return ANNOUNCE_TYPES[normalized]
    except KeyError as error:
        raise AtomParseError("unknown announcement type") from error


def _required_text(parent: ElementTree.Element, name: str) -> str:
    value = element_text(parent.find(f"{{{_ATOM}}}{name}"))
    if not value:
        raise AtomParseError(f"missing Atom {name}")
    return value


def _required_datetime(
    parent: ElementTree.Element,
    name: str,
) -> datetime:
    try:
        return parse_utc_datetime(_required_text(parent, name))
    except ValueError as error:
        raise AtomParseError(f"invalid Atom {name}") from error


def _base_and_version(entry_id: str) -> tuple[str, int]:
    normalized = entry_id.strip()
    if normalized.casefold().startswith(_OAI_ARXIV_PREFIX):
        candidate = normalized[len(_OAI_ARXIV_PREFIX) :]
    else:
        parsed = urlsplit(normalized)
        if parsed.scheme:
            candidate = parsed.path.removeprefix("/abs/").lstrip("/")
        else:
            candidate = normalized
    try:
        base, version = parse_arxiv_id(candidate)
    except ValueError as error:
        raise AtomParseError("invalid Atom entry ID") from error
    if version is None:
        raise AtomParseError("Atom entry ID must include a version")
    return base, version


def _authors(element: ElementTree.Element) -> tuple[str, ...]:
    structured = tuple(
        _required_text(author, "name")
        for author in element.findall(f"{{{_ATOM}}}author")
    )
    if structured:
        return structured
    creators = tuple(
        element_text(creator)
        for creator in element.findall(f"{{{_DC}}}creator")
    )
    authors = tuple(
        normalized
        for creator in creators
        for name in _split_dc_creator(creator)
        if (normalized := normalize_space(name))
    )
    if not authors:
        raise AtomParseError("missing Atom authors")
    return authors


def _split_dc_creator(value: str) -> tuple[str, ...]:
    """Split the feed's author line without breaking affiliation commas."""

    parts: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(value):
        if character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return tuple(parts)


def _categories(element: ElementTree.Element) -> tuple[str, ...]:
    categories: list[str] = []
    seen: set[str] = set()
    for category_element in element.findall(f"{{{_ATOM}}}category"):
        value = normalize_space(category_element.attrib.get("term", ""))
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            categories.append(value)
    if not categories:
        raise AtomParseError("missing Atom categories")
    return tuple(categories)


def _optional_arxiv_text(
    element: ElementTree.Element,
    *names: str,
) -> str:
    for name in names:
        value = element_text(element.find(f"{{{_ARXIV}}}{name}"))
        if value:
            return value
    return ""


def _summary_abstract(
    element: ElementTree.Element,
    arxiv_id: str,
    version_number: int,
    announce_type: AnnounceType,
) -> str:
    summary = _required_text(element, "summary")
    match = _SUMMARY_HEADER_RE.fullmatch(summary)
    if match is None:
        if summary.casefold().startswith("arxiv:"):
            raise AtomParseError("invalid Atom summary header")
        return summary
    try:
        header_id, header_version = _base_and_version(
            match.group("identifier")
        )
        header_announce = parse_announce_type(match.group("announce"))
    except AtomParseError as error:
        raise AtomParseError("invalid Atom summary header") from error
    if (
        header_id != arxiv_id
        or header_version != version_number
        or header_announce is not announce_type
    ):
        raise AtomParseError("Atom summary header disagrees with entry")
    abstract = normalize_space(match.group("abstract"))
    if not abstract:
        raise AtomParseError("missing Atom abstract")
    return abstract


def parse_atom_batch(
    payload: bytes,
    category: str,
    *,
    observed_at: datetime | None = None,
) -> AtomBatch:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise AtomParseError("malformed Atom XML") from error
    feed_updated_at = _required_datetime(root, "updated")
    entries: list[AtomEntry] = []
    mailing_dates: set[date] = set()
    for position, element in enumerate(root.findall(f"{{{_ATOM}}}entry")):
        entry_id = _required_text(element, "id")
        arxiv_id, version_number = _base_and_version(entry_id)
        production_entry = entry_id.casefold().startswith(_OAI_ARXIV_PREFIX)
        timestamp_name = (
            "published"
            if production_entry or version_number == 1
            else "updated"
        )
        published_at = _required_datetime(element, timestamp_name)
        mailing_date = published_at.astimezone(_NEW_YORK).date()
        mailing_dates.add(mailing_date)
        announce_type = parse_announce_type(
            element_text(element.find(f"{{{_ARXIV}}}announce_type"))
        )
        authors = _authors(element)
        categories = _categories(element)
        primary_element = element.find(f"{{{_ARXIV}}}primary_category")
        primary_category = (
            categories[0]
            if primary_element is None
            else normalize_space(primary_element.attrib.get("term", ""))
            or categories[0]
        )
        doi = _optional_arxiv_text(element, "DOI", "doi") or None
        metadata = PaperMetadata(
            arxiv_id=arxiv_id,
            title=_required_text(element, "title"),
            authors=authors,
            abstract=_summary_abstract(
                element,
                arxiv_id,
                version_number,
                announce_type,
            ),
            primary_category=primary_category,
            categories=categories,
            comments=_optional_arxiv_text(element, "comment", "comments"),
            journal_ref=_optional_arxiv_text(
                element,
                "journal_reference",
                "journal_ref",
            ),
            doi=doi,
        )
        entries.append(
            AtomEntry(
                metadata=metadata,
                announced_version=version_number,
                published_at=published_at,
                announce_type=announce_type,
                mailing_date=mailing_date,
                position=position,
            )
        )
    if len(mailing_dates) > 1:
        raise AtomParseError("mixed Atom published dates")
    mailing_date = (
        next(iter(mailing_dates))
        if mailing_dates
        else feed_updated_at.astimezone(_NEW_YORK).date()
    )
    return AtomBatch(
        category=category,
        mailing_date=mailing_date,
        entries=tuple(entries),
        raw_sha256=hashlib.sha256(payload).hexdigest(),
        fetched_at=feed_updated_at if observed_at is None else observed_at,
    )
