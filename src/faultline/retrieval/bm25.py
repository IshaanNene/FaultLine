"""Okapi BM25 over the corpus.

BM25 is not the fallback here, it is the reason retrieval is hybrid at all.
Alerts are full of exact identifiers -- `OOMKilled`, `ECONNREFUSED`,
`http_error_rate`, an image tag like `2.14.0` -- and a dense embedding blurs
precisely the tokens that make a runbook the right one. Lexical search is what
finds "the OOMKilled runbook"; dense search is what finds "the runbook about pods
dying from memory pressure" when nobody wrote the word OOMKilled.

The tokenizer therefore does more work than the ranker. An identifier is emitted
whole *and* split, so `http_error_rate` matches a query for `error rate` and a
query for the metric name, and `OOMKilled` matches `oom killed`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# BM25 parameters. k1 controls term-frequency saturation, b controls length
# normalization; these are the standard defaults and there is no corpus here
# large enough to justify tuning them.
K1 = 1.5
B = 0.75

_WORD = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*")
_SPLIT_IDENTIFIER = re.compile(r"[._-]")
# Two boundaries, not one. The obvious rule (lowercase followed by uppercase)
# handles httpError but misses OOMKilled, where the split sits between two capitals
# -- which is the single token this domain cares most about.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Words that carry no signal in a corpus that is entirely about incidents. Kept
# deliberately short: over-pruning hurts more than it helps at this scale.
_STOPWORD_TEXT = (
    "a an and are as at be been but by can do does for from had has have if in into is it "
    "its may not of on or that the their then there these they this to was were what when "
    "which who will with you your"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def tokenize(text: str) -> list[str]:
    """Domain-aware tokenization.

    Identifiers are emitted whole and in pieces, so a query can match either the
    exact symbol or the words inside it.
    """
    tokens: list[str] = []
    for match in _WORD.finditer(text):
        raw = match.group(0)
        lowered = raw.lower()
        if lowered not in STOPWORDS:
            tokens.append(lowered)

        # OOMKilled -> oom, killed
        camel_parts = [p.lower() for p in _CAMEL.split(raw) if p]
        # http_error_rate -> http, error, rate
        pieces = [
            piece
            for part in camel_parts
            for piece in _SPLIT_IDENTIFIER.split(part)
            if piece and piece != lowered
        ]
        tokens.extend(p for p in pieces if p not in STOPWORDS)
    return tokens


@dataclass(slots=True)
class Posting:
    doc_id: str
    frequencies: Counter[str]
    length: int


class BM25Index:
    """An in-memory BM25 index.

    In production this is `pg_search`'s BM25 index inside the same Postgres as
    everything else (see ADR 2). The scoring is the same; this keeps the whole
    retrieval stack runnable and testable without a database, the same way the
    in-memory bus does for the queue.
    """

    def __init__(self, k1: float = K1, b: float = B) -> None:
        self._k1 = k1
        self._b = b
        self._postings: dict[str, Posting] = {}
        self._document_frequency: Counter[str] = Counter()
        self._total_length = 0

    def add(self, doc_id: str, text: str) -> None:
        tokens = tokenize(text)
        frequencies = Counter(tokens)
        if doc_id in self._postings:
            self._remove(doc_id)
        self._postings[doc_id] = Posting(doc_id, frequencies, len(tokens))
        self._total_length += len(tokens)
        for term in frequencies:
            self._document_frequency[term] += 1

    def _remove(self, doc_id: str) -> None:
        posting = self._postings.pop(doc_id)
        self._total_length -= posting.length
        for term in posting.frequencies:
            self._document_frequency[term] -= 1
            if self._document_frequency[term] <= 0:
                del self._document_frequency[term]

    def __len__(self) -> int:
        return len(self._postings)

    @property
    def average_length(self) -> float:
        return self._total_length / len(self._postings) if self._postings else 0.0

    def _idf(self, term: str) -> float:
        n = len(self._postings)
        df = self._document_frequency.get(term, 0)
        # The +1 inside the log keeps IDF non-negative for terms in most
        # documents, which otherwise lets a common word drag a score below zero.
        return math.log(((n - df + 0.5) / (df + 0.5)) + 1)

    def search(self, query: str, limit: int = 50) -> list[tuple[str, float]]:
        if not self._postings:
            return []
        query_terms = tokenize(query)
        if not query_terms:
            return []

        avgdl = self.average_length or 1.0
        scored: list[tuple[str, float]] = []
        for posting in self._postings.values():
            score = 0.0
            for term in set(query_terms):
                frequency = posting.frequencies.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + self._k1 * (
                    1 - self._b + self._b * posting.length / avgdl
                )
                score += self._idf(term) * (frequency * (self._k1 + 1)) / denominator
            if score > 0:
                scored.append((posting.doc_id, score))

        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:limit]
