from __future__ import annotations

from dataclasses import dataclass
from math import log

import pytest


@dataclass(frozen=True)
class _Paper:
    arxiv_id: str
    title: str
    abstract: str


@dataclass(frozen=True)
class _Document:
    paper: _Paper


def test_normalize_text_collapses_unicode_punctuation_and_whitespace() -> None:
    from arxiv_digest.text import normalize_text

    assert normalize_text("  Graph\u2014NEURAL\nnetworks!  ") == normalize_text(
        "graph neural networks"
    )


def test_tokenize_and_extract_one_to_three_grams_deterministically() -> None:
    from arxiv_digest.text import extract_ngrams, tokenize

    assert tokenize("The Graph, and NEURAL network") == (
        "graph",
        "neural",
        "network",
    )
    assert extract_ngrams("The graph neural network", ngram_range=(1, 3)) == (
        "graph",
        "neural",
        "network",
        "graph neural",
        "neural network",
        "graph neural network",
    )


def test_tfidf_uses_title_and_abstract_and_penalizes_common_terms() -> None:
    from arxiv_digest.text import build_tfidf_vectors

    documents = (
        _Document(_Paper("1", "Phase garden", "Aurora background")),
        _Document(_Paper("2", "Phase orbit", "Meadow background")),
        _Document(_Paper("3", "Quantum garden", "Orchard background")),
    )

    first = build_tfidf_vectors(documents, ngram_range=(1, 3))
    second = build_tfidf_vectors(documents, ngram_range=(1, 3))

    assert "phase garden" in first["1"]
    assert "aurora" in first["1"]
    assert first["1"]["phase garden"] > first["1"]["phase"]
    assert first["1"]["aurora"] > first["1"]["background"]
    assert list(first.items()) == list(second.items())


def test_document_frequency_counts_papers_not_repetitions_within_one_paper() -> None:
    from arxiv_digest.text import build_tfidf_vectors

    documents = (
        _Document(_Paper("1", "echo echo echo echo", "solo")),
        _Document(_Paper("2", "other", "solo")),
    )

    vector = build_tfidf_vectors(documents, ngram_range=(1, 1))["1"]

    expected_ratio = (1.0 + log(4)) * (log(3 / 2) + 1.0)
    assert vector["echo"] / vector["solo"] == pytest.approx(expected_ratio)
