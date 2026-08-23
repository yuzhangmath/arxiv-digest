from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from types import MappingProxyType
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from arxiv_digest.models import (
    AnnounceType,
    AtomBatch,
    AtomEntry,
    PaperMetadata,
    PaperVersion,
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
_NEW_YORK = ZoneInfo("America/New_York")

ANNOUNCE_TYPES = MappingProxyType(
    {
        "new": AnnounceType.NEW,
        "cross": AnnounceType.CROSS,
        "replace": AnnounceType.REPLACE,
        "replace-cross": AnnounceType.REPLACE_CROSS,
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
        response = self.client.get(
            f"{self.base_url}/{quote(category, safe='')}",
            **request_options,
        )
        return parse_atom_batch(response.body, category)


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
    parsed = urlsplit(entry_id)
    if parsed.scheme:
        candidate = parsed.path.removeprefix("/abs/").lstrip("/")
    else:
        candidate = entry_id
    try:
        base, version = parse_arxiv_id(candidate)
    except ValueError as error:
        raise AtomParseError("invalid Atom entry ID") from error
    if version is None:
        raise AtomParseError("Atom entry ID must include a version")
    return base, version


def parse_atom_batch(payload: bytes, category: str) -> AtomBatch:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise AtomParseError("malformed Atom XML") from error
    fetched_at = _required_datetime(root, "updated")
    mailing_date = fetched_at.astimezone(_NEW_YORK).date()
    entries: list[AtomEntry] = []
    for position, element in enumerate(root.findall(f"{{{_ATOM}}}entry")):
        arxiv_id, version_number = _base_and_version(
            _required_text(element, "id")
        )
        timestamp_name = "published" if version_number == 1 else "updated"
        submitted_at = _required_datetime(element, timestamp_name)
        announce_type = parse_announce_type(
            element_text(element.find(f"{{{_ARXIV}}}announce_type"))
        )
        authors = tuple(
            _required_text(author, "name")
            for author in element.findall(f"{{{_ATOM}}}author")
        )
        categories = tuple(
            normalize_space(value)
            for category_element in element.findall(f"{{{_ATOM}}}category")
            if (value := category_element.attrib.get("term", "")).strip()
        )
        primary_element = element.find(f"{{{_ARXIV}}}primary_category")
        primary_category = (
            None
            if primary_element is None
            else normalize_space(primary_element.attrib.get("term", "")) or None
        )
        doi = element_text(element.find(f"{{{_ARXIV}}}doi")) or None
        metadata = PaperMetadata(
            arxiv_id=arxiv_id,
            title=_required_text(element, "title"),
            authors=authors,
            abstract=_required_text(element, "summary"),
            primary_category=primary_category,
            categories=categories,
            comments=element_text(element.find(f"{{{_ARXIV}}}comment")),
            journal_ref=element_text(
                element.find(f"{{{_ARXIV}}}journal_ref")
            ),
            doi=doi,
        )
        entries.append(
            AtomEntry(
                metadata=metadata,
                version=PaperVersion(version_number, submitted_at),
                announce_type=announce_type,
                mailing_date=mailing_date,
                position=position,
            )
        )
    return AtomBatch(
        category=category,
        mailing_date=mailing_date,
        entries=tuple(entries),
        raw_sha256=hashlib.sha256(payload).hexdigest(),
        fetched_at=fetched_at,
    )
