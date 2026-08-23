from datetime import datetime, timezone
from xml.etree import ElementTree

import pytest

from arxiv_digest.sources import xml
from arxiv_digest.sources.xml import parse_arxiv_id


def test_parse_arxiv_id_supports_modern_and_legacy_versions() -> None:
    assert parse_arxiv_id("2608.00001v2") == ("2608.00001", 2)
    assert parse_arxiv_id("hep-th/9901001v3") == ("hep-th/9901001", 3)


def test_parse_arxiv_id_ignores_surrounding_whitespace() -> None:
    assert parse_arxiv_id("  2608.00001v2\n") == ("2608.00001", 2)


def test_parse_arxiv_id_allows_an_unversioned_identifier() -> None:
    assert parse_arxiv_id("2608.00001") == ("2608.00001", None)


def test_parse_arxiv_id_supports_a_four_digit_modern_suffix() -> None:
    assert parse_arxiv_id("0704.0001v1") == ("0704.0001", 1)


def test_parse_arxiv_id_supports_a_subject_class_legacy_archive() -> None:
    assert parse_arxiv_id("math.GT/0309136v4") == ("math.GT/0309136", 4)


def test_parse_arxiv_id_rejects_version_zero() -> None:
    with pytest.raises(ValueError, match="invalid arXiv identifier"):
        parse_arxiv_id("2608.00001v0")


def test_normalize_space_collapses_xml_whitespace() -> None:
    assert xml.normalize_space("  Neutral\n  synthetic\t text  ") == (
        "Neutral synthetic text"
    )


def test_element_text_is_independent_of_namespace_prefixes() -> None:
    first = ElementTree.fromstring(
        "<a:title xmlns:a='urn:synthetic'> Neutral <a:b>text</a:b> </a:title>"
    )
    second = ElementTree.fromstring(
        "<b:title xmlns:b='urn:synthetic'> Neutral <b:b>text</b:b> </b:title>"
    )

    assert xml.element_text(first) == "Neutral text"
    assert xml.element_text(second) == "Neutral text"


def test_parse_utc_datetime_normalizes_an_aware_timestamp() -> None:
    parsed = xml.parse_utc_datetime("2026-08-01T02:30:00+02:00")

    assert parsed == datetime(2026, 8, 1, 0, 30, tzinfo=timezone.utc)
    assert parsed.tzinfo is timezone.utc


@pytest.mark.parametrize("value", ["2026-08-01T02:30:00", "not-a-date"])
def test_parse_utc_datetime_rejects_invalid_or_naive_values(value: str) -> None:
    with pytest.raises(ValueError):
        xml.parse_utc_datetime(value)
