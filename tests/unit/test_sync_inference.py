from datetime import date, datetime, timezone

from arxiv_digest.models import (
    AnnounceType,
    Confidence,
    DateBasis,
    EventCandidate,
    EventEvidence,
    EvidenceSource,
    OaiArticle,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.sync import associate_evidence, diff_oai_article
from arxiv_digest.storage.store import StoredArticleSnapshot


def _paper(
    *,
    categories: tuple[str, ...] = ("cs.SE",),
    title: str = "Synthetic Recovery Systems",
) -> PaperMetadata:
    return PaperMetadata(
        arxiv_id="2608.03001",
        title=title,
        authors=("A. Example",),
        abstract="A fictional abstract about conservative recovery.",
        primary_category="cs.SE",
        categories=categories,
    )


def _version(number: int, day: int) -> PaperVersion:
    return PaperVersion(
        number,
        datetime(2026, 8, day, 12, tzinfo=timezone.utc),
    )


def _article(*versions: PaperVersion, metadata: PaperMetadata | None = None):
    return OaiArticle(
        oai_identifier="oai:arXiv.org:2608.03001",
        oai_datestamp=date(2026, 8, 20),
        set_specs=("cs:SE",),
        metadata=_paper() if metadata is None else metadata,
        versions=tuple(versions),
    )


def _snapshot(
    *versions: PaperVersion,
    categories: tuple[str, ...] = ("cs.SE",),
) -> StoredArticleSnapshot:
    return StoredArticleSnapshot(
        category="cs.SE",
        metadata=_paper(categories=categories),
        versions=tuple(versions),
        observed_categories=categories,
        last_oai_datestamp=date(2026, 8, 1),
        last_raw_sha256="e" * 64,
        last_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def test_first_version_inside_coverage_becomes_inferred_new() -> None:
    result = diff_oai_article(
        None,
        _article(_version(1, 5)),
        "cs.SE",
        date(2026, 8, 1),
        set_spec="cs:SE",
        raw_sha256="d" * 64,
        observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert len(result) == 1
    candidate = result[0]
    assert candidate.announced_version == 1
    assert candidate.effective_date == date(2026, 8, 5)
    assert candidate.date_basis is DateBasis.VERSION_HISTORY_UTC
    assert candidate.evidence.source is EvidenceSource.OAI
    assert candidate.evidence.confidence is Confidence.INFERRED
    assert candidate.evidence.announce_type is AnnounceType.NEW
    assert candidate.evidence.oai_datestamp == date(2026, 8, 20)


def test_each_new_higher_version_becomes_a_distinct_replacement() -> None:
    result = diff_oai_article(
        _snapshot(_version(1, 2)),
        _article(_version(1, 2), _version(2, 5), _version(3, 9)),
        "cs.SE",
        date(2026, 8, 1),
        set_spec="cs:SE",
        raw_sha256="f" * 64,
        observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert [candidate.announced_version for candidate in result] == [2, 3]
    assert [candidate.effective_date for candidate in result] == [
        date(2026, 8, 5),
        date(2026, 8, 9),
    ]
    assert all(
        candidate.evidence.announce_type is AnnounceType.REPLACE
        for candidate in result
    )


def test_selected_secondary_category_requires_proven_prior_absence() -> None:
    previous = _snapshot(_version(1, 5), categories=("cs.SE",))
    current = _article(
        _version(1, 5),
        metadata=_paper(categories=("cs.SE", "math.LO")),
    )

    result = diff_oai_article(
        previous,
        current,
        "math.LO",
        date(2026, 8, 1),
        set_spec="math:LO",
        raw_sha256="1" * 64,
        observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert len(result) == 1
    candidate = result[0]
    assert candidate.announced_version is None
    assert candidate.effective_date == date(2026, 8, 5)
    assert candidate.evidence.announce_type is AnnounceType.CROSS
    assert candidate.evidence.source_key.startswith(
        "oai-category:math.LO:2608.03001:"
    )

    already_present = _snapshot(
        _version(1, 5), categories=("cs.SE", "math.LO")
    )
    assert (
        diff_oai_article(
            already_present,
            current,
            "math.LO",
            date(2026, 8, 1),
            set_spec="math:LO",
            raw_sha256="2" * 64,
            observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        == ()
    )


def test_old_and_administrative_only_oai_changes_do_not_create_events() -> None:
    coverage_start = date(2026, 8, 1)
    old = OaiArticle(
        oai_identifier="oai:arXiv.org:2608.03001",
        oai_datestamp=date(2026, 8, 20),
        set_specs=("cs:SE",),
        metadata=_paper(),
        versions=(
            PaperVersion(
                1, datetime(2026, 7, 1, tzinfo=timezone.utc)
            ),
        ),
    )
    assert (
        diff_oai_article(
            None,
            old,
            "cs.SE",
            coverage_start,
            raw_sha256="3" * 64,
            observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        == ()
    )

    prior = _snapshot(_version(1, 5))
    administrative = _article(
        _version(1, 5), metadata=_paper(title="Revised synthetic title")
    )
    assert (
        diff_oai_article(
            prior,
            administrative,
            "cs.SE",
            coverage_start,
            raw_sha256="4" * 64,
            observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        == ()
    )


def _observed_candidate(
    *,
    source: EvidenceSource,
    confidence: Confidence,
    effective_date: date,
    announced_version: int | None,
    source_key: str,
) -> EventCandidate:
    announce_type = (
        AnnounceType.NEW
        if source is not EvidenceSource.CATCHUP
        else AnnounceType.CROSS
    )
    evidence = EventEvidence(
        source_key=source_key,
        source=source,
        confidence=confidence,
        category="cs.SE",
        announce_type=announce_type,
        mailing_date=(
            effective_date
            if source in {EvidenceSource.ATOM, EvidenceSource.CATCHUP}
            else None
        ),
        announced_version=announced_version,
        list_position=0 if source is not EvidenceSource.OAI else None,
        oai_datestamp=(
            date(2026, 8, 20) if source is EvidenceSource.OAI else None
        ),
        raw_sha256="5" * 64,
        observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    return EventCandidate(
        arxiv_id="2608.03001",
        announced_version=announced_version,
        effective_date=effective_date,
        date_basis=(
            DateBasis.VERSION_HISTORY_UTC
            if source is EvidenceSource.OAI
            else (
                DateBasis.FEED_MAILING
                if source is EvidenceSource.ATOM
                else DateBasis.CATCHUP_MAILING
            )
        ),
        evidence=evidence,
    )


def test_atom_associates_versionless_catchup_and_inferred_version() -> None:
    inferred = _observed_candidate(
        source=EvidenceSource.OAI,
        confidence=Confidence.INFERRED,
        effective_date=date(2026, 8, 6),
        announced_version=1,
        source_key="oai:cs:SE:2608.03001:v1",
    )
    catchup = _observed_candidate(
        source=EvidenceSource.CATCHUP,
        confidence=Confidence.RECOVERED,
        effective_date=date(2026, 8, 6),
        announced_version=None,
        source_key="catchup:cs.SE:2026-08-06:2608.03001:cross",
    )
    atom = _observed_candidate(
        source=EvidenceSource.ATOM,
        confidence=Confidence.CURRENT,
        effective_date=date(2026, 8, 6),
        announced_version=1,
        source_key="atom:cs.SE:2026-08-06:2608.03001:v1:cross",
    )

    result = associate_evidence((inferred, catchup, atom))

    assert len(result) == 1
    associated = result[0]
    assert associated.announced_version == 1
    assert associated.effective_date == date(2026, 8, 6)
    assert associated.date_basis is DateBasis.FEED_MAILING
    assert associated.confidence is Confidence.CURRENT
    assert {item.source for item in associated.evidence} == {
        EvidenceSource.OAI,
        EvidenceSource.CATCHUP,
        EvidenceSource.ATOM,
    }


def test_ambiguous_inferred_versions_leave_catchup_versionless() -> None:
    first = _observed_candidate(
        source=EvidenceSource.OAI,
        confidence=Confidence.INFERRED,
        effective_date=date(2026, 8, 6),
        announced_version=1,
        source_key="oai:cs:SE:2608.03001:v1",
    )
    second = _observed_candidate(
        source=EvidenceSource.OAI,
        confidence=Confidence.INFERRED,
        effective_date=date(2026, 8, 6),
        announced_version=2,
        source_key="oai:cs:SE:2608.03001:v2",
    )
    catchup = _observed_candidate(
        source=EvidenceSource.CATCHUP,
        confidence=Confidence.RECOVERED,
        effective_date=date(2026, 8, 6),
        announced_version=None,
        source_key="catchup:cs.SE:2026-08-06:2608.03001:replace",
    )

    result = associate_evidence((first, second, catchup))

    assert len(result) == 3
    assert [item.announced_version for item in result] == [1, 2, None]


def test_two_atom_versions_on_one_date_never_merge_through_catchup() -> None:
    first = _observed_candidate(
        source=EvidenceSource.ATOM,
        confidence=Confidence.CURRENT,
        effective_date=date(2026, 8, 6),
        announced_version=1,
        source_key="atom:cs.SE:2026-08-06:2608.03001:v1:replace",
    )
    second = _observed_candidate(
        source=EvidenceSource.ATOM,
        confidence=Confidence.CURRENT,
        effective_date=date(2026, 8, 6),
        announced_version=2,
        source_key="atom:cs.SE:2026-08-06:2608.03001:v2:replace",
    )
    catchup = _observed_candidate(
        source=EvidenceSource.CATCHUP,
        confidence=Confidence.RECOVERED,
        effective_date=date(2026, 8, 6),
        announced_version=None,
        source_key="catchup:cs.SE:2026-08-06:2608.03001:replace",
    )

    result = associate_evidence((first, second, catchup))

    assert len(result) == 3
    assert [item.announced_version for item in result] == [1, 2, None]
