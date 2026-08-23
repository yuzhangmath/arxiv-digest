from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone

import pytest

from arxiv_digest import models


def _paper() -> models.PaperMetadata:
    return models.PaperMetadata(
        arxiv_id="2608.00001",
        title="Neutral Systems",
        authors=("A. Example",),
        abstract="A synthetic abstract.",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )


def _version(number: int = 1) -> models.PaperVersion:
    return models.PaperVersion(
        number,
        datetime(2026, 8, number, tzinfo=timezone.utc),
    )


def _evidence() -> models.EventEvidence:
    return models.EventEvidence(
        source_key="oai:cs.SE:2608.00001:v1",
        source=models.EvidenceSource.OAI,
        confidence=models.Confidence.INFERRED,
        category="cs.SE",
        announce_type=None,
        mailing_date=None,
        announced_version=1,
        list_position=None,
        oai_datestamp=date(2026, 8, 3),
        raw_sha256="0" * 64,
        observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


def _atom_entry() -> models.AtomEntry:
    return models.AtomEntry(
        _paper(),
        _version(),
        models.AnnounceType.NEW,
        date(2026, 8, 1),
        0,
    )


def _atom_batch() -> models.AtomBatch:
    return models.AtomBatch(
        "cs.SE",
        date(2026, 8, 1),
        (_atom_entry(),),
        "1" * 64,
        datetime(2026, 8, 2, tzinfo=timezone.utc),
    )


def _catchup_entry() -> models.CatchupEntry:
    return models.CatchupEntry(
        _paper(),
        models.AnnounceType.CROSS,
        date(2026, 8, 1),
        0,
    )


def _catchup_page() -> models.CatchupPage:
    return models.CatchupPage(
        "cs.SE",
        date(2026, 8, 1),
        1,
        1,
        (_catchup_entry(),),
        "2" * 64,
    )


def _review_event() -> models.ReviewEvent:
    evidence = _evidence()
    return models.ReviewEvent(
        7,
        "2608.00001",
        1,
        date(2026, 8, 1),
        models.DateBasis.VERSION_HISTORY_UTC,
        models.Confidence.INFERRED,
        (evidence,),
        9,
        None,
    )


def test_domain_enums_have_exact_wire_values() -> None:
    assert tuple(models.AnnounceType) == (
        models.AnnounceType.NEW,
        models.AnnounceType.CROSS,
        models.AnnounceType.REPLACE,
        models.AnnounceType.REPLACE_CROSS,
    )
    assert [value.value for value in models.AnnounceType] == [
        "new",
        "cross",
        "replace",
        "replace-cross",
    ]
    assert [value.value for value in models.EvidenceSource] == [
        "atom",
        "catchup",
        "oai",
    ]
    assert [value.value for value in models.Confidence] == [
        "current",
        "recovered",
        "inferred",
    ]
    assert [value.value for value in models.DateBasis] == [
        "feed_mailing",
        "catchup_mailing",
        "version_history_utc",
    ]
    assert [value.value for value in models.EnrichmentStatus] == [
        "complete",
        "empty",
        "failed",
    ]


def test_paper_metadata_is_an_immutable_tuple_based_record() -> None:
    paper = models.PaperMetadata(
        arxiv_id="2608.00001",
        title="Neutral Systems",
        authors=("A. Example",),
        abstract="A synthetic abstract.",
        primary_category="cs.SE",
        categories=("cs.SE", "math.OC"),
    )

    assert paper.authors == ("A. Example",)
    assert paper.categories == ("cs.SE", "math.OC")
    assert paper.comments == ""
    assert paper.journal_ref == ""
    assert paper.doi is None
    with pytest.raises(FrozenInstanceError):
        paper.title = "Changed"  # type: ignore[misc]


def test_paper_metadata_requires_an_unversioned_arxiv_id() -> None:
    with pytest.raises(ValueError, match="arXiv"):
        models.PaperMetadata(
            arxiv_id="2608.00001v1",
            title="Neutral Systems",
            authors=("A. Example",),
            abstract="A synthetic abstract.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "  "),
        ("authors", ()),
        ("authors", ("  ",)),
        ("categories", ()),
        ("categories", ("  ",)),
        ("primary_category", "  "),
    ],
)
def test_paper_metadata_rejects_blank_required_values(
    field: str,
    value: object,
) -> None:
    values = {
        "arxiv_id": "2608.00001",
        "title": "Neutral Systems",
        "authors": ("A. Example",),
        "abstract": "A synthetic abstract.",
        "primary_category": "cs.SE",
        "categories": ("cs.SE",),
    }
    values[field] = value

    with pytest.raises(ValueError, match="blank|at least one"):
        models.PaperMetadata(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authors", ["A. Example"]),
        ("categories", ["cs.SE"]),
        ("authors", ("A. Example", "a. example")),
        ("categories", ("cs.SE", "cs.se")),
    ],
)
def test_paper_metadata_requires_unique_immutable_tuples(
    field: str,
    value: object,
) -> None:
    values = {
        "arxiv_id": "2608.00001",
        "title": "Neutral Systems",
        "authors": ("A. Example",),
        "abstract": "A synthetic abstract.",
        "primary_category": "cs.SE",
        "categories": ("cs.SE",),
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        models.PaperMetadata(**values)  # type: ignore[arg-type]


def test_paper_version_requires_a_positive_number_and_utc_time() -> None:
    with pytest.raises(ValueError, match="positive"):
        models.PaperVersion(0, datetime(2026, 8, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="UTC"):
        models.PaperVersion(1, datetime(2026, 8, 1))
    with pytest.raises(ValueError, match="UTC"):
        models.PaperVersion(
            1,
            datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=2))),
        )

    version = models.PaperVersion(
        1,
        datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert version.number == 1


def test_event_records_keep_oai_metadata_dates_separate_from_review_dates() -> None:
    evidence = _evidence()
    candidate = models.EventCandidate(
        arxiv_id="2608.00001",
        announced_version=1,
        effective_date=date(2026, 8, 1),
        date_basis=models.DateBasis.VERSION_HISTORY_UTC,
        evidence=evidence,
    )
    event = models.ReviewEvent(
        event_id=7,
        arxiv_id="2608.00001",
        announced_version=1,
        effective_date=candidate.effective_date,
        date_basis=candidate.date_basis,
        confidence=evidence.confidence,
        evidence=(evidence,),
        queue_revision=9,
        reviewed_at=None,
    )

    assert evidence.oai_datestamp == date(2026, 8, 3)
    assert event.effective_date == date(2026, 8, 1)
    assert event.date_basis is models.DateBasis.VERSION_HISTORY_UTC
    assert event.evidence == (evidence,)


def test_source_records_expose_normalized_immutable_values() -> None:
    paper = _paper()
    version = _version()
    config = models.CategoryConfig("cs.SE", "cs:SE", date(2026, 8, 1))
    article = models.OaiArticle(
        "oai:arXiv.org:2608.00001",
        date(2026, 8, 3),
        ("cs:SE",),
        paper,
        (version,),
    )
    tombstone = models.OaiTombstone(
        "oai:arXiv.org:2608.00002",
        date(2026, 8, 3),
        ("cs:SE",),
    )
    atom_entry = models.AtomEntry(
        paper,
        version,
        models.AnnounceType.NEW,
        date(2026, 8, 1),
        0,
    )
    atom_batch = models.AtomBatch(
        "cs.SE",
        date(2026, 8, 1),
        (atom_entry,),
        "1" * 64,
        datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    catchup_entry = models.CatchupEntry(
        paper,
        models.AnnounceType.CROSS,
        date(2026, 8, 1),
        0,
    )
    catchup_page = models.CatchupPage(
        "cs.SE",
        date(2026, 8, 1),
        1,
        1,
        (catchup_entry,),
        "2" * 64,
    )
    catchup_day = models.CatchupDay(
        "cs.SE",
        date(2026, 8, 1),
        models.EnrichmentStatus.COMPLETE,
        (catchup_page,),
        None,
        None,
    )

    assert config.oai_set_spec == "cs:SE"
    assert article.versions == (version,)
    assert tombstone.set_specs == ("cs:SE",)
    assert atom_entry.announced_version == 1
    assert atom_batch.entries == (atom_entry,)
    assert catchup_entry.announced_version is None
    assert catchup_day.pages == (catchup_page,)


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: replace(_evidence(), announced_version=0),
            id="evidence-version",
        ),
        pytest.param(
            lambda: replace(_evidence(), list_position=-1),
            id="evidence-position",
        ),
        pytest.param(
            lambda: replace(_evidence(), raw_sha256="not-a-hash"),
            id="evidence-hash",
        ),
        pytest.param(
            lambda: replace(
                _evidence(), observed_at=datetime(2026, 8, 3)
            ),
            id="evidence-time",
        ),
        pytest.param(
            lambda: replace(_atom_entry(), position=-1),
            id="atom-position",
        ),
        pytest.param(
            lambda: replace(_atom_batch(), raw_sha256="f" * 63),
            id="atom-hash",
        ),
        pytest.param(
            lambda: replace(
                _atom_batch(),
                fetched_at=datetime(
                    2026,
                    8,
                    2,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            ),
            id="atom-time",
        ),
        pytest.param(
            lambda: replace(_catchup_entry(), position=-1),
            id="catchup-position",
        ),
        pytest.param(
            lambda: replace(_catchup_page(), raw_sha256="g" * 64),
            id="catchup-hash",
        ),
        pytest.param(
            lambda: replace(_review_event(), announced_version=0),
            id="review-version",
        ),
        pytest.param(
            lambda: replace(
                _review_event(), reviewed_at=datetime(2026, 8, 4)
            ),
            id="review-time",
        ),
    ],
)
def test_records_reject_invalid_versions_positions_hashes_and_times(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize(
    "versions",
    [
        (_version(2), _version(1)),
        (_version(1), _version(1)),
    ],
)
def test_oai_version_histories_must_be_strictly_increasing(
    versions: tuple[models.PaperVersion, ...],
) -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        models.OaiArticle(
            "oai:arXiv.org:2608.00001",
            date(2026, 8, 3),
            ("cs:SE",),
            _paper(),
            versions,
        )


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: models.OaiArticle(
                "oai:arXiv.org:2608.00001",
                date(2026, 8, 3),
                ["cs:SE"],  # type: ignore[arg-type]
                _paper(),
                (_version(),),
            ),
            id="oai-set-specs",
        ),
        pytest.param(
            lambda: models.OaiArticle(
                "oai:arXiv.org:2608.00001",
                date(2026, 8, 3),
                ("cs:SE",),
                _paper(),
                [_version()],  # type: ignore[arg-type]
            ),
            id="oai-versions",
        ),
        pytest.param(
            lambda: models.OaiTombstone(
                "oai:arXiv.org:2608.00002",
                date(2026, 8, 3),
                ["cs:SE"],  # type: ignore[arg-type]
            ),
            id="tombstone-set-specs",
        ),
        pytest.param(
            lambda: replace(_atom_batch(), entries=[_atom_entry()]),
            id="atom-entries",
        ),
        pytest.param(
            lambda: replace(_catchup_page(), entries=[_catchup_entry()]),
            id="catchup-entries",
        ),
        pytest.param(
            lambda: models.CatchupDay(
                "cs.SE",
                date(2026, 8, 1),
                models.EnrichmentStatus.COMPLETE,
                [_catchup_page()],  # type: ignore[arg-type]
                None,
                None,
            ),
            id="catchup-pages",
        ),
        pytest.param(
            lambda: replace(_review_event(), evidence=[_evidence()]),
            id="review-evidence",
        ),
    ],
)
def test_collection_fields_require_immutable_tuples(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(TypeError, match="tuple"):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: models.EventCandidate(
                "2608.00001v1",
                1,
                date(2026, 8, 1),
                models.DateBasis.VERSION_HISTORY_UTC,
                _evidence(),
            ),
            id="candidate-id",
        ),
        pytest.param(
            lambda: replace(_review_event(), arxiv_id="bad-id"),
            id="review-id",
        ),
        pytest.param(
            lambda: replace(_evidence(), category="  "),
            id="evidence-category",
        ),
        pytest.param(
            lambda: models.CategoryConfig("  ", "cs:SE", date(2026, 8, 1)),
            id="config-category",
        ),
        pytest.param(
            lambda: models.CategoryConfig("cs.SE", "  ", date(2026, 8, 1)),
            id="config-set-spec",
        ),
        pytest.param(
            lambda: replace(_atom_batch(), category="  "),
            id="atom-category",
        ),
        pytest.param(
            lambda: replace(_catchup_page(), category="  "),
            id="catchup-page-category",
        ),
        pytest.param(
            lambda: models.CatchupDay(
                "  ",
                date(2026, 8, 1),
                models.EnrichmentStatus.COMPLETE,
                (_catchup_page(),),
                None,
                None,
            ),
            id="catchup-day-category",
        ),
    ],
)
def test_records_require_valid_ids_and_nonblank_categories(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        factory()
