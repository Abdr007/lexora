#!/usr/bin/env python3
"""Measure retrieval over an uploaded document, not over the pinned corpus.

§5 measures the law library. Nothing measured the workspace, and the interface said so
honestly — `calibrated: false` everywhere it is exposed. That is the right disclosure and
it is not a measurement, so this closes the gap: the same document a user would upload,
run through the same ingest, with hand-labelled questions.

**Labels are phrases, not ids.** The corpus set labels a question with `law_id` and
`article_no`, which are stable because the corpus is pinned. An uploaded document has no
stable ids at all — `doc_id` is a fresh uuid per ingest and section numbers move whenever
the chunker changes. So a label here names a distinctive phrase from the passage that
answers the question, and retrieval is correct when a returned passage contains it. Every
phrase is verified to exist in the extracted text before anything is scored, so a typo in
a label fails loudly rather than depressing the score.

The document is `dubai-tenancy-law-26-2007.pdf` — a real government PDF, uploaded through
the real path. Using a file this project generated would measure only its own output.

    make eval-workspace
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "apps" / "api"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import numpy as np  # noqa: E402

from app.core.embedding import embed_documents  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.rag.rerank import apply_refusal_gate, rerank  # noqa: E402
from app.workspace.chunker import chunk_document, coverage  # noqa: E402
from app.workspace.extract import extract_bytes  # noqa: E402
from app.workspace.retriever import WorkspaceRetriever  # noqa: E402

DOCUMENT = REPO_ROOT / "corpus" / "pdf" / "dubai-tenancy-law-26-2007.pdf"
QUESTIONS = REPO_ROOT / "eval" / "workspace_questions.jsonl"
RESULTS = REPO_ROOT / "eval" / "results" / "workspace.json"

#: Reported at @5 to match §5, so the two tables can be read side by side.
TOP_K = 5


def normalise(text: str) -> str:
    """Whitespace-insensitive comparison: chunking rewraps lines, phrases must survive."""
    return " ".join(text.split()).lower()


def load_labels() -> list[dict[str, Any]]:
    with QUESTIONS.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def verify_labels(labels: list[dict[str, Any]], document_text: str) -> None:
    """Every phrase must exist in the document. A label that cannot be right is a bug."""
    haystack = normalise(document_text)
    missing = [
        label["id"]
        for label in labels
        if label["answerable"] and normalise(label["must_contain"]) not in haystack
    ]
    if missing:
        raise SystemExit(
            f"labels {missing} name phrases that do not appear in {DOCUMENT.name}. "
            "Fix the label — do not lower the score to match it."
        )


def main() -> int:
    settings = Settings()
    labels = load_labels()

    document = extract_bytes(DOCUMENT.read_bytes(), DOCUMENT.name, settings=settings)
    verify_labels(labels, document.text)

    chunks = chunk_document(document, "doc-eval", settings)
    kept = coverage(document, chunks)
    if kept < 1.0:
        raise SystemExit(f"ingest lost {(1 - kept):.2%} of the document; fix that first")

    vectors = np.asarray(embed_documents([c.text for c in chunks], settings), dtype=np.float32)
    retriever = WorkspaceRetriever.build(chunks, vectors, settings)
    workspace_settings = settings.model_copy(
        update={"refusal_score_floor": settings.workspace_refusal_score_floor}
    )

    rows: list[dict[str, Any]] = []
    for label in labels:
        started = time.perf_counter()
        retrieval = retriever.retrieve(label["question"])
        top = rerank(label["question"], list(retrieval.candidates), workspace_settings)
        # The jurisdiction check is off for a workspace, exactly as the API runs it.
        outcome = apply_refusal_gate(
            top, workspace_settings, best_dense=retrieval.best_dense, scope=None
        )
        elapsed = (time.perf_counter() - started) * 1000

        gold_rank: int | None = None
        if label["answerable"]:
            needle = normalise(label["must_contain"])
            for position, scored in enumerate(top, start=1):
                if needle in normalise(scored.chunk.text):
                    gold_rank = position
                    break

        rows.append(
            {
                "id": label["id"],
                "answerable": label["answerable"],
                "gold_rank": gold_rank,
                "refused": not outcome.covered,
                "best_score": outcome.best_score,
                "latency_ms": round(elapsed, 1),
            }
        )

    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]

    def rate(predicate: Any, of: list[dict[str, Any]]) -> float:
        return round(sum(1 for r in of if predicate(r)) / len(of), 4) if of else 0.0

    metrics = {
        "hit_rate_at_5": rate(
            lambda r: r["gold_rank"] is not None and r["gold_rank"] <= TOP_K, answerable
        ),
        "hit_rate_at_1": rate(lambda r: r["gold_rank"] == 1, answerable),
        "mrr": round(
            sum(0.0 if r["gold_rank"] is None else 1 / r["gold_rank"] for r in answerable)
            / len(answerable),
            4,
        )
        if answerable
        else 0.0,
        # The number that matters: does it decline a question this document cannot answer?
        "refusal_accuracy": rate(lambda r: r["refused"], unanswerable),
        # And the cost of that: a refusal on a question the document DOES answer.
        "false_refusals": sum(1 for r in answerable if r["refused"]),
    }
    latencies = sorted(r["latency_ms"] for r in rows)
    metrics["latency_p50_ms"] = latencies[len(latencies) // 2]
    metrics["latency_p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]

    payload = {
        "document": DOCUMENT.name,
        "pages": len(document.pages),
        "chunks": len(chunks),
        "coverage": kept,
        "questions": {"answerable": len(answerable), "unanswerable": len(unanswerable)},
        "refusal_score_floor": workspace_settings.refusal_score_floor,
        "metrics": metrics,
        "per_question": rows,
    }
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(
        f"\n  {DOCUMENT.name} — {len(document.pages)} pages, {len(chunks)} chunks, "
        f"coverage {kept:.2%}\n"
    )
    print(f"  hit-rate@5        {metrics['hit_rate_at_5']:.4f}   ({len(answerable)} answerable)")
    print(f"  hit-rate@1        {metrics['hit_rate_at_1']:.4f}")
    print(f"  MRR               {metrics['mrr']:.4f}")
    print(
        f"  refusal accuracy  {metrics['refusal_accuracy']:.4f}   "
        f"({len(unanswerable)} unanswerable)"
    )
    print(f"  false refusals    {metrics['false_refusals']}")
    print(
        f"  latency           p50 {metrics['latency_p50_ms']:.0f} ms · "
        f"p95 {metrics['latency_p95_ms']:.0f} ms"
    )
    print(f"\n  written to {RESULTS.relative_to(REPO_ROOT)}\n")

    for row in rows:
        if row["answerable"] and row["gold_rank"] is None:
            print(f"  MISS  {row['id']}: the labelled passage was not in the top 5")
        if row["answerable"] and row["refused"]:
            print(f"  FALSE REFUSAL  {row['id']}")
        if not row["answerable"] and not row["refused"]:
            print(f"  ANSWERED THE UNANSWERABLE  {row['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
