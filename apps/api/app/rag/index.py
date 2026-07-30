"""Pipeline A — offline indexing.

    PDFs -> parse -> chunk -> {dense vectors in Qdrant, BM25 sparse index on disk}

Run with ``make index``. Nothing in the query path ever writes here, which is what makes
the query path safe to run concurrently: index writes happen only in this script.

Three artefacts are the source of truth and are portable:

* ``var/index/chunks.jsonl`` — every chunk with its full text and metadata.
* ``var/index/bm25.json``    — the tokenised sparse index over exactly those chunks.
* ``var/index/vectors.json`` — their dense vectors, in the same order.

The Qdrant collection is a *derived* cache rebuilt from those whenever it is missing or
stale (:func:`ensure_vector_store`). That indirection matters for deployment: embedded
Qdrant holds an exclusive lock on its directory, so a container that shipped a prebuilt
lock file could not be started twice, and a corpus re-download at deploy time would be a
needless dependency on two government hosts staying up.

The vectors are committed rather than recomputed for two reasons. Embedding is not
batch-invariant, so recomputing them reproduces the evaluated index only to ~4e-4 per
component; and loading an ONNX session to rebuild them was enough to get the image build
OOM-killed on Hugging Face (AUDIT.md §6.5). Rebuilding from the file needs no model and
takes about a second.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.core.embedding import embed_documents
from app.core.models import Chunk
from app.core.settings import Settings, get_settings
from app.core.vectorstore import (
    collection_count,
    get_client,
    recreate_collection,
    upsert_chunks,
)
from app.rag.chunk import chunk_documents
from app.rag.parse import ParseDiagnostics, parse_corpus
from app.rag.sparse import BM25Index

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IndexReport:
    """Everything ``make index`` prints, and everything AUDIT.md quotes."""

    chunks: int
    articles: int
    per_law: dict[str, int]
    parse_diagnostics: dict[str, Any]
    chunk_stats: dict[str, float | int]
    embed_seconds: float
    total_seconds: float
    contents_check_passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunks": self.chunks,
            "articles": self.articles,
            "per_law": self.per_law,
            "parse_diagnostics": self.parse_diagnostics,
            "chunk_stats": self.chunk_stats,
            "embed_seconds": round(self.embed_seconds, 2),
            "total_seconds": round(self.total_seconds, 2),
            "contents_check_passed": self.contents_check_passed,
        }


def write_chunks(chunks: list[Chunk], settings: Settings) -> None:
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    with settings.chunks_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk.model_dump(), ensure_ascii=False) + "\n")


def load_chunks(settings: Settings | None = None) -> list[Chunk]:
    """Read the canonical chunk store."""
    cfg = settings or get_settings()
    if not cfg.chunks_path.exists():
        raise FileNotFoundError(
            f"{cfg.chunks_path} not found. Run `make index` to build the index."
        )
    with cfg.chunks_path.open(encoding="utf-8") as handle:
        return [Chunk.model_validate_json(line) for line in handle if line.strip()]


def write_vectors(vectors: list[list[float]], settings: Settings | None = None) -> None:
    """Persist the dense vectors beside the chunks they belong to, in the same order."""
    cfg = settings or get_settings()
    array = np.asarray(vectors, dtype=np.float32)
    expected = (len(vectors), cfg.embedding_dim)
    if array.shape != expected:
        raise ValueError(f"expected {expected} vectors, got {array.shape}")
    # ``tolist`` widens each float32 to the float64 that represents it exactly, and JSON
    # round-trips float64 through repr without loss, so reloading as float32 returns the
    # identical bits. Written compactly: the default separators add ~250 KB of spaces.
    with cfg.vectors_path.open("w", encoding="utf-8") as handle:
        json.dump(array.tolist(), handle, separators=(",", ":"))


def load_vectors(count: int, settings: Settings | None = None) -> list[list[float]] | None:
    """Read the persisted vectors, or ``None`` if absent or not matching the chunks.

    Returning ``None`` rather than raising keeps re-embedding available as a fallback for
    a checkout that has chunks but no vectors. A mismatch is never silently tolerated:
    stale vectors paired with fresh chunks would misattribute every citation.
    """
    cfg = settings or get_settings()
    if not cfg.vectors_path.exists():
        return None
    with cfg.vectors_path.open(encoding="utf-8") as handle:
        array = np.asarray(json.load(handle), dtype=np.float32)
    if array.shape != (count, cfg.embedding_dim):
        logger.warning(
            "ignoring %s: shape %s does not match %d chunks of dim %d; re-embedding",
            cfg.vectors_path,
            array.shape,
            count,
            cfg.embedding_dim,
        )
        return None
    return [row.tolist() for row in array]


def build_index(settings: Settings | None = None) -> IndexReport:
    """Parse the corpus, chunk it, and write both indexes."""
    cfg = settings or get_settings()
    started = time.perf_counter()

    diagnostics = ParseDiagnostics()
    documents = parse_corpus(cfg, diagnostics)
    chunks, chunk_stats = chunk_documents(documents, cfg)

    per_law: dict[str, int] = {}
    for chunk in chunks:
        per_law[chunk.law_id] = per_law.get(chunk.law_id, 0) + 1

    write_chunks(chunks, cfg)
    BM25Index.build(chunks).save(cfg.bm25_path)

    embed_started = time.perf_counter()
    vectors = embed_documents([chunk.text for chunk in chunks], cfg)
    embed_seconds = time.perf_counter() - embed_started
    write_vectors(vectors, cfg)

    recreate_collection(cfg)
    written = upsert_chunks(chunks, vectors, cfg)
    if written != len(chunks):  # pragma: no cover - defensive
        raise RuntimeError(f"upserted {written} points for {len(chunks)} chunks")

    report = IndexReport(
        chunks=len(chunks),
        articles=sum(len(document.articles) for document in documents),
        per_law=per_law,
        parse_diagnostics=diagnostics.as_dict(),
        chunk_stats=chunk_stats.as_dict(),
        embed_seconds=embed_seconds,
        total_seconds=time.perf_counter() - started,
        contents_check_passed=diagnostics.contents_check_passed,
    )
    cfg.index_meta_path.write_text(
        json.dumps(
            {
                "embedding_model": cfg.embedding_model,
                "embedding_dim": cfg.embedding_dim,
                "chunk_target_tokens": cfg.chunk_target_tokens,
                "chunk_overlap_tokens": cfg.chunk_overlap_tokens,
                "effective_chunk_max_tokens": cfg.effective_chunk_max_tokens,
                "collection": cfg.qdrant_collection,
                "report": report.as_dict(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return report


def ensure_vector_store(settings: Settings | None = None) -> int:
    """Materialise the Qdrant collection from ``chunks.jsonl`` when it is missing.

    Returns the number of points in the collection. Called at API startup so a fresh
    container — or a fresh checkout that has the committed index artefacts but no
    ``var/qdrant`` — serves correct results without anyone remembering to reindex.
    """
    cfg = settings or get_settings()
    chunks = load_chunks(cfg)
    existing = collection_count(cfg)
    if existing == len(chunks):
        logger.info("vector store ready: %d points", existing)
        return existing

    logger.info(
        "rebuilding vector store: collection has %d points, chunk store has %d",
        existing,
        len(chunks),
    )
    # Prefer the persisted vectors. Beyond saving the work, this is what keeps a
    # deployed container serving the same numbers the evaluation scored: re-embedding
    # reproduces them only to ~4e-4 per component, because padding length — and so the
    # order of the reductions — depends on how the batch happened to be composed.
    #
    # It is also what lets the image be built at all. Loading the ONNX session here, on
    # top of the batch's activations, was enough to get the Hugging Face build container
    # OOM-killed (AUDIT.md §6.5).
    vectors = load_vectors(len(chunks), cfg)
    if vectors is None:
        logger.info("no usable %s; embedding %d chunks", cfg.vectors_path, len(chunks))
        vectors = embed_documents([chunk.text for chunk in chunks], cfg)

    recreate_collection(cfg)
    return upsert_chunks(chunks, vectors, cfg)


def _print_report(report: IndexReport, settings: Settings) -> None:
    print("\nLexora index built\n")
    print(f"  articles           {report.articles}")
    print(f"  chunks             {report.chunks}")
    for law_id, count in sorted(report.per_law.items()):
        print(f"      {law_id:28} {count:4} chunks")
    stats = report.chunk_stats
    print(
        f"  tokens/chunk       mean {stats['tokens_mean']}  max {stats['tokens_max']}  "
        f"(encoder window {settings.embedding_max_tokens})"
    )
    print(f"  articles split     {stats['articles_split']}")
    print(f"  over window        {stats['over_window']}  (must be 0)")
    print("\n  parse diagnostics")
    for key, value in report.parse_diagnostics.items():
        print(f"      {key:28} {value}")
    verdict = "PASS" if report.contents_check_passed else "FAIL"
    print(f"\n  contents cross-check  {verdict}")
    print(f"  embed {report.embed_seconds:.1f}s   total {report.total_seconds:.1f}s\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Lexora index from the corpus.")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="report on the existing index without rebuilding it",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()

    try:
        if args.verify_only:
            chunks = load_chunks(settings)
            points = collection_count(settings)
            print(f"chunks.jsonl {len(chunks)}   qdrant points {points}")
            return 0 if points == len(chunks) else 1
        report = build_index(settings)
        _print_report(report, settings)
        return 0 if report.contents_check_passed and report.chunk_stats["over_window"] == 0 else 1
    finally:
        get_client(settings).close()


if __name__ == "__main__":
    raise SystemExit(main())
