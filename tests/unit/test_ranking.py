from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from math import isclose, sqrt
from pathlib import Path

from arxiv_digest.models import (
    AnnounceType,
    EvidenceSource,
    PaperMetadata,
    ReviewEvent,
    SourceObservation,
    VersionResolution,
)
from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory


DAY = date(2026, 8, 22)


def _paper(
    arxiv_id: str,
    *,
    title: str,
    abstract: str = "A neutral synthetic abstract.",
    authors: tuple[str, ...] = ("Aster Vale",),
    categories: tuple[str, ...] = ("synthetic.alpha",),
) -> PaperMetadata:
    return PaperMetadata(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        abstract=abstract,
        primary_category=categories[0],
        categories=categories,
    )


def _event(
    arxiv_id: str,
    *,
    support_categories: tuple[str, ...] = ("synthetic.alpha",),
) -> ReviewEvent:
    return ReviewEvent(
        event_id=int(arxiv_id.rsplit(".", 1)[-1]),
        arxiv_id=arxiv_id,
        daily_list_date=DAY,
        announced_version=1,
        version_resolution=VersionResolution.CHRONOLOGY_MATCHED,
        observations=tuple(
            SourceObservation(
                source_key=f"catchup:{category}:{arxiv_id}",
                arxiv_id=arxiv_id,
                source=EvidenceSource.CATCHUP,
                category=category,
                announce_type=AnnounceType.NEW,
                daily_list_date=DAY,
                announced_version=None,
                list_position=None,
                oai_datestamp=None,
                response_sha256="a" * 64,
                observed_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
            )
            for category in support_categories
        ),
        queue_revision=1,
        reviewed_at=None,
    )


def _positioned_event(arxiv_id: str, position: int | None) -> ReviewEvent:
    evidence = ()
    if position is not None:
        evidence = (
            SourceObservation(
                source_key=f"catchup:synthetic.alpha:{arxiv_id}",
                arxiv_id=arxiv_id,
                source=EvidenceSource.CATCHUP,
                category="synthetic.alpha",
                announce_type=AnnounceType.NEW,
                daily_list_date=DAY,
                announced_version=None,
                list_position=position,
                oai_datestamp=None,
                response_sha256="a" * 64,
                observed_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
            ),
        )
    base = _event(arxiv_id)
    return ReviewEvent(
        event_id=base.event_id,
        arxiv_id=base.arxiv_id,
        daily_list_date=base.daily_list_date,
        announced_version=base.announced_version,
        version_resolution=base.version_resolution,
        observations=evidence,
        queue_revision=base.queue_revision,
        reviewed_at=base.reviewed_at,
    )


def _profile(
    *,
    categories: tuple[str, ...] = ("synthetic.alpha",),
    keywords: tuple[str, ...] = (),
    phrases: tuple[str, ...] = (),
    authors: tuple[str, ...] = (),
    seed_papers: tuple[str, ...] = (),
) -> Profile:
    return Profile(
        schema_version=2,
        revision=1,
        category_coverage=tuple(
            ProfileCategory(category, date(2026, 8, 1))
            for category in categories
        ),
        keywords=keywords,
        phrases=phrases,
        authors=authors,
        seed_papers=seed_papers,
        pdf_destination=PdfDestination("downloads", Path("/tmp/ranking-papers")),
    )


def test_exact_normalized_phrase_in_title_is_a_top_match_with_reason() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    paper = _paper(
        "2608.01001",
        title="Graph neural networks: a synthetic survey",
    )

    ranked = rank_date(
        (_event(paper.arxiv_id),),
        (paper,),
        _profile(phrases=("Graph—Neural Networks",)),
        {},
        {},
    )

    assert len(ranked) == 1
    assert ranked[0].tier is RankingTier.TOP
    assert ranked[0].reasons[0].kind == "phrase"
    assert "Graph—Neural Networks" in ranked[0].reasons[0].label
    assert ranked[0].reasons[0].location == "title"


def test_keyword_matches_are_exact_and_title_outweighs_abstract() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    papers = (
        _paper("2608.01002", title="Orbit methods"),
        _paper(
            "2608.01003",
            title="Synthetic methods",
            abstract="An ORBIT method appears here.",
        ),
        _paper("2608.01004", title="Orbital methods"),
    )

    ranked = rank_date(
        tuple(_event(paper.arxiv_id) for paper in papers),
        papers,
        _profile(keywords=("orbit",)),
        {},
        {},
    )
    by_id = {item.paper.arxiv_id: item for item in ranked}

    assert by_id["2608.01002"].tier is RankingTier.POSSIBLE
    assert by_id["2608.01003"].tier is RankingTier.POSSIBLE
    assert by_id["2608.01002"].score > by_id["2608.01003"].score
    assert by_id["2608.01002"].reasons[0].location == "title"
    assert by_id["2608.01003"].reasons[0].location == "abstract"
    assert by_id["2608.01004"].tier is RankingTier.OTHER


def test_abstract_phrase_is_possible_and_same_phrase_counts_once() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    papers = (
        _paper(
            "2608.01005",
            title="Synthetic methods",
            abstract="We study lattice flow in a neutral setting.",
        ),
        _paper(
            "2608.01006",
            title="Lattice flow methods",
            abstract="Lattice flow also appears here.",
        ),
    )

    ranked = rank_date(
        tuple(_event(paper.arxiv_id) for paper in papers),
        papers,
        _profile(phrases=("lattice flow",)),
        {},
        {},
    )
    by_id = {item.paper.arxiv_id: item for item in ranked}

    assert by_id["2608.01005"].tier is RankingTier.POSSIBLE
    assert by_id["2608.01005"].reasons[0].location == "abstract"
    assert by_id["2608.01006"].tier is RankingTier.TOP
    assert len([reason for reason in by_id["2608.01006"].reasons if reason.kind == "phrase"]) == 1


def test_selected_author_match_is_top_and_names_the_author() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    paper = _paper(
        "2608.01007",
        title="Neutral methods",
        authors=("Aster Vale", "River Moss"),
    )

    ranked = rank_date(
        (_event(paper.arxiv_id),),
        (paper,),
        _profile(authors=("ASTER VALE",)),
        {},
        {},
    )

    assert ranked[0].tier is RankingTier.TOP
    assert ranked[0].reasons[0].kind == "author"
    assert "ASTER VALE" in ranked[0].reasons[0].label
    assert ranked[0].reasons[0].location == "authors"


def test_two_distinct_explicit_values_are_top_but_duplicate_value_counts_once() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    distinct = _paper(
        "2608.01008",
        title="Orbit methods",
        abstract="The analysis uses lattice flow.",
    )
    duplicate = _paper(
        "2608.01009",
        title="Neutral methods",
        abstract="The analysis follows an orbit.",
    )

    distinct_result = rank_date(
        (_event(distinct.arxiv_id),),
        (distinct,),
        _profile(keywords=("orbit",), phrases=("lattice flow",)),
        {},
        {},
    )[0]
    duplicate_result = rank_date(
        (_event(duplicate.arxiv_id),),
        (duplicate,),
        _profile(keywords=("orbit",), phrases=("ORBIT",)),
        {},
        {},
    )[0]

    assert distinct_result.tier is RankingTier.TOP
    assert duplicate_result.tier is RankingTier.POSSIBLE


def test_selected_categories_receive_one_equal_order_independent_baseline() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    papers = (
        _paper("2608.01010", title="Alpha", categories=("synthetic.alpha",)),
        _paper("2608.01011", title="Beta", categories=("synthetic.beta",)),
        _paper(
            "2608.01012",
            title="Both",
            categories=("synthetic.alpha", "synthetic.beta"),
        ),
        # Current metadata can include a selected category without making the
        # recovered announcement visible or eligible for its baseline.
        _paper("2608.01013", title="Outside", categories=("synthetic.alpha",)),
    )
    events = (
        _event("2608.01010", support_categories=("synthetic.alpha",)),
        _event("2608.01011", support_categories=("synthetic.beta",)),
        _event(
            "2608.01012",
            support_categories=("synthetic.alpha", "synthetic.beta"),
        ),
        _event("2608.01013", support_categories=("synthetic.gamma",)),
    )

    forward = rank_date(
        events,
        papers,
        _profile(categories=("synthetic.alpha", "synthetic.beta")),
        {},
        {},
    )
    reverse = rank_date(
        events,
        papers,
        _profile(categories=("synthetic.beta", "synthetic.alpha")),
        {},
        {},
    )
    forward_scores = {item.paper.arxiv_id: item.score for item in forward}
    reverse_scores = {item.paper.arxiv_id: item.score for item in reverse}

    assert forward_scores == reverse_scores
    assert forward_scores["2608.01010"] == forward_scores["2608.01011"]
    assert forward_scores["2608.01011"] == forward_scores["2608.01012"]
    assert forward_scores["2608.01010"] > forward_scores["2608.01013"]
    assert all(item.tier is RankingTier.OTHER for item in forward)


def test_seed_similarity_uses_complete_date_top_tenth_and_top_third() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    papers = (_paper("2608.01020", title="signal", abstract=""),) + tuple(
        _paper(
            f"2608.{1020 + index:05d}",
            title=f"signal filler{index}",
            abstract="",
        )
        for index in range(1, 10)
    )

    ranked = rank_date(
        tuple(_event(paper.arxiv_id) for paper in papers),
        papers,
        _profile(seed_papers=("2501.00001",)),
        {"2501.00001": {"signal": 1.0}},
        {},
    )
    by_id = {item.paper.arxiv_id: item for item in ranked}

    assert len(ranked) == 10
    assert by_id["2608.01020"].tier is RankingTier.TOP
    assert by_id["2608.01021"].tier is RankingTier.POSSIBLE
    assert by_id["2608.01023"].tier is RankingTier.POSSIBLE
    assert by_id["2608.01024"].tier is RankingTier.OTHER
    reason = next(
        reason
        for reason in by_id["2608.01020"].reasons
        if reason.kind == "seed_similarity"
    )
    assert "2501.00001" in reason.label
    assert reason.location is None


def test_versions_of_one_paper_have_distinct_stable_similarity_ranks() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    paper = _paper("2608.01999", title="signal", abstract="")
    version_one = replace(
        _event(paper.arxiv_id), event_id=9001, announced_version=1
    )
    version_two = replace(
        _event(paper.arxiv_id), event_id=9002, announced_version=2
    )
    fillers = tuple(
        _paper(f"2608.{2000 + index:05d}", title=f"neutral {index}", abstract="")
        for index in range(8)
    )
    events = (
        version_two,
        *(_event(filler.arxiv_id) for filler in fillers),
        version_one,
    )

    forward = rank_date(
        events,
        (paper, *fillers),
        _profile(seed_papers=("2501.00015",)),
        {"2501.00015": {"signal": 1.0}},
        {},
    )
    reverse = rank_date(
        tuple(reversed(events)),
        (paper, *fillers),
        _profile(seed_papers=("2501.00015",)),
        {"2501.00015": {"signal": 1.0}},
        {},
    )

    expected = (
        (1, RankingTier.TOP),
        (2, RankingTier.POSSIBLE),
    )
    assert tuple(
        (item.event.announced_version, item.tier)
        for item in forward
        if item.paper.arxiv_id == paper.arxiv_id
    ) == expected
    assert tuple(
        (item.event.announced_version, item.tier)
        for item in reverse
        if item.paper.arxiv_id == paper.arxiv_id
    ) == expected


def test_top_tenth_is_ranked_by_seed_similarity_not_saved_similarity() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    papers = (
        _paper("2608.01070", title="alpha", abstract=""),
        _paper("2608.01071", title="beta", abstract=""),
    ) + tuple(
        _paper(
            f"2608.{1072 + index:05d}",
            title=f"neutral{index}",
            abstract="",
        )
        for index in range(8)
    )
    seed_vector = {
        "alpha": 0.25,
        "beta": sqrt(1.0 - 0.25 * 0.25),
    }

    ranked = rank_date(
        tuple(_event(paper.arxiv_id) for paper in papers),
        papers,
        _profile(seed_papers=("2501.00012",)),
        {"2501.00012": seed_vector},
        {"2501.00013": {"alpha": 1.0}},
    )
    by_id = {item.paper.arxiv_id: item for item in ranked}

    assert by_id["2608.01071"].tier is RankingTier.TOP
    assert by_id["2608.01070"].tier is RankingTier.POSSIBLE


def test_top_third_is_ranked_by_best_seed_or_saved_similarity() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    papers = (
        _paper("2608.01080", title="neutral one", abstract=""),
        _paper("2608.01081", title="neutral two", abstract=""),
        _paper("2608.01089", title="alpha", abstract=""),
    )

    ranked = rank_date(
        tuple(_event(paper.arxiv_id) for paper in papers),
        papers,
        _profile(),
        {},
        {"2501.00014": {"alpha": 1.0}},
    )
    by_id = {item.paper.arxiv_id: item for item in ranked}

    assert by_id["2608.01089"].tier is RankingTier.POSSIBLE
    assert by_id["2608.01080"].tier is RankingTier.OTHER


def test_similarity_uses_date_wide_tfidf_vectors() -> None:
    from arxiv_digest.ranking import (
        CATEGORY_BASELINE_WEIGHT,
        SEED_SIMILARITY_WEIGHT,
        rank_date,
    )
    from arxiv_digest.text import build_tfidf_vectors

    papers = (
        _paper("2608.01028", title="common rare", abstract=""),
        _paper("2608.01029", title="common other", abstract=""),
    )
    vectors = build_tfidf_vectors(papers)

    ranked = rank_date(
        tuple(_event(paper.arxiv_id) for paper in papers),
        papers,
        _profile(seed_papers=("2501.00009",)),
        {"2501.00009": {"rare": 1.0}},
        {},
    )
    first = next(item for item in ranked if item.paper.arxiv_id == "2608.01028")
    expected = (
        CATEGORY_BASELINE_WEIGHT
        + SEED_SIMILARITY_WEIGHT * vectors["2608.01028"]["rare"]
    )

    assert isclose(first.score, expected, rel_tol=1e-12)


def test_similarity_tiers_enforce_numeric_floors() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    paper = _paper("2608.01030", title="signal", abstract="")

    def tier_for(similarity: float) -> RankingTier:
        return rank_date(
            (_event(paper.arxiv_id),),
            (paper,),
            _profile(seed_papers=("2501.00002",)),
            {
                "2501.00002": {
                    "signal": similarity,
                    "orthogonal": sqrt(1.0 - similarity * similarity),
                }
            },
            {},
        )[0].tier

    assert tier_for(0.20) is RankingTier.TOP
    assert tier_for(0.19) is RankingTier.POSSIBLE
    assert tier_for(0.09) is RankingTier.OTHER


def test_saved_paper_similarity_is_explained_and_lower_weight_than_seed() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    paper = _paper("2608.01031", title="signal", abstract="")
    event = _event(paper.arxiv_id)
    seed_result = rank_date(
        (event,),
        (paper,),
        _profile(seed_papers=("2501.00003",)),
        {"2501.00003": {"signal": 1.0}},
        {},
    )[0]
    saved_result = rank_date(
        (event,),
        (paper,),
        _profile(),
        {},
        {"2501.00004": {"signal": 1.0}},
    )[0]

    assert saved_result.tier is RankingTier.POSSIBLE
    assert seed_result.score > saved_result.score
    reason = next(
        reason for reason in saved_result.reasons if reason.kind == "saved_similarity"
    )
    assert "2501.00004" in reason.label


def test_equal_scores_sort_by_position_then_id_with_missing_position_last() -> None:
    from arxiv_digest.ranking import rank_date

    papers = tuple(
        _paper(arxiv_id, title="Neutral")
        for arxiv_id in (
            "2608.01039",
            "2608.01040",
            "2608.01041",
            "2608.01042",
        )
    )
    events = (
        _positioned_event("2608.01039", None),
        _positioned_event("2608.01042", 1),
        _positioned_event("2608.01040", 5),
        _positioned_event("2608.01041", 1),
    )

    ranked = rank_date(events, papers, _profile(), {}, {})
    reranked = rank_date(tuple(reversed(events)), papers, _profile(), {}, {})

    expected = (
        "2608.01041",
        "2608.01042",
        "2608.01040",
        "2608.01039",
    )
    assert tuple(item.paper.arxiv_id for item in ranked) == expected
    assert tuple(item.paper.arxiv_id for item in reranked) == expected


def test_position_is_smallest_among_active_catchup_support() -> None:
    from arxiv_digest.ranking import rank_date

    stronger = _positioned_event("2608.01045", 8)
    other_support = SourceObservation(
        source_key="catchup:2608.01045",
        arxiv_id="2608.01045",
        source=EvidenceSource.CATCHUP,
        category="synthetic.alpha",
        announce_type=AnnounceType.NEW,
        daily_list_date=DAY,
        announced_version=None,
        list_position=1,
        oai_datestamp=None,
        response_sha256="b" * 64,
        observed_at=datetime(2026, 8, 23, 12, tzinfo=timezone.utc),
    )
    mixed = replace(
        stronger,
        observations=(other_support, stronger.observations[0]),
    )
    middle = _positioned_event("2608.01046", 5)
    papers = (
        _paper("2608.01045", title="Neutral"),
        _paper("2608.01046", title="Neutral"),
    )

    ranked = rank_date((mixed, middle), papers, _profile(), {}, {})

    assert tuple(item.paper.arxiv_id for item in ranked) == (
        "2608.01045",
        "2608.01046",
    )


def test_sorting_prioritizes_tier_then_descending_score() -> None:
    from arxiv_digest.ranking import rank_date

    papers = (
        _paper("2608.01053", title="Neutral", authors=("Aster Vale",)),
        _paper("2608.01052", title="Lattice flow", authors=("River Moss",)),
        _paper("2608.01051", title="Orbit", authors=("River Moss",)),
        _paper("2608.01050", title="Neutral", authors=("River Moss",)),
    )
    profile = _profile(
        keywords=("orbit",),
        phrases=("lattice flow",),
        authors=("Aster Vale",),
    )

    ranked = rank_date(
        tuple(_event(paper.arxiv_id) for paper in reversed(papers)),
        papers,
        profile,
        {},
        {},
    )

    assert tuple(item.paper.arxiv_id for item in ranked) == (
        "2608.01053",
        "2608.01052",
        "2608.01051",
        "2608.01050",
    )


def test_neutral_profile_keeps_all_papers_in_other() -> None:
    from arxiv_digest.ranking import RankingTier, rank_date

    papers = (
        _paper("2608.01060", title="First neutral paper"),
        _paper("2608.01061", title="Second neutral paper"),
    )

    ranked = rank_date(
        tuple(_event(paper.arxiv_id) for paper in papers),
        papers,
        _profile(categories=("synthetic.unselected",)),
        {},
        {},
    )

    assert {item.paper.arxiv_id for item in ranked} == {
        "2608.01060",
        "2608.01061",
    }
    assert all(item.tier is RankingTier.OTHER for item in ranked)


def test_ranking_preserves_review_state_and_has_no_io_side_effects(monkeypatch) -> None:
    import socket
    import sqlite3

    from arxiv_digest.ranking import rank_date

    reviewed_at = datetime(2026, 8, 23, 2, tzinfo=timezone.utc)
    event = replace(_event("2608.01062"), reviewed_at=reviewed_at)
    paper = _paper("2608.01062", title="Signal", abstract="")
    profile = _profile(seed_papers=("2501.00010",))
    seed_vectors = {"2501.00010": {"signal": 1.0}}
    saved_vectors = {"2501.00011": {"signal": 1.0}}
    original_seed = {key: dict(value) for key, value in seed_vectors.items()}
    original_saved = {key: dict(value) for key, value in saved_vectors.items()}

    def unexpected_io(*args, **kwargs):
        raise AssertionError("pure ranking attempted external I/O")

    monkeypatch.setattr(sqlite3, "connect", unexpected_io)
    monkeypatch.setattr(socket, "create_connection", unexpected_io)

    ranked = rank_date(
        (event,),
        {paper.arxiv_id: paper},
        profile,
        seed_vectors,
        saved_vectors,
    )

    assert ranked[0].event is event
    assert ranked[0].event.reviewed_at == reviewed_at
    assert profile == _profile(seed_papers=("2501.00010",))
    assert seed_vectors == original_seed
    assert saved_vectors == original_saved
