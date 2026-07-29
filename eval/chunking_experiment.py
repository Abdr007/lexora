#!/usr/bin/env python3
"""Re-index the corpus at several chunk sizes and measure what changes.

    python eval/chunking_experiment.py

The interesting result is not "which number is biggest". It is that the encoder's context
window is a hard constraint the chunk size has to respect: ``bge-small-en-v1.5`` truncates
at 512 tokens, so a 600- or 1000-token chunk is *indexed* in full but *embedded* only up
to the cut. Its tail is invisible to dense retrieval while still being shown to the user
as retrieved evidence.

Each configuration is therefore run twice — capped at the window and uncapped — so the
cost of ignoring the constraint is a measured number rather than an assertion. Writes
``eval/results/chunking.json``, which the /metrics page renders.

The experiment builds its indexes in a scratch collection and restores the production
index at the end, so running it never leaves the service pointing at 300-token chunks.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "apps" / "api") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app.core.embedding import embed_documents  # noqa: E402
from app.core.models import Chunk  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.core.vectorstore import (  # noqa: E402
    close_client,
    recreate_collection,
    upsert_chunks,
)
from app.rag.chunk import chunk_documents  # noqa: E402
from app.rag.index import build_index  # noqa: E402
from app.rag.parse import ParseDiagnostics, parse_corpus  # noqa: E402
from app.rag.rerank import rerank  # noqa: E402
from app.rag.retrieve import HybridRetriever  # noqa: E402
from app.rag.sparse import BM25Index  # noqa: E402
from eval.ragas_run import load_labels  # noqa: E402

logger = logging.getLogger(__name__)

TARGETS: Final = (300, 600, 1000)
RESULTS: Final = REPO_ROOT / "eval" / "results" / "chunking.json"


def _part_of(part_id: str) -> str:
    return "cabinet-resolution" if "cabinet" in part_id else "decree-law"


def evaluate(chunks: list[Chunk], top_k: int) -> dict[str, float]:
    """Index these chunks and measure hit-rate against the labelled set."""
    settings = get_settings()
    recreate_collection(settings)
    upsert_chunks(chunks, embed_documents([c.text for c in chunks], settings), settings)

    retriever = HybridRetriever(
        settings=settings,
        sparse=BM25Index.build(chunks),
        chunks_by_id={chunk.chunk_id: chunk for chunk in chunks},
    )

    labels = [label for label in load_labels() if label.answerable]
    hit1 = hit5 = 0
    for label in labels:
        candidates = list(retriever.retrieve(label.question).candidates)
        ranked = rerank(label.question, candidates, settings)
        for rank, item in enumerate(ranked[:top_k], start=1):
            if label.matches(
                item.chunk.law_id, item.chunk.article_no, _part_of(item.chunk.part_id)
            ):
                hit5 += 1
                if rank == 1:
                    hit1 += 1
                break
    total = len(labels) or 1
    return {"hit_rate_at_1": hit1 / total, "hit_rate_at_5": hit5 / total}


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    documents = parse_corpus(settings, ParseDiagnostics())
    rows: list[dict[str, Any]] = []

    try:
        for target in TARGETS:
            for capped in (True, False):
                chunks, stats = chunk_documents(
                    documents,
                    settings,
                    target_tokens=target,
                    enforce_encoder_window=capped,
                )
                label = f"{target}{'' if capped else ' (uncapped)'}"
                print(f"  indexing {label}: {stats.chunks} chunks, max {stats.tokens_max} tokens…")
                scores = evaluate(chunks, settings.rerank_top_k)
                rows.append(
                    {
                        "target_tokens": target,
                        "encoder_window_enforced": capped,
                        "chunks": stats.chunks,
                        "tokens_mean": stats.tokens_mean,
                        "tokens_max": stats.tokens_max,
                        "articles_split": stats.articles_split,
                        "over_window": stats.over_window,
                        "hit_rate_at_1": round(scores["hit_rate_at_1"], 4),
                        "hit_rate_at_5": round(scores["hit_rate_at_5"], 4),
                    }
                )
                print(
                    f"      hit@1 {scores['hit_rate_at_1']:.3f}   "
                    f"hit@5 {scores['hit_rate_at_5']:.3f}   "
                    f"over-window {stats.over_window}"
                )
    finally:
        # Always restore the production index — a half-finished experiment must never
        # leave the service serving 1000-token chunks.
        print("\n  restoring the production index…")
        build_index(settings)
        close_client()

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")

    print("\n" + "=" * 78)
    print(
        f"  {'target':>8} {'capped':>7} {'chunks':>7} {'mean':>6} "
        f"{'max':>5} {'over':>5} {'hit@1':>7} {'hit@5':>7}"
    )
    for row in rows:
        print(
            f"  {row['target_tokens']:>8} {row['encoder_window_enforced']!s:>7} "
            f"{row['chunks']:>7} {row['tokens_mean']:>6.0f} {row['tokens_max']:>5} "
            f"{row['over_window']:>5} {row['hit_rate_at_1']:>7.3f} {row['hit_rate_at_5']:>7.3f}"
        )
    print(f"\n  wrote {RESULTS.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
