"""Hybrid retrieval: dense + sparse, fused with Reciprocal Rank Fusion.

    query ──┬─ Qdrant dense  top-20 ─┐
            └─ BM25   sparse top-20 ─┴─ RRF (k=60) ─> top-20 candidates

Why fuse ranks rather than scores
---------------------------------
Cosine similarity lives in [-1, 1] and BM25 is an unbounded corpus-dependent sum. Adding
them, or min-max normalising them per query, makes the weighting depend on how spread
that particular query's scores happened to be — a hidden, query-varying hyperparameter.
Reciprocal Rank Fusion throws the magnitudes away and keeps only the ordering:

    RRF(d) = Σ_retrievers 1 / (k + rank_r(d))          rank is 1-based

`k` (60, from Cormack et al. 2009) damps the top of each list so a single retriever
cannot dominate on its own confidence: rank 1 contributes 1/61 and rank 2 contributes
1/62, a 1.6% difference, while a document *both* retrievers rank first scores roughly
twice either alone. Agreement is what the fusion rewards, which is exactly the signal
wanted when one retriever understands paraphrase and the other understands exact terms.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final

from app.core.models import Chunk, RetrievalSource, ScoredChunk
from app.core.settings import Settings, get_settings
from app.rag.sparse import BM25Index

logger = logging.getLogger(__name__)

MIN_RRF_K: Final = 1


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Fused candidates plus the per-retriever detail the Evidence Panel shows."""

    candidates: tuple[ScoredChunk, ...]
    dense_hits: int
    sparse_hits: int
    overlap: int
    #: Best raw dense cosine over the candidate set. Used by the refusal gate as a
    #: domain signal that is independent of the cross-encoder's topical judgement.
    best_dense: float | None = None

    def __len__(self) -> int:
        return len(self.candidates)


def reciprocal_rank_fusion(
    ranked_lists: dict[str, list[str]],
    k: int = 60,
) -> dict[str, float]:
    """Fuse ranked id lists into one score per id.

    Args:
        ranked_lists: retriever name -> ids in descending relevance. Ties must already
            be broken by the caller; RRF has no notion of a tie.
        k: rank damping constant. Must be >= 1, else rank 1 divides by zero when k=-1
            and the ordering inverts for small k.

    Returns:
        id -> fused score. Higher is better.
    """
    if k < MIN_RRF_K:
        raise ValueError(f"RRF k must be >= {MIN_RRF_K}, got {k}")
    fused: dict[str, float] = {}
    for ids in ranked_lists.values():
        for zero_based, doc_id in enumerate(ids):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + zero_based + 1)
    return fused


@dataclass(slots=True)
class HybridRetriever:
    """Holds the two indexes and fuses them. Read-only, therefore concurrency-safe."""

    settings: Settings
    sparse: BM25Index
    chunks_by_id: dict[str, Chunk] = field(default_factory=dict)

    @classmethod
    def load(cls, settings: Settings | None = None) -> HybridRetriever:
        from app.rag.index import load_chunks

        cfg = settings or get_settings()
        chunks = load_chunks(cfg)
        return cls(
            settings=cfg,
            sparse=BM25Index.load(cfg.bm25_path),
            chunks_by_id={chunk.chunk_id: chunk for chunk in chunks},
        )

    def retrieve(self, query: str, law_id: str | None = None) -> RetrievalResult:
        """Run both retrievers and fuse them."""
        from app.core.embedding import embed_query
        from app.core.vectorstore import search as dense_search

        cfg = self.settings
        dense = dense_search(embed_query(query, cfg), cfg.dense_top_k, cfg, law_id=law_id)
        sparse_raw = self.sparse.search(query, cfg.sparse_top_k)

        # A law filter must apply to both retrievers or the fusion is filtering-biased:
        # the dense list would be restricted while the sparse list smuggled other laws in.
        sparse: list[tuple[str, float]] = []
        for chunk_id, score in sparse_raw:
            chunk = self.chunks_by_id.get(chunk_id)
            if chunk is None:
                logger.warning("sparse index references unknown chunk %s", chunk_id)
                continue
            if law_id and chunk.law_id != law_id:
                continue
            sparse.append((chunk_id, score))

        dense_ids = [chunk.chunk_id for chunk, _ in dense]
        sparse_ids = [chunk_id for chunk_id, _ in sparse]
        fused = reciprocal_rank_fusion({"dense": dense_ids, "sparse": sparse_ids}, k=cfg.rrf_k)

        dense_rank = {chunk_id: i + 1 for i, chunk_id in enumerate(dense_ids)}
        dense_score = {chunk.chunk_id: score for chunk, score in dense}
        sparse_rank = {chunk_id: i + 1 for i, chunk_id in enumerate(sparse_ids)}
        sparse_score = dict(sparse)

        # Deterministic ordering: chunk_id breaks score ties so identical inputs always
        # produce an identical candidate list, which the eval harness depends on.
        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))[: cfg.fused_top_k]

        candidates: list[ScoredChunk] = []
        for position, (chunk_id, rrf_score) in enumerate(ordered):
            chunk = self.chunks_by_id.get(chunk_id)
            if chunk is None:  # pragma: no cover - guarded above for the sparse path
                continue
            in_dense = chunk_id in dense_rank
            in_sparse = chunk_id in sparse_rank
            candidates.append(
                ScoredChunk(
                    chunk=chunk,
                    source=(
                        RetrievalSource.BOTH
                        if in_dense and in_sparse
                        else RetrievalSource.DENSE
                        if in_dense
                        else RetrievalSource.SPARSE
                    ),
                    dense_rank=dense_rank.get(chunk_id),
                    dense_score=dense_score.get(chunk_id),
                    sparse_rank=sparse_rank.get(chunk_id),
                    sparse_score=sparse_score.get(chunk_id),
                    rrf_score=rrf_score,
                    fused_rank=position + 1,
                )
            )

        return RetrievalResult(
            candidates=tuple(candidates),
            dense_hits=len(dense_ids),
            sparse_hits=len(sparse_ids),
            overlap=len(set(dense_ids) & set(sparse_ids)),
            best_dense=max((score for _, score in dense), default=None),
        )
