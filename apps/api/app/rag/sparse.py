"""BM25 sparse index over exactly the chunks in the dense index.

Why a keyword index at all, next to a perfectly good vector index: statutes are full of
tokens whose *identity* matters and whose *meaning* is nil. "Article 51" and "Article 15"
sit in almost the same place in embedding space; to BM25 they are different terms. The
reverse is equally true — "end-of-service money" retrieves Article 51 by meaning while
sharing no term with it. Hybrid retrieval exists because neither failure mode is rare.

Deliberately **not** stemmed. A stemmer folds "leave"/"leaves" together, which is fine,
but it also mangles the defined terms a statute turns on. Exact-term recall is the entire
reason this index exists, so tokens are kept as written.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self

from rank_bm25 import BM25Okapi

from app.core.models import Chunk

logger = logging.getLogger(__name__)

# Keeps alphanumerics together so "51", "2007" and "sub-lease" survive as usable terms.
_TOKEN_RE: Final = re.compile(r"[a-z0-9]+")

# Extremely common English function words. A short, conservative list: dropping too much
# would remove statutory phrasing that carries meaning ("not", "before", "within").
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "which",
        "will",
        "with",
    }
)


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop only very common function words."""
    return [token for token in _TOKEN_RE.findall(text.lower()) if token not in _STOPWORDS]


@dataclass(slots=True)
class BM25Index:
    """A persisted BM25 index keyed by chunk id."""

    chunk_ids: list[str]
    corpus: list[list[str]]
    _bm25: BM25Okapi | None = None

    @classmethod
    def build(cls, chunks: list[Chunk]) -> Self:
        if not chunks:
            raise ValueError("cannot build a sparse index over zero chunks")
        return cls(
            chunk_ids=[chunk.chunk_id for chunk in chunks],
            corpus=[tokenize(chunk.text) for chunk in chunks],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 1,
            "chunk_ids": self.chunk_ids,
            "corpus": self.corpus,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logger.info("wrote sparse index: %d documents -> %s", len(self.chunk_ids), path)

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.exists():
            raise FileNotFoundError(f"{path} not found. Run `make index` to build the index.")
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            chunk_ids=list(payload["chunk_ids"]), corpus=[list(d) for d in payload["corpus"]]
        )

    @property
    def bm25(self) -> BM25Okapi:
        """Scorer, constructed lazily.

        Only the tokenised corpus is persisted, not a pickled scorer: unpickling is
        arbitrary code execution, and an index file is exactly the kind of artefact that
        travels between machines. Rebuilding over a few hundred documents is immediate.
        """
        if self._bm25 is None:
            self._bm25 = BM25Okapi(self.corpus)
        return self._bm25

    def search(self, query: str, limit: int) -> list[tuple[str, float]]:
        """Top ``limit`` ``(chunk_id, score)`` pairs, best first.

        Chunks scoring zero share no term with the query and are dropped rather than
        padded in: a zero-score document at rank 8 still earns Reciprocal Rank Fusion
        weight, which would let an unrelated passage ride into the reranker.
        """
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        ranked = sorted(
            ((self.chunk_ids[i], float(score)) for i, score in enumerate(scores) if score > 0.0),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return ranked[:limit]

    def __len__(self) -> int:
        return len(self.chunk_ids)
