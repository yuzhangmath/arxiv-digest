from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone

import pytest

from arxiv_digest.models import OaiArticle, OaiTombstone, PaperMetadata, PaperVersion
from arxiv_digest.sources.oai import OaiProtocolError


def _document(
    arxiv_id: str = "2608.00001",
    *,
    submitted: date = date(2026, 7, 1),
    evidence_dates: tuple[date, ...] = (),
    categories: tuple[str, ...] = ("synthetic.alpha",),
):
    from arxiv_digest.candidates import CandidateDocument

    return CandidateDocument(
        paper=PaperMetadata(
            arxiv_id=arxiv_id,
            title=f"Synthetic title {arxiv_id}",
            authors=("Aster Vale",),
            abstract="A domain-neutral synthetic abstract.",
            primary_category=categories[0],
            categories=categories,
        ),
        versions=(
            PaperVersion(
                number=1,
                submitted_at=datetime.combine(
                    submitted,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ),
            ),
        ),
        eligible_categories=categories,
        evidence_dates=evidence_dates,
    )


def _corpus(*documents):
    from arxiv_digest.candidates import CandidateCorpus

    return CandidateCorpus(
        schema_version=1,
        categories=("synthetic.alpha",),
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        source_hashes=("c" * 64,),
        documents=tuple(documents),
    )


def _rich_document(
    arxiv_id: str,
    category: str,
    *,
    title: str,
    abstract: str,
    authors: tuple[str, ...],
    day: date,
):
    from arxiv_digest.candidates import CandidateDocument

    return CandidateDocument(
        paper=PaperMetadata(
            arxiv_id=arxiv_id,
            title=title,
            authors=authors,
            abstract=abstract,
            primary_category=category,
            categories=(category,),
        ),
        versions=(
            PaperVersion(
                number=1,
                submitted_at=datetime.combine(
                    day,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ),
            ),
        ),
        eligible_categories=(category,),
        evidence_dates=(),
    )


def test_candidate_document_is_immutable_and_eligibility_is_inclusive() -> None:
    from arxiv_digest.candidates import corpus_limits, is_candidate_eligible

    document = _document(submitted=date(2026, 5, 1), evidence_dates=(date(2026, 6, 1),))

    assert is_candidate_eligible(
        document,
        window_start=date(2026, 6, 1),
        window_end=date(2026, 8, 29),
    )
    assert not is_candidate_eligible(
        document,
        window_start=date(2026, 6, 2),
        window_end=date(2026, 8, 29),
    )
    assert corpus_limits() == (500, 2_000)
    with pytest.raises(FrozenInstanceError):
        document.evidence_dates = ()  # type: ignore[misc]


def test_candidate_cache_round_trips_exact_shard_and_expires_after_seven_days(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import CandidateCache, CandidateCategoryShard

    created_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    current = [created_at]
    shard = CandidateCategoryShard(
        schema_version=1,
        category="synthetic.alpha",
        set_spec="synthetic:alpha",
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        created_at=created_at,
        source_hashes=("a" * 64,),
        documents=(_document(),),
        completed_strata=(0,),
        continuation_by_stratum=((1, "opaque+/token=="),),
        pages_fetched=2,
        exhausted=False,
    )
    cache = CandidateCache(tmp_path, clock=lambda: current[0])

    cache.save_shard(shard)

    assert cache.load_shard("synthetic.alpha", "synthetic:alpha") == shard
    assert (cache.shard_path("synthetic.alpha", "synthetic:alpha").stat().st_mode & 0o777) == 0o600
    current[0] = created_at + timedelta(days=7, microseconds=1)
    assert cache.load_shard("synthetic.alpha", "synthetic:alpha") is None


def test_candidate_cache_hydrates_only_the_exact_persisted_corpus_hash(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import (
        CandidateCache,
        CandidateCategoryShard,
        candidate_corpus_hash,
        derive_candidate_corpus,
    )
    from arxiv_digest.models import CategoryConfig

    created_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    shard = CandidateCategoryShard(
        schema_version=1,
        category="synthetic.alpha",
        set_spec="synthetic:alpha",
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        created_at=created_at,
        source_hashes=("a" * 64,),
        documents=(_document(),),
        completed_strata=tuple(range(18)),
        continuation_by_stratum=(),
        pages_fetched=18,
        exhausted=True,
    )
    cache = CandidateCache(tmp_path, clock=lambda: created_at)
    cache.save_shard(shard)
    expected = derive_candidate_corpus(
        (shard,), categories=("synthetic.alpha",)
    )
    configs = (
        CategoryConfig(
            "synthetic.alpha",
            "synthetic:alpha",
            date(2026, 7, 1),
        ),
    )

    assert cache.load_accepted_corpus(
        configs,
        expected_hash=candidate_corpus_hash(expected),
    ) == expected
    assert cache.load_accepted_corpus(
        configs,
        expected_hash="f" * 64,
    ) is None


def test_candidate_cache_rejects_non_integer_schema_and_oversized_payload(tmp_path) -> None:
    from arxiv_digest.candidates import (
        CandidateCache,
        CandidateCacheError,
        CandidateCategoryShard,
    )

    values = dict(
        schema_version=True,
        category="synthetic.alpha",
        set_spec="synthetic:alpha",
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        source_hashes=(),
        documents=(),
        completed_strata=(),
        continuation_by_stratum=(),
        pages_fetched=0,
        exhausted=False,
    )
    with pytest.raises(ValueError, match="schema"):
        CandidateCategoryShard(**values)

    cache = CandidateCache(tmp_path, max_bytes=16)
    path = cache.shard_path("synthetic.alpha", "synthetic:alpha")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 17)
    with pytest.raises(CandidateCacheError, match="size"):
        cache.load_shard("synthetic.alpha", "synthetic:alpha")


def test_derive_corpus_deduplicates_only_after_merging_eligible_categories() -> None:
    from arxiv_digest.candidates import CandidateCategoryShard, derive_candidate_corpus

    created_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)

    def shard(category: str, document, digest: str) -> CandidateCategoryShard:
        return CandidateCategoryShard(
            schema_version=1,
            category=category,
            set_spec=f"synthetic:{category.rsplit('.', 1)[-1]}",
            window_start=date(2026, 5, 25),
            window_end=date(2026, 8, 22),
            created_at=created_at,
            source_hashes=(digest * 64,),
            documents=(document,),
            completed_strata=tuple(range(18)),
            continuation_by_stratum=(),
            pages_fetched=18,
            exhausted=True,
        )

    corpus = derive_candidate_corpus(
        (
            shard("synthetic.alpha", _document(categories=("synthetic.alpha",)), "a"),
            shard("synthetic.beta", _document(categories=("synthetic.beta",)), "b"),
        ),
        categories=("synthetic.alpha", "synthetic.beta"),
    )

    assert len(corpus.documents) == 1
    assert corpus.documents[0].eligible_categories == (
        "synthetic.alpha",
        "synthetic.beta",
    )
    assert corpus.source_hashes == ("a" * 64, "b" * 64)


def test_derived_corpus_applies_category_and_global_caps_deterministically() -> None:
    from arxiv_digest.candidates import CandidateCategoryShard, derive_candidate_corpus

    categories = tuple(f"synthetic.{name}" for name in ("a", "b", "c", "d", "e"))
    created_at = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    serial = 0
    shards = []
    for index, category in enumerate(categories):
        documents = []
        for item in range(501):
            serial += 1
            documents.append(
                _document(
                    f"2608.{serial:05d}",
                    submitted=date(2026, 5, 25) + timedelta(days=item % 90),
                    categories=(category,),
                )
            )
        shards.append(
            CandidateCategoryShard(
                schema_version=1,
                category=category,
                set_spec=f"synthetic:{index}",
                window_start=date(2026, 5, 25),
                window_end=date(2026, 8, 22),
                created_at=created_at,
                source_hashes=(f"{index}" * 64,),
                documents=tuple(documents),
                completed_strata=tuple(range(18)),
                continuation_by_stratum=(),
                pages_fetched=18,
                exhausted=False,
                truncated_strata=(17,),
            )
        )

    first = derive_candidate_corpus(tuple(shards), categories=categories)
    second = derive_candidate_corpus(tuple(shards), categories=categories)

    assert first == second
    assert len(first.documents) == 2_000
    assert all(
        sum(category in document.eligible_categories for document in first.documents)
        <= 500
        for category in categories
    )


def test_search_candidate_papers_matches_title_author_and_arxiv_id() -> None:
    from arxiv_digest.candidates import search_candidate_papers

    first = _document("2608.00001")
    second = _document("2608.00002")
    second = type(second)(
        paper=PaperMetadata(
            arxiv_id=second.paper.arxiv_id,
            title="Copper Aurora Almanac",
            authors=("Juniper Quill",),
            abstract=second.paper.abstract,
            primary_category=second.paper.primary_category,
            categories=second.paper.categories,
        ),
        versions=second.versions,
        eligible_categories=second.eligible_categories,
        evidence_dates=second.evidence_dates,
    )
    corpus = _corpus(first, second)

    assert search_candidate_papers(corpus, "copper") == (second,)
    assert search_candidate_papers(corpus, "JUNIPER---QUILL") == (second,)
    assert search_candidate_papers(corpus, "2608.00001") == (first,)


def test_manual_lookup_uses_get_record_and_maps_unknown_or_deleted_ids() -> None:
    from arxiv_digest.candidates import CandidateLookupError, lookup_candidate_paper

    paper = _document().paper
    article = OaiArticle(
        oai_identifier=f"oai:arXiv.org:{paper.arxiv_id}",
        oai_datestamp=date(2026, 8, 20),
        set_specs=("synthetic:alpha",),
        metadata=paper,
        versions=_document().versions,
    )

    class Source:
        def __init__(self, outcome):
            self.outcome = outcome
            self.calls = []

        def get_record(self, arxiv_id: str):
            self.calls.append(arxiv_id)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    source = Source(article)
    looked_up = lookup_candidate_paper(source, "2608.00001")
    assert looked_up.paper == paper
    assert source.calls == ["2608.00001"]

    deleted = Source(
        OaiTombstone(
            oai_identifier="oai:arXiv.org:2608.00001",
            oai_datestamp=date(2026, 8, 20),
            set_specs=("synthetic:alpha",),
        )
    )
    with pytest.raises(CandidateLookupError, match="deleted") as caught:
        lookup_candidate_paper(deleted, "2608.00001")
    assert caught.value.code == "deleted_paper"

    missing = Source(OaiProtocolError("idDoesNotExist", "No such record"))
    with pytest.raises(CandidateLookupError, match="not found") as caught:
        lookup_candidate_paper(missing, "2608.00009")
    assert caught.value.code == "unknown_paper"


def test_suggestions_are_explainable_diverse_and_rerank_after_acceptance() -> None:
    from arxiv_digest.candidates import CandidateCorpus, build_suggestions

    documents = (
        _rich_document(
            "2608.00100",
            "synthetic.alpha",
            title="Seed Beacon",
            abstract="latent orchard coupling under a copper sky",
            authors=("Seed Author", "Seed Coauthor"),
            day=date(2026, 6, 2),
        ),
        _rich_document(
            "2608.00101",
            "synthetic.alpha",
            title="Lantern Atlas",
            abstract="latent orchard coupling joins amber paths",
            authors=("Seed Coauthor", "Nova Recurring"),
            day=date(2026, 6, 12),
        ),
        _rich_document(
            "2608.00102",
            "synthetic.beta",
            title="Meadow Atlas",
            abstract="latent orchard coupling joins violet paths",
            authors=("Nova Recurring", "Beta Guest"),
            day=date(2026, 7, 2),
        ),
        _rich_document(
            "2608.00103",
            "synthetic.gamma",
            title="Tidal Almanac",
            abstract="latent orchard coupling joins silver paths",
            authors=("Nova Recurring", "Gamma Guest"),
            day=date(2026, 8, 2),
        ),
    )
    corpus = CandidateCorpus(
        schema_version=1,
        categories=("synthetic.alpha", "synthetic.beta", "synthetic.gamma"),
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        source_hashes=("d" * 64,),
        documents=documents,
    )

    suggestions = build_suggestions(corpus, ("2608.00100",), (), ())

    assert suggestions == build_suggestions(corpus, ("2608.00100",), (), ())
    assert all(item.paper.arxiv_id != "2608.00100" for item in suggestions.papers)
    assert any(item.value == "latent" and item.kind == "keyword" for item in suggestions.terms)
    assert any(
        item.value == "latent orchard" and item.kind == "phrase"
        for item in suggestions.terms
    )
    assert {item.name for item in suggestions.authors} >= {
        "Seed Coauthor",
        "Nova Recurring",
    }
    assert all(item.reasons for item in (*suggestions.papers, *suggestions.terms, *suggestions.authors))

    reranked = build_suggestions(
        corpus,
        ("2608.00100",),
        ("latent",),
        ("Nova Recurring",),
    )
    assert "latent" not in {item.value for item in reranked.terms}
    assert "Nova Recurring" not in {item.name for item in reranked.authors}


def test_term_suggestions_exclude_formula_fragments_but_keep_descriptive_topics() -> None:
    from arxiv_digest.candidates import build_suggestions

    documents = tuple(
        _rich_document(
            f"2608.0010{index}",
            "synthetic.alpha",
            title="Homotopy theory and persistent homology",
            abstract=(
                r"For $2$, $1$, $s$, $k$, and $n$, compare $\mathbb{R}$ "
                r"with $\mathbb{F}$ when $n > 1$. Homotopy theory studies "
                "persistent homology and topological groups."
            ),
            authors=(f"Author {index}",),
            day=date(2026, 7, index),
        )
        for index in (1, 2)
    )

    suggestions = build_suggestions(_corpus(*documents), (), (), ())
    values = {item.value for item in suggestions.terms}

    assert values.isdisjoint(
        {"2", "1", "s", "k", "n", "n 1", "mathbb r", "mathbb f"}
    )
    assert {"homotopy", "homotopy theory"} <= values


def test_term_suggestions_exclude_single_word_academic_boilerplate() -> None:
    from arxiv_digest.candidates import build_suggestions

    documents = tuple(
        _rich_document(
            f"2608.0011{index}",
            "synthetic.alpha",
            title="Homotopy over finite complexes",
            abstract=(
                "We also prove, prove, prove, and prove comparison results "
                "over topological spaces."
            ),
            authors=(f"Author {index}",),
            day=date(2026, 7, index),
        )
        for index in (1, 2)
    )

    values = {
        item.value
        for item in build_suggestions(_corpus(*documents), (), (), ()).terms
    }

    assert values.isdisjoint({"also", "prove", "over"})
    assert "homotopy" in values


def test_latex_command_filter_does_not_hide_the_same_word_in_prose() -> None:
    from arxiv_digest.candidates import build_suggestions

    documents = (
        _rich_document(
            "2608.00121",
            "synthetic.alpha",
            title="Sphere bundles",
            abstract="Sphere bundles connect stable homotopy topics.",
            authors=("Author One",),
            day=date(2026, 7, 1),
        ),
        _rich_document(
            "2608.00122",
            "synthetic.alpha",
            title="Sphere spectra",
            abstract="Sphere spectra connect stable homotopy topics.",
            authors=("Author Two",),
            day=date(2026, 7, 2),
        ),
        _rich_document(
            "2608.00123",
            "synthetic.alpha",
            title="A notation convention",
            abstract=r"Write $\sphere$ for the distinguished object.",
            authors=("Author Three",),
            day=date(2026, 7, 3),
        ),
    )

    values = {
        item.value
        for item in build_suggestions(_corpus(*documents), (), (), ()).terms
    }

    assert "sphere" in values


def test_paper_reason_does_not_claim_relation_for_disjoint_seed_text() -> None:
    from arxiv_digest.candidates import build_suggestions

    seed = _rich_document(
        "2608.00110",
        "synthetic.alpha",
        title="Quasar Zircon",
        abstract="Nebula photon lattice",
        authors=("Seed Author",),
        day=date(2026, 7, 1),
    )
    candidate = _rich_document(
        "2608.00111",
        "synthetic.alpha",
        title="Orchard Copper",
        abstract="Meadow lantern tapestry",
        authors=("Other Author",),
        day=date(2026, 7, 2),
    )

    suggestion = build_suggestions(
        _corpus(seed, candidate),
        (seed.paper.arxiv_id,),
        (),
        (),
    ).papers[0]

    assert suggestion.score > 0
    assert "Textually related to selected seed papers" not in suggestion.reasons
    assert "Distinctive title and abstract text" in suggestion.reasons


def test_custom_entries_are_normalized_and_validated_without_accepting_them() -> None:
    from arxiv_digest.candidates import (
        validate_custom_arxiv_id,
        validate_custom_author,
        validate_custom_keyword,
        validate_custom_phrase,
    )

    assert validate_custom_arxiv_id(" 2608.00001 ") == "2608.00001"
    assert validate_custom_keyword("  Aurora  ") == "Aurora"
    assert validate_custom_phrase("  latent   orchard ") == "latent orchard"
    assert validate_custom_author("  Nova   Recurring ") == "Nova Recurring"
    with pytest.raises(ValueError):
        validate_custom_arxiv_id("2608.00001v2")
    with pytest.raises(ValueError):
        validate_custom_keyword("two words")
    with pytest.raises(ValueError):
        validate_custom_phrase("single")
