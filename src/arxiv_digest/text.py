"""Deterministic local text normalization and sparse TF-IDF helpers."""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable
from math import log, sqrt
from typing import Any


# Grammatical function words and generic academic boilerplate only.  Keeping
# this list deliberately domain-neutral prevents it from encoding research
# preferences into setup suggestions.
GENERIC_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "here",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "of",
        "on",
        "or",
        "our",
        "paper",
        "present",
        "propose",
        "results",
        "show",
        "shows",
        "study",
        "than",
        "that",
        "the",
        "their",
        "these",
        "this",
        "those",
        "to",
        "using",
        "was",
        "we",
        "were",
        "which",
        "with",
        "would",
    }
)


def normalize_text(value: str) -> str:
    """Return case-folded NFKC text with punctuation collapsed to spaces."""

    if not isinstance(value, str):
        raise TypeError("text must be a string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    words = "".join(
        character
        if character.isalnum() or unicodedata.category(character).startswith("M")
        else " "
        for character in normalized
    )
    return " ".join(words.split())


def tokenize(value: str) -> tuple[str, ...]:
    """Tokenize normalized text, omitting only generic stop words."""

    return tuple(
        token
        for token in normalize_text(value).split()
        if token not in GENERIC_STOP_WORDS
    )


def extract_ngrams(
    value: str | tuple[str, ...] | list[str],
    *,
    ngram_range: tuple[int, int] = (1, 3),
) -> tuple[str, ...]:
    """Extract space-joined n-grams in stable length-then-position order."""

    if (
        not isinstance(ngram_range, tuple)
        or len(ngram_range) != 2
        or any(type(bound) is not int for bound in ngram_range)
        or ngram_range[0] < 1
        or ngram_range[0] > ngram_range[1]
    ):
        raise ValueError("ngram_range must be a positive (minimum, maximum) tuple")
    tokens = tokenize(value) if isinstance(value, str) else tuple(value)
    return tuple(
        " ".join(tokens[index : index + size])
        for size in range(ngram_range[0], ngram_range[1] + 1)
        for index in range(0, len(tokens) - size + 1)
    )


def build_tfidf_vectors(
    documents: Iterable[object],
    *,
    ngram_range: tuple[int, int] = (1, 3),
) -> dict[str, dict[str, float]]:
    """Build deterministic, L2-normalized sparse vectors keyed by arXiv ID.

    ``documents`` may be candidate documents (with a ``paper`` attribute) or
    paper-like records directly.  Title and abstract are treated as separate
    fields so no artificial n-gram spans their boundary.
    """

    rows: list[tuple[str, Counter[str]]] = []
    seen_ids: set[str] = set()
    for document in documents:
        paper: Any = getattr(document, "paper", document)
        arxiv_id = getattr(paper, "arxiv_id", None)
        title = getattr(paper, "title", None)
        abstract = getattr(paper, "abstract", None)
        if not all(isinstance(value, str) for value in (arxiv_id, title, abstract)):
            raise TypeError("documents must expose string arxiv_id, title, and abstract")
        if arxiv_id in seen_ids:
            raise ValueError(f"duplicate document ID: {arxiv_id}")
        seen_ids.add(arxiv_id)
        terms = Counter(
            extract_ngrams(title, ngram_range=ngram_range)
            + extract_ngrams(abstract, ngram_range=ngram_range)
        )
        rows.append((arxiv_id, terms))

    document_frequency: Counter[str] = Counter()
    for _, counts in rows:
        document_frequency.update(counts.keys())
    document_count = len(rows)

    result: dict[str, dict[str, float]] = {}
    for arxiv_id, counts in rows:
        unnormalized = {
            term: (1.0 + log(count))
            * (log((1.0 + document_count) / (1.0 + document_frequency[term])) + 1.0)
            for term, count in counts.items()
        }
        norm = sqrt(sum(weight * weight for weight in unnormalized.values()))
        result[arxiv_id] = {
            term: (weight / norm if norm else 0.0)
            for term, weight in sorted(unnormalized.items())
        }
    return result
