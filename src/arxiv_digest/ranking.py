"""Pure, explainable ranking for one arXiv announcement date."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import ceil, sqrt

from arxiv_digest.models import (
    EvidenceSource,
    PaperMetadata,
    ReviewEvent,
)
from arxiv_digest.profile import Profile
from arxiv_digest.text import build_tfidf_vectors, normalize_text


PHRASE_TITLE_WEIGHT = 5.0
PHRASE_ABSTRACT_WEIGHT = 2.5
KEYWORD_TITLE_WEIGHT = 3.0
KEYWORD_ABSTRACT_WEIGHT = 1.5
AUTHOR_WEIGHT = 6.0
CATEGORY_BASELINE_WEIGHT = 0.25
SEED_SIMILARITY_WEIGHT = 2.0
SAVED_SIMILARITY_WEIGHT = 0.75
TOP_SEED_SIMILARITY_FLOOR = 0.20
POSSIBLE_SIMILARITY_FLOOR = 0.10


class RankingTier(StrEnum):
    TOP = "top"
    POSSIBLE = "possible"
    OTHER = "other"


_TIER_ORDER = {
    RankingTier.TOP: 0,
    RankingTier.POSSIBLE: 1,
    RankingTier.OTHER: 2,
}

@dataclass(frozen=True, slots=True)
class RankingReason:
    kind: str
    label: str
    location: str | None


@dataclass(frozen=True, slots=True)
class RankedPaper:
    event: ReviewEvent
    paper: PaperMetadata
    tier: RankingTier
    score: float
    reasons: tuple[RankingReason, ...]


def _contains(selected: str, text: str) -> bool:
    selected_tokens = normalize_text(selected).split()
    text_tokens = normalize_text(text).split()
    width = len(selected_tokens)
    return bool(width) and any(
        text_tokens[index : index + width] == selected_tokens
        for index in range(len(text_tokens) - width + 1)
    )


def _cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    left_norm = sqrt(sum(value * value for value in left.values()))
    right_norm = sqrt(sum(value * value for value in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(
        value * right.get(term, 0.0) for term, value in left.items()
    ) / (left_norm * right_norm)


def _best_similarity(
    vector: Mapping[str, float],
    references: Mapping[str, Mapping[str, float]],
) -> tuple[float, str | None]:
    scored = tuple(
        (max(0.0, min(1.0, _cosine(vector, reference))), arxiv_id)
        for arxiv_id, reference in references.items()
    )
    if not scored:
        return 0.0, None
    score, arxiv_id = min(scored, key=lambda item: (-item[0], item[1]))
    return score, arxiv_id


def _mailing_position(event: ReviewEvent) -> int | None:
    positioned = tuple(
        observation.list_position
        for observation in event.observations
        if observation.source is EvidenceSource.CATCHUP
        and observation.daily_list_date == event.daily_list_date
        and observation.list_position is not None
    )
    return None if not positioned else min(positioned)


def _event_sort_key(event: ReviewEvent) -> tuple[str, bool, int, int]:
    return (
        event.arxiv_id,
        event.announced_version is None,
        event.announced_version or 0,
        event.event_id,
    )


def _ranking_sort_key(
    item: RankedPaper,
) -> tuple[int, float, bool, int, str, bool, int, int]:
    position = _mailing_position(item.event)
    return (
        _TIER_ORDER[item.tier],
        -item.score,
        position is None,
        position if position is not None else 0,
        *_event_sort_key(item.event),
    )


def rank_date(
    events: Iterable[ReviewEvent],
    papers: Iterable[PaperMetadata] | Mapping[str, PaperMetadata],
    profile: Profile,
    seed_vectors: Mapping[str, Mapping[str, float]],
    saved_vectors: Mapping[str, Mapping[str, float]],
    *,
    date_vectors: Mapping[str, Mapping[str, float]] | None = None,
) -> tuple[RankedPaper, ...]:
    """Rank every supplied event without mutating inputs or external state."""

    paper_values = papers.values() if isinstance(papers, Mapping) else papers
    paper_by_id = {paper.arxiv_id: paper for paper in paper_values}
    selected_categories = {
        item.category.casefold(): item for item in profile.category_coverage
    }
    ranked: list[RankedPaper] = []
    for event in events:
        paper = paper_by_id[event.arxiv_id]
        reasons: list[RankingReason] = []
        explicit_matches: set[str] = set()
        score = 0.0
        matched_categories = tuple(
            sorted(
                {
                    selected_categories[observation.category.casefold()].category
                    for observation in event.observations
                    if observation.source is EvidenceSource.CATCHUP
                    and observation.category is not None
                    and observation.daily_list_date == event.daily_list_date
                    and observation.category.casefold() in selected_categories
                    and event.daily_list_date
                    >= selected_categories[
                        observation.category.casefold()
                    ].coverage_start
                },
                key=str.casefold,
            )
        )
        if matched_categories:
            score += CATEGORY_BASELINE_WEIGHT
        for keyword in profile.keywords:
            location = None
            weight = 0.0
            if _contains(keyword, paper.title):
                location = "title"
                weight = KEYWORD_TITLE_WEIGHT
            elif _contains(keyword, paper.abstract):
                location = "abstract"
                weight = KEYWORD_ABSTRACT_WEIGHT
            if location is not None:
                reasons.append(
                    RankingReason(
                        kind="keyword",
                        label=f'Matched selected keyword “{keyword}”',
                        location=location,
                    )
                )
                score += weight
                explicit_matches.add(normalize_text(keyword))
        for phrase in profile.phrases:
            location = None
            weight = 0.0
            if _contains(phrase, paper.title):
                location = "title"
                weight = PHRASE_TITLE_WEIGHT
            elif _contains(phrase, paper.abstract):
                location = "abstract"
                weight = PHRASE_ABSTRACT_WEIGHT
            if location is not None:
                reasons.append(
                    RankingReason(
                        kind="phrase",
                        label=f'Matched selected phrase “{phrase}”',
                        location=location,
                    )
                )
                score += weight
                explicit_matches.add(normalize_text(phrase))
        normalized_authors = {normalize_text(author) for author in paper.authors}
        for author in profile.authors:
            if normalize_text(author) in normalized_authors:
                reasons.append(
                    RankingReason(
                        kind="author",
                        label=f'Matched selected author “{author}”',
                        location="authors",
                    )
                )
                score += AUTHOR_WEIGHT
                explicit_matches.add(normalize_text(author))
        if matched_categories:
            reasons.append(
                RankingReason(
                    kind="category",
                    label="Selected category: " + ", ".join(matched_categories),
                    location=None,
                )
            )
        tier = (
            RankingTier.TOP
            if any(
                reason.kind == "author"
                or (reason.kind == "phrase" and reason.location == "title")
                for reason in reasons
            )
            or len(explicit_matches) >= 2
            else RankingTier.POSSIBLE
            if explicit_matches
            else RankingTier.OTHER
        )
        ranked.append(RankedPaper(event, paper, tier, score, tuple(reasons)))

    if date_vectors is None:
        date_vectors = build_tfidf_vectors(
            {
                item.paper.arxiv_id: item.paper
                for item in ranked
            }.values(),
            ngram_range=(1, 3),
        )

    selected_seed_vectors = {
        arxiv_id: seed_vectors[arxiv_id]
        for arxiv_id in profile.seed_papers
        if arxiv_id in seed_vectors
    }
    similarities: dict[int, tuple[float, str | None, float, str | None]] = {}
    for item in ranked:
        vector = date_vectors.get(item.paper.arxiv_id, {})
        seed_score, seed_id = _best_similarity(vector, selected_seed_vectors)
        saved_score, saved_id = _best_similarity(vector, saved_vectors)
        similarities[item.event.event_id] = (
            seed_score,
            seed_id,
            saved_score,
            saved_id,
        )

    seed_order = sorted(
        ranked,
        key=lambda item: (
            -similarities[item.event.event_id][0],
            *_event_sort_key(item.event),
        ),
    )
    paper_order = sorted(
        ranked,
        key=lambda item: (
            -max(
                similarities[item.event.event_id][0],
                similarities[item.event.event_id][2],
            ),
            *_event_sort_key(item.event),
        ),
    )
    seed_ranks = {
        item.event.event_id: index for index, item in enumerate(seed_order)
    }
    paper_ranks = {
        item.event.event_id: index for index, item in enumerate(paper_order)
    }
    top_tenth = max(1, ceil(0.10 * len(ranked))) if ranked else 0
    top_third = max(1, ceil(len(ranked) / 3)) if ranked else 0

    reranked: list[RankedPaper] = []
    for item in ranked:
        event_id = item.event.event_id
        seed_score, seed_id, saved_score, saved_id = similarities[event_id]
        reasons = list(item.reasons)
        if seed_score > 0.0 and seed_id is not None:
            reasons.append(
                RankingReason(
                    kind="seed_similarity",
                    label=f"Related to selected seed paper {seed_id}",
                    location=None,
                )
            )
        if saved_score > 0.0 and saved_id is not None:
            reasons.append(
                RankingReason(
                    kind="saved_similarity",
                    label=f"Similar to saved paper {saved_id}",
                    location=None,
                )
            )
        score = (
            item.score
            + SEED_SIMILARITY_WEIGHT * seed_score
            + SAVED_SIMILARITY_WEIGHT * saved_score
        )
        paper_similarity = max(seed_score, saved_score)
        tier = item.tier
        if (
            tier is not RankingTier.TOP
            and seed_id is not None
            and seed_score >= TOP_SEED_SIMILARITY_FLOOR
            and seed_ranks[event_id] < top_tenth
        ):
            tier = RankingTier.TOP
        elif (
            tier is RankingTier.OTHER
            and (seed_id is not None or saved_id is not None)
            and paper_similarity >= POSSIBLE_SIMILARITY_FLOOR
            and paper_ranks[event_id] < top_third
        ):
            tier = RankingTier.POSSIBLE
        reranked.append(
            RankedPaper(item.event, item.paper, tier, score, tuple(reasons))
        )
    return tuple(sorted(reranked, key=_ranking_sort_key))
