"""Shared XML and identifier helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from xml.etree.ElementTree import Element


_ID_RE = re.compile(
    r"^(?P<base>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))"
    r"(?:v(?P<version>[1-9]\d*))?$"
)


def normalize_space(value: str) -> str:
    """Collapse XML whitespace into single spaces."""

    return " ".join(value.split())


def element_text(element: Element | None) -> str:
    """Return normalized descendant text regardless of namespace prefixes."""

    if element is None:
        return ""
    return normalize_space("".join(element.itertext()))


def parse_utc_datetime(value: str) -> datetime:
    """Parse an aware ISO timestamp and normalize it to UTC."""

    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_arxiv_id(value: str) -> tuple[str, int | None]:
    """Return the base arXiv identifier and its optional version."""

    match = _ID_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"invalid arXiv identifier: {value!r}")
    version = match.group("version")
    return match.group("base"), None if version is None else int(version)
