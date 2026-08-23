from __future__ import annotations

from collections.abc import Callable
import re
from datetime import date
from hashlib import sha256
from types import MappingProxyType
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from arxiv_digest.models import (
    AnnounceType,
    CatchupDay,
    CatchupEntry,
    CatchupPage,
    EnrichmentStatus,
    PaperMetadata,
)
from arxiv_digest.rate_limit import (
    ArxivHttpClient,
    ArxivRequestCancelled,
    Interface,
)
from arxiv_digest.sources.xml import normalize_space, parse_arxiv_id


CATCHUP_SECTIONS = MappingProxyType(
    {
        "new submissions": AnnounceType.NEW,
        "cross submissions": AnnounceType.CROSS,
        "replacements": AnnounceType.REPLACE,
    }
)

_SUBJECT_CATEGORY_RE = re.compile(
    r"\((?P<category>[a-z][a-z0-9-]*(?:\.[A-Za-z0-9-]+)?)\)\s*$"
)


class CatchupError(Exception):
    """A catch-up failure with a persistence-safe diagnostic."""

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


class CatchupLayoutError(CatchupError):
    """The response did not contain the expected catch-up structure."""

    def __init__(self) -> None:
        super().__init__(
            "catchup_layout_changed",
            "The arXiv catch-up page layout was not recognized.",
        )


class CatchupFetchError(CatchupError):
    """A catch-up request failed without exposing transport details."""

    def __init__(self) -> None:
        super().__init__(
            "catchup_fetch_failed",
            "The arXiv catch-up page could not be retrieved.",
        )


def failed_day(
    category: str,
    mailing_date: date,
    error: CatchupError,
) -> CatchupDay:
    return CatchupDay(
        category=category,
        mailing_date=mailing_date,
        status=EnrichmentStatus.FAILED,
        pages=(),
        error_code=error.code,
        error_message=error.safe_message,
    )


def _failed_day_with_pages(
    category: str,
    mailing_date: date,
    error: CatchupError,
    pages: list[CatchupPage],
) -> CatchupDay:
    if not pages:
        return failed_day(category, mailing_date, error)
    return CatchupDay(
        category=category,
        mailing_date=mailing_date,
        status=EnrichmentStatus.FAILED,
        pages=tuple(pages),
        error_code=error.code,
        error_message=error.safe_message,
    )


def _normalized_page_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            parsed.query,
            "",
        )
    )


def _page_urls(root: Tag, page_url: str) -> tuple[str, ...]:
    current = urlsplit(page_url)
    values: list[str] = []
    seen: set[str] = set()
    for anchor in root.select(".paging a[href]"):
        try:
            candidate = _normalized_page_url(
                urljoin(page_url, str(anchor["href"]))
            )
            parsed = urlsplit(candidate)
            allowed = (
                parsed.scheme == "https"
                and parsed.hostname == "arxiv.org"
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and parsed.path == current.path
            )
        except ValueError:
            continue
        if not allowed or candidate in seen:
            continue
        seen.add(candidate)
        values.append(candidate)
    normalized_current = _normalized_page_url(page_url)
    if normalized_current not in seen:
        values.insert(0, normalized_current)
    return tuple(values)


def _discover_page_urls(payload: bytes, page_url: str) -> tuple[str, ...]:
    soup = BeautifulSoup(payload, "html.parser")
    root = soup.select_one("#dlpage")
    if root is None:
        raise CatchupLayoutError
    return _page_urls(root, page_url)


def _field_text(meta: Tag, selector: str) -> str:
    field = meta.select_one(selector)
    if field is None:
        return ""
    for descriptor in field.select(".descriptor"):
        descriptor.decompose()
    return normalize_space(field.get_text(" ", strip=True))


def _required_field(meta: Tag, selector: str) -> str:
    value = _field_text(meta, selector)
    if not value:
        raise CatchupLayoutError
    return value


def _entry_position(term: Tag) -> int:
    marker = term.select_one("a[name]")
    match = (
        None
        if marker is None
        else re.fullmatch(r"item([1-9]\d*)", str(marker.get("name", "")))
    )
    if match is None:
        raise CatchupLayoutError
    return int(match.group(1)) - 1


def _subject_category(subject: Tag) -> str:
    attribute = normalize_space(str(subject.get("data-category", "")))
    if attribute:
        return attribute
    match = _SUBJECT_CATEGORY_RE.search(subject.get_text(" ", strip=True))
    return "" if match is None else match.group("category")


def _entry_arxiv_id(term: Tag) -> str:
    for identifier in term.select("a[href]"):
        try:
            parsed = urlsplit(
                urljoin("https://arxiv.org", str(identifier.get("href", "")))
            )
            if (
                parsed.scheme != "https"
                or parsed.hostname != "arxiv.org"
                or parsed.port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or not parsed.path.startswith("/abs/")
            ):
                continue
            candidate = parsed.path.removeprefix("/abs/").strip("/")
            arxiv_id, _current_version = parse_arxiv_id(candidate)
            return arxiv_id
        except ValueError:
            continue
    raise CatchupLayoutError


def _parse_entry(
    term: Tag,
    details: Tag,
    section: AnnounceType,
    mailing_date: date,
) -> CatchupEntry:
    meta = details.select_one(".meta")
    if meta is None:
        raise CatchupLayoutError
    arxiv_id = _entry_arxiv_id(term)
    authors = tuple(
        normalize_space(author.get_text(" ", strip=True))
        for author in meta.select(".list-authors a")
        if normalize_space(author.get_text(" ", strip=True))
    )
    category_values: list[str] = []
    for subject in meta.select(".list-subjects span:not(.descriptor)"):
        category_value = _subject_category(subject)
        if category_value and category_value not in category_values:
            category_values.append(category_value)
    categories = tuple(category_values)
    primary = meta.select_one(".list-subjects .primary-subject")
    primary_category = (
        None if primary is None else _subject_category(primary) or None
    )
    try:
        metadata = PaperMetadata(
            arxiv_id=arxiv_id,
            title=_required_field(meta, ".list-title"),
            authors=authors,
            abstract=_required_field(meta, "p.mathjax"),
            primary_category=primary_category,
            categories=categories,
            comments=_field_text(meta, ".list-comments"),
            journal_ref=_field_text(meta, ".list-journal-ref"),
            doi=_field_text(meta, ".list-doi") or None,
        )
        return CatchupEntry(
            metadata=metadata,
            section=section,
            mailing_date=mailing_date,
            position=_entry_position(term),
        )
    except (TypeError, ValueError) as error:
        raise CatchupLayoutError from error


def parse_catchup_page(
    payload: bytes,
    category: str,
    mailing_date: date,
    page_url: str,
) -> CatchupPage:
    soup = BeautifulSoup(payload, "html.parser")
    root = soup.select_one("#dlpage")
    if root is None:
        raise CatchupLayoutError
    page_urls = _page_urls(root, page_url)
    normalized_current = _normalized_page_url(page_url)
    try:
        page_number = page_urls.index(normalized_current) + 1
    except ValueError as error:
        raise CatchupLayoutError from error
    entries: list[CatchupEntry] = []
    for heading in root.find_all("h3"):
        label = normalize_space(heading.get_text(" ", strip=True)).casefold()
        section = CATCHUP_SECTIONS.get(label)
        if section is None:
            continue
        listing = heading.find_next_sibling("dl")
        if listing is None:
            raise CatchupLayoutError
        terms = listing.find_all("dt", recursive=False)
        details = listing.find_all("dd", recursive=False)
        if len(terms) != len(details):
            raise CatchupLayoutError
        entries.extend(
            _parse_entry(term, detail, section, mailing_date)
            for term, detail in zip(terms, details, strict=True)
        )
    if not entries and root.select_one(".no-papers, .no-articles") is None:
        raise CatchupLayoutError
    return CatchupPage(
        category=category,
        mailing_date=mailing_date,
        page=page_number,
        total_pages=len(page_urls),
        entries=tuple(entries),
        raw_sha256=sha256(payload).hexdigest(),
    )


class CatchupSource:
    def __init__(
        self,
        client: ArxivHttpClient,
        *,
        base_url: str = "https://arxiv.org/catchup",
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    def fetch_day(
        self,
        category: str,
        mailing_date: date,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> CatchupDay:
        encoded_category = quote(category, safe=".")
        first_url = f"{self._base_url}/{encoded_category}/{mailing_date.isoformat()}"
        pending = [first_url]
        scheduled = {_normalized_page_url(first_url)}
        pages: list[CatchupPage] = []
        while pending:
            request_url = pending.pop(0)
            try:
                request_options = {
                    "interface": Interface.CATCHUP,
                    "accept": "text/html, application/xhtml+xml",
                }
                if cancelled is not None:
                    request_options["cancelled"] = cancelled
                response = self._client.get(request_url, **request_options)
                if response.status != 200:
                    raise CatchupFetchError
                page_url = _normalized_page_url(response.final_url)
                page = parse_catchup_page(
                    response.body,
                    category,
                    mailing_date,
                    page_url,
                )
                advertised_urls = _discover_page_urls(response.body, page_url)
                scheduled.add(page_url)
            except ArxivRequestCancelled:
                raise
            except CatchupError as error:
                return _failed_day_with_pages(
                    category,
                    mailing_date,
                    error,
                    pages,
                )
            except Exception:
                return _failed_day_with_pages(
                    category,
                    mailing_date,
                    CatchupFetchError(),
                    pages,
                )
            pages.append(page)
            for advertised_url in advertised_urls:
                if advertised_url in scheduled:
                    continue
                scheduled.add(advertised_url)
                pending.append(advertised_url)
        status = (
            EnrichmentStatus.COMPLETE
            if any(page.entries for page in pages)
            else EnrichmentStatus.EMPTY
        )
        return CatchupDay(
            category=category,
            mailing_date=mailing_date,
            status=status,
            pages=tuple(pages),
            error_code=None,
            error_message=None,
        )
