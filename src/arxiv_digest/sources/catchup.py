from __future__ import annotations

from collections.abc import Callable
import re
from datetime import date, datetime
from hashlib import sha256
from types import MappingProxyType
from urllib.parse import (
    parse_qsl,
    quote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from bs4 import BeautifulSoup, Tag

from arxiv_digest.models import (
    AnnounceType,
    CatchupDay,
    CatchupEntry,
    CatchupPage,
    EnrichmentStatus,
    EvidenceSource,
    PaperMetadata,
    SourceObservation,
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
_CURRENT_CATCHUP_SECTIONS = MappingProxyType(
    {
        "new submissions": AnnounceType.NEW,
        "cross submissions": AnnounceType.CROSS,
        "replacement submissions": AnnounceType.REPLACE,
    }
)

_SUBJECT_CATEGORY_RE = re.compile(
    r"\((?P<category>[a-z][a-z0-9-]*(?:\.[A-Za-z0-9-]+)?)\)"
)
_ZERO_ENTRIES_RE = re.compile(
    r"(?:\btotal\s+of\s+0\s+(?:entries|papers)\b"
    r"|\bshowing\s+0\s+of\s+0\s+(?:entries|papers)\b)",
    re.IGNORECASE,
)
_TOTAL_ENTRIES_RE = re.compile(
    r"\btotal\s+of\s+(?P<count>\d+)\s+entries\b",
    re.IGNORECASE,
)
_CURRENT_HEADING_RE = re.compile(
    r"^(?P<label>new submissions|cross submissions|replacement submissions)"
    r"\s+\((?:continued,\s+)?showing\s+(?:(?:first|last)\s+)?"
    r"(?P<shown>[1-9]\d*)\s+of\s+(?P<total>[1-9]\d*)\s+entries\)$",
    re.IGNORECASE,
)
_CATCHUP_PATH_RE = re.compile(r"^/catchup/[^/]+/\d{4}-\d{2}-\d{2}$")
_MAX_CATCHUP_PAGES = 100


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


def catchup_observations(
    day: CatchupDay,
    observed_at: datetime,
) -> tuple[SourceObservation, ...]:
    """Normalize one exact daily-list result into source observations."""
    if day.status == EnrichmentStatus.FAILED:
        return ()
    return tuple(
        SourceObservation(
            source_key=(
                f"catchup:{day.category}:{day.mailing_date.isoformat()}:"
                f"{entry.metadata.arxiv_id}:{entry.position}"
            ),
            arxiv_id=entry.metadata.arxiv_id,
            source=EvidenceSource.CATCHUP,
            category=day.category,
            announce_type=entry.section,
            daily_list_date=day.mailing_date,
            announced_version=entry.announced_version,
            list_position=entry.position,
            oai_datestamp=None,
            response_sha256=page.raw_sha256,
            observed_at=observed_at,
        )
        for page in sorted(day.pages, key=lambda item: item.page)
        for entry in sorted(page.entries, key=lambda item: item.position)
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


def _safe_same_path_url(value: str, page_url: str) -> str | None:
    current = urlsplit(page_url)
    try:
        candidate = _normalized_page_url(urljoin(page_url, value))
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
        return None
    return candidate if allowed else None


def _is_catchup_page_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme.casefold() == "https"
            and parsed.hostname == "arxiv.org"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and _CATCHUP_PATH_RE.fullmatch(parsed.path) is not None
        )
    except ValueError:
        return False


def _production_page_number(url: str) -> int | None:
    try:
        values = parse_qsl(
            urlsplit(url).query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return None
    if len(values) != 2 or {key for key, _value in values} != {"abs", "page"}:
        return None
    parameters = dict(values)
    page = parameters.get("page", "")
    if (
        parameters.get("abs") != "False"
        or re.fullmatch(r"[1-9]\d*", page) is None
    ):
        return None
    return int(page)


def _production_page_url(page_url: str, page: int) -> str:
    current = urlsplit(page_url)
    return urlunsplit(
        (
            "https",
            "arxiv.org",
            current.path,
            urlencode((("abs", "False"), ("page", page))),
            "",
        )
    )


def _legacy_page_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if not parsed.query:
        return _normalized_page_url(url)
    try:
        values = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return None
    if len(values) != 2 or {key for key, _value in values} != {"skip", "show"}:
        return None
    parameters = dict(values)
    skip = parameters.get("skip", "")
    show = parameters.get("show", "")
    if (
        re.fullmatch(r"\d+", skip) is None
        or re.fullmatch(r"[1-9]\d*", show) is None
    ):
        return None
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode((("skip", skip), ("show", show))),
            "",
        )
    )


def _page_urls(root: Tag, page_url: str) -> tuple[str, ...]:
    if not _is_catchup_page_url(page_url):
        raise CatchupLayoutError
    current_page = _production_page_number(page_url)
    if current_page is not None:
        pages = {current_page: _production_page_url(page_url, current_page)}
        for anchor in root.select("a[href]"):
            candidate = _safe_same_path_url(str(anchor["href"]), page_url)
            if candidate is None:
                continue
            page = _production_page_number(candidate)
            if page is not None:
                pages[page] = _production_page_url(page_url, page)
        last_page = max(pages)
        if last_page > _MAX_CATCHUP_PAGES:
            raise CatchupLayoutError
        return tuple(
            _production_page_url(page_url, page)
            for page in range(1, last_page + 1)
        )

    values: list[str] = []
    seen: set[str] = set()
    for anchor in root.select(".paging a[href]"):
        candidate = _safe_same_path_url(str(anchor["href"]), page_url)
        if candidate is None:
            continue
        candidate = _legacy_page_url(candidate)
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        values.append(candidate)
    normalized_current = _legacy_page_url(page_url)
    if normalized_current is None:
        raise CatchupLayoutError
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


def _categories(meta: Tag) -> tuple[str, ...]:
    field = meta.select_one(".list-subjects")
    if field is None:
        return ()
    values: list[str] = []
    for subject in field.select("span:not(.descriptor)"):
        category = _subject_category(subject)
        if category and category not in values:
            values.append(category)
    for match in _SUBJECT_CATEGORY_RE.finditer(field.get_text(" ", strip=True)):
        category = match.group("category")
        if category not in values:
            values.append(category)
    return tuple(values)


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
    categories = _categories(meta)
    primary = meta.select_one(".list-subjects .primary-subject")
    primary_category = (
        None if primary is None else _subject_category(primary) or None
    )
    try:
        metadata = PaperMetadata(
            arxiv_id=arxiv_id,
            title=_required_field(meta, ".list-title"),
            authors=authors,
            abstract=_field_text(meta, "p.mathjax"),
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


def _heading_section(heading: Tag) -> AnnounceType | None:
    value = normalize_space(heading.get_text(" ", strip=True)).casefold()
    legacy = CATCHUP_SECTIONS.get(value)
    if legacy is not None:
        return legacy
    match = _CURRENT_HEADING_RE.fullmatch(value)
    if match is None:
        return None
    return _CURRENT_CATCHUP_SECTIONS[match.group("label").casefold()]


def _current_heading_entry_count(heading: Tag) -> int | None:
    value = normalize_space(heading.get_text(" ", strip=True)).casefold()
    match = _CURRENT_HEADING_RE.fullmatch(value)
    return None if match is None else int(match.group("shown"))


def _nested_section_entries(heading: Tag) -> tuple[tuple[Tag, Tag], ...]:
    pairs: list[tuple[Tag, Tag]] = []
    term: Tag | None = None
    for sibling in heading.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if sibling.name == "h3":
            break
        if sibling.name == "dt":
            if term is not None:
                raise CatchupLayoutError
            term = sibling
        elif sibling.name == "dd":
            if term is None:
                raise CatchupLayoutError
            pairs.append((term, sibling))
            term = None
    if term is not None:
        raise CatchupLayoutError
    return tuple(pairs)


def _section_entries(heading: Tag) -> tuple[tuple[Tag, Tag], ...]:
    parent = heading.parent
    if (
        isinstance(parent, Tag)
        and parent.name == "dl"
        and parent.get("id") == "articles"
    ):
        return _nested_section_entries(heading)
    listing = heading.find_next_sibling("dl")
    if listing is None:
        raise CatchupLayoutError
    terms = listing.find_all("dt", recursive=False)
    details = listing.find_all("dd", recursive=False)
    if len(terms) != len(details):
        raise CatchupLayoutError
    return tuple(zip(terms, details, strict=True))


def _is_explicitly_empty(root: Tag) -> bool:
    if root.select_one(".no-papers, .no-articles") is not None:
        return True
    if _ZERO_ENTRIES_RE.search(
        normalize_space(root.get_text(" ", strip=True))
    ) is None:
        return False
    return any(
        normalize_space(paragraph.get_text(" ", strip=True))
        .casefold()
        .startswith("no updates for ")
        for paragraph in root.find_all("p")
    )


def _advertised_total(root: Tag) -> int:
    totals = {
        int(match.group("count"))
        for match in _TOTAL_ENTRIES_RE.finditer(
            normalize_space(root.get_text(" ", strip=True))
        )
    }
    if len(totals) != 1:
        raise CatchupLayoutError
    return next(iter(totals))


def _current_listing_entries(listing: Tag) -> tuple[tuple[Tag, Tag], ...]:
    headings = listing.find_all("h3", recursive=False)
    if len(headings) != 1 or _heading_section(headings[0]) is None:
        raise CatchupLayoutError
    structural = [
        child
        for child in listing.children
        if isinstance(child, Tag) and child.name in {"h3", "dt", "dd"}
    ]
    if not structural or structural[0] is not headings[0]:
        raise CatchupLayoutError
    return _nested_section_entries(headings[0])


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
    production_page = _production_page_number(page_url)
    if production_page is None:
        normalized_current = _legacy_page_url(page_url)
        try:
            page_number = page_urls.index(normalized_current) + 1
        except ValueError as error:
            raise CatchupLayoutError from error
    else:
        page_number = production_page
    total_pages = (
        len(page_urls)
        if production_page is None
        else max(
            page
            for url in page_urls
            if (page := _production_page_number(url)) is not None
        )
    )
    entries: list[CatchupEntry] = []
    current_listings = root.select("dl#articles")
    if current_listings:
        advertised_total = _advertised_total(root)
        for listing in current_listings:
            heading = listing.find("h3", recursive=False)
            if heading is None:
                raise CatchupLayoutError
            section = _heading_section(heading)
            if section is None:
                raise CatchupLayoutError
            pairs = _current_listing_entries(listing)
            shown = _current_heading_entry_count(heading)
            if shown is None or len(pairs) != shown:
                raise CatchupLayoutError
            entries.extend(
                _parse_entry(term, detail, section, mailing_date)
                for term, detail in pairs
            )
        if len(entries) > advertised_total:
            raise CatchupLayoutError
    else:
        for heading in root.find_all("h3"):
            section = _heading_section(heading)
            if section is None:
                continue
            entries.extend(
                _parse_entry(term, detail, section, mailing_date)
                for term, detail in _section_entries(heading)
            )
    if not entries and not _is_explicitly_empty(root):
        raise CatchupLayoutError
    return CatchupPage(
        category=category,
        mailing_date=mailing_date,
        page=page_number,
        total_pages=total_pages,
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
        first_url = (
            f"{self._base_url}/{encoded_category}/{mailing_date.isoformat()}"
            "?abs=False&page=1"
        )
        expected_path = urlsplit(_normalized_page_url(first_url)).path
        pending = [first_url]
        scheduled = {_normalized_page_url(first_url)}
        pages: list[CatchupPage] = []
        advertised_total: int | None = None
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
                if urlsplit(page_url).path != expected_path:
                    raise CatchupLayoutError
                page = parse_catchup_page(
                    response.body,
                    category,
                    mailing_date,
                    page_url,
                )
                soup = BeautifulSoup(response.body, "html.parser")
                root = soup.select_one("#dlpage")
                if root is None:
                    raise CatchupLayoutError
                page_total = _advertised_total(root)
                if (
                    advertised_total is not None
                    and page_total != advertised_total
                ):
                    raise CatchupLayoutError
                advertised_total = page_total
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
        if advertised_total is None:
            return failed_day(category, mailing_date, CatchupLayoutError())
        entries = tuple(
            entry for page in pages for entry in page.entries
        )
        positions = {entry.position for entry in entries}
        if (
            len(entries) != advertised_total
            or positions != set(range(advertised_total))
        ):
            return _failed_day_with_pages(
                category,
                mailing_date,
                CatchupLayoutError(),
                pages,
            )
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
