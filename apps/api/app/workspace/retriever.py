"""Hybrid retrieval over one session's uploaded documents.

Same contract as :class:`~app.rag.retrieve.HybridRetriever` — same ``retrieve`` signature,
same :class:`RetrievalResult` — so the pipeline, reranker, refusal gate and citation
verifier are the corpus's code, unmodified. Only the storage differs.

**Dense search is a numpy matmul, not Qdrant.** Two reasons, and the second is the real
one. A session holds tens to a few hundred chunks, where a brute-force cosine over a
contiguous float32 matrix is faster than a network round trip to an index. More
importantly, embedded Qdrant takes an exclusive lock on its directory: per-session
collections would serialise every user behind one writer, and writing uploads into the
*shared* collection would mix a stranger's contract into the law corpus that the
evaluation measured. Neither is acceptable, and neither arises if uploads never reach
Qdrant at all.

The vectors are unit-normalised at write time, so cosine similarity is a dot product.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from app.core.models import Chunk, RetrievalSource, ScoredChunk
from app.core.settings import Settings, get_settings
from app.rag.retrieve import RetrievalResult, reciprocal_rank_fusion
from app.rag.sparse import BM25Index

logger = logging.getLogger(__name__)


def _normalise_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A zero vector cannot be normalised; leaving the norm at 1 maps it to a similarity of
    # 0 against everything, which is the correct meaning of "carries no signal".
    norms[norms == 0] = 1.0
    normalised: np.ndarray = (matrix / norms).astype(np.float32)
    return normalised


@dataclass(slots=True)
class WorkspaceRetriever:
    """Built per query from a session's chunks. Cheap: the vectors already exist."""

    settings: Settings
    chunks: list[Chunk]
    vectors: np.ndarray
    sparse: BM25Index
    chunks_by_id: dict[str, Chunk] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        chunks: list[Chunk],
        vectors: np.ndarray,
        settings: Settings | None = None,
    ) -> WorkspaceRetriever:
        cfg = settings or get_settings()
        if len(chunks) != vectors.shape[0]:
            raise ValueError(f"{len(chunks)} chunks but {vectors.shape[0]} vectors")
        return cls(
            settings=cfg,
            chunks=chunks,
            vectors=_normalise_rows(vectors),
            sparse=BM25Index.build(chunks),
            chunks_by_id={chunk.chunk_id: chunk for chunk in chunks},
        )

    def retrieve(self, query: str, law_id: str | None = None) -> RetrievalResult:
        """``law_id`` filters to a single uploaded document, mirroring the corpus filter."""
        from app.core.embedding import embed_query

        cfg = self.settings
        if not self.chunks:
            return RetrievalResult(candidates=(), dense_hits=0, sparse_hits=0, overlap=0)

        query_vector = np.asarray(embed_query(query, cfg), dtype=np.float32)
        query_vector /= np.linalg.norm(query_vector) or 1.0
        similarities = self.vectors @ query_vector

        # Filter before ranking, so a document filter cannot bias one retriever against
        # the other — the same trap the corpus retriever documents for its law filter.
        allowed = [
            index
            for index, chunk in enumerate(self.chunks)
            if law_id is None or chunk.law_id == law_id
        ]
        if not allowed:
            return RetrievalResult(candidates=(), dense_hits=0, sparse_hits=0, overlap=0)

        # Descending similarity, chunk_id breaking ties, so identical inputs rank
        # identically on every run.
        ordered_dense = sorted(
            allowed, key=lambda i: (-float(similarities[i]), self.chunks[i].chunk_id)
        )[: cfg.dense_top_k]
        dense_ids = [self.chunks[i].chunk_id for i in ordered_dense]
        dense_score = {self.chunks[i].chunk_id: float(similarities[i]) for i in ordered_dense}

        sparse: list[tuple[str, float]] = []
        for chunk_id, score in self.sparse.search(query, cfg.sparse_top_k):
            chunk = self.chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            if law_id and chunk.law_id != law_id:
                continue
            sparse.append((chunk_id, score))
        sparse_ids = [chunk_id for chunk_id, _ in sparse]

        fused = reciprocal_rank_fusion({"dense": dense_ids, "sparse": sparse_ids}, k=cfg.rrf_k)
        dense_rank = {chunk_id: i + 1 for i, chunk_id in enumerate(dense_ids)}
        sparse_rank = {chunk_id: i + 1 for i, chunk_id in enumerate(sparse_ids)}
        sparse_score = dict(sparse)

        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))[: cfg.fused_top_k]
        candidates: list[ScoredChunk] = []
        for chunk_id, rrf_score in ordered:
            chunk = self.chunks_by_id.get(chunk_id)
            if chunk is None:
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
                )
            )

        overlap = len(set(dense_ids) & set(sparse_ids))
        best_dense = max(dense_score.values(), default=None)
        return RetrievalResult(
            candidates=tuple(candidates),
            dense_hits=len(dense_ids),
            sparse_hits=len(sparse_ids),
            overlap=overlap,
            best_dense=best_dense,
        )
