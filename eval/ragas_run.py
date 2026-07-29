#!/usr/bin/env python3
"""Evaluate Lexora against ``eval/questions.jsonl`` and write ``eval/results/latest.json``.

    python eval/ragas_run.py                 # both configurations, retrieval metrics
    python eval/ragas_run.py --no-rerank     # fused candidates only, no cross-encoder
    python eval/ragas_run.py --judge         # add Claude-judged faithfulness/relevance
    python eval/ragas_run.py --calibrate     # recompute the refusal floor from the data

The default run measures **both** arms — with and without reranking — because the number
that matters is the delta, not either value on its own. Running one arm and asserting the
other would be exactly the kind of unmeasured claim this project exists to avoid.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "apps" / "api") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app.core.claude import engine_name, is_online  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.core.vectorstore import close_client  # noqa: E402
from app.rag.pipeline import Pipeline  # noqa: E402
from app.rag.rerank import apply_refusal_gate  # noqa: E402
from app.rag.scope import check_scope  # noqa: E402
from eval.metrics import (  # noqa: E402
    Label,
    RetrievalOutcome,
    calibrate_floor,
    latency_summary,
    score,
)

logger = logging.getLogger(__name__)

QUESTIONS: Final = REPO_ROOT / "eval" / "questions.jsonl"
RESULTS_DIR: Final = REPO_ROOT / "eval" / "results"


def load_labels() -> list[Label]:
    if not QUESTIONS.exists():
        raise SystemExit(f"{QUESTIONS} not found. Run `python eval/build_questions.py` first.")
    labels: list[Label] = []
    with QUESTIONS.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row: dict[str, Any] = json.loads(line)
            labels.append(
                Label(
                    question_id=row["id"],
                    question=row["question"],
                    answerable=bool(row["answerable"]),
                    law_id=row.get("law_id"),
                    article_no=row.get("article_no"),
                    part=row.get("part"),
                    answer=row.get("answer", ""),
                )
            )
    return labels


def _part_of(part_id: str) -> str:
    return "cabinet-resolution" if "cabinet" in part_id else "decree-law"


def run_configuration(
    pipeline: Pipeline, labels: list[Label], *, use_rerank: bool
) -> list[RetrievalOutcome]:
    """Retrieve for every question and record where the labelled article landed."""
    settings = pipeline.settings
    outcomes: list[RetrievalOutcome] = []
    for position, label in enumerate(labels, start=1):
        started = time.perf_counter()
        retrieval = pipeline.retriever.retrieve(label.question)
        ranked = pipeline.rank(label.question, retrieval, use_rerank=use_rerank)
        elapsed = (time.perf_counter() - started) * 1000
        gate = apply_refusal_gate(
            ranked,
            settings,
            best_dense=retrieval.best_dense,
            scope=check_scope(label.question),
        )

        gold_rank: int | None = None
        if label.answerable and label.law_id is not None and label.article_no is not None:
            for rank, item in enumerate(ranked, start=1):
                if label.matches(
                    item.chunk.law_id, item.chunk.article_no, _part_of(item.chunk.part_id)
                ):
                    gold_rank = rank
                    break

        outcomes.append(
            RetrievalOutcome(
                label=label,
                gold_rank=gold_rank,
                retrieved=len(ranked),
                refused=not gate.covered,
                best_score=gate.best_score,
                latency_ms=elapsed,
            )
        )
        if position % 10 == 0:
            print(f"    {position}/{len(labels)}")
    return outcomes


def run_judge(pipeline: Pipeline, labels: list[Label]) -> dict[str, float | None]:
    """Faithfulness and answer relevance over the answerable questions."""
    from eval.judge import judge

    answerable = [label for label in labels if label.answerable]
    faithfulness: list[float] = []
    relevance: list[float] = []
    for position, label in enumerate(answerable, start=1):
        result = pipeline.run(label.question)
        if result.answer.kind.value != "answer":
            continue
        verdict = judge(
            label.question,
            result.answer.text,
            [item.chunk.text for item in result.answer.evidence],
            pipeline.settings,
        )
        if verdict.faithfulness is not None:
            faithfulness.append(verdict.faithfulness)
        if verdict.answer_relevancy is not None:
            relevance.append(verdict.answer_relevancy)
        if position % 5 == 0:
            print(f"    judged {position}/{len(answerable)}")
    return {
        "faithfulness": round(sum(faithfulness) / len(faithfulness), 4) if faithfulness else None,
        "answer_relevancy": round(sum(relevance) / len(relevance), 4) if relevance else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-rerank", action="store_true", help="evaluate the no-rerank arm only")
    parser.add_argument("--judge", action="store_true", help="add Claude-judged metrics")
    parser.add_argument("--calibrate", action="store_true", help="recompute the refusal floor")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / "latest.json")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    labels = load_labels()
    answerable = sum(1 for label in labels if label.answerable)
    print(
        f"Lexora evaluation — {len(labels)} questions "
        f"({answerable} answerable, {len(labels) - answerable} traps)"
    )
    print(f"  engine   {engine_name(settings)}")
    print(f"  reranker {settings.reranker_model}")

    pipeline = Pipeline(settings)
    try:
        configurations: dict[str, list[RetrievalOutcome]] = {}
        arms = (
            [("no_rerank", False)]
            if args.no_rerank
            else [("no_rerank", False), ("with_rerank", True)]
        )
        for name, use_rerank in arms:
            print(f"\n  running {name}…")
            configurations[name] = run_configuration(pipeline, labels, use_rerank=use_rerank)

        judged: dict[str, float | None] = {"faithfulness": None, "answer_relevancy": None}
        if args.judge:
            if not is_online(settings):
                print("\n  --judge requires ANTHROPIC_API_KEY; skipping judged metrics")
            else:
                print("\n  judging generations…")
                judged = run_judge(pipeline, labels)

        payload: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "engine": engine_name(settings),
            "judge_model": settings.answer_model if args.judge and is_online(settings) else None,
            "reranker": settings.reranker_model,
            "embedding_model": settings.embedding_model,
            "dataset": {
                "questions": len(labels),
                "answerable": answerable,
                "traps": len(labels) - answerable,
            },
            "configurations": {},
            "latency_ms": {},
        }

        for name, outcomes in configurations.items():
            metrics = score(outcomes, settings.rerank_top_k)
            entry = metrics.as_dict()
            if name == "with_rerank":
                entry["faithfulness"] = judged["faithfulness"]
                entry["answer_relevancy"] = judged["answer_relevancy"]
            payload["configurations"][name] = entry
            payload["latency_ms"][name] = latency_summary(outcomes)

        primary = configurations.get("with_rerank") or next(iter(configurations.values()))
        calibration = calibrate_floor(primary)
        if calibration:
            payload["refusal_calibration"] = {
                **calibration,
                "active_floor": settings.refusal_score_floor,
            }

        chunking = RESULTS_DIR / "chunking.json"
        if chunking.exists():
            payload["chunking_experiment"] = json.loads(chunking.read_text(encoding="utf-8"))

        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        detail = RESULTS_DIR / "per_question.json"
        detail.write_text(
            json.dumps(
                {
                    name: score(o, settings.rerank_top_k).per_question
                    for name, o in configurations.items()
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        _report(payload, args.calibrate, settings.refusal_score_floor)
        print(f"\n  wrote {args.out.relative_to(REPO_ROOT)}")
        return 0
    finally:
        close_client()


def _format_metric(value: object) -> str:
    """Render a metric cell: an absent measurement must not look like a bad one."""
    if value is None:
        return "pending"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _report(payload: dict[str, Any], show_calibration: bool, active_floor: float) -> None:
    print("\n" + "=" * 74)
    rows = [
        ("hit_rate_at_1", "hit-rate@1"),
        ("hit_rate_at_5", "hit-rate@5"),
        ("mrr", "MRR"),
        ("context_precision", "context precision"),
        ("context_recall", "context recall"),
        ("refusal_accuracy", "refusal accuracy"),
        ("false_refusals", "false refusals"),
        ("faithfulness", "faithfulness"),
        ("answer_relevancy", "answer relevance"),
    ]
    configurations = payload["configurations"]
    before = configurations.get("no_rerank", {})
    after = configurations.get("with_rerank", {})
    print(f"  {'metric':22} {'no rerank':>12} {'with rerank':>13} {'delta':>10}")
    print("  " + "-" * 60)
    for key, label in rows:
        b, a = before.get(key), after.get(key)
        delta = (
            f"{a - b:+.4f}"
            if isinstance(a, (int, float))
            and isinstance(b, (int, float))
            and key != "false_refusals"
            else (f"{a - b:+d}" if isinstance(a, int) and isinstance(b, int) else "")
        )
        print(f"  {label:22} {_format_metric(b):>12} {_format_metric(a):>13} {delta:>10}")

    for name, stats in payload.get("latency_ms", {}).items():
        if stats:
            print(
                f"\n  latency {name:12} p50 {stats['p50_ms']}ms  "
                f"p95 {stats['p95_ms']}ms  max {stats['max_ms']}ms"
            )

    calibration = payload.get("refusal_calibration")
    if calibration:
        print(
            f"\n  refusal calibration: answerable_min {calibration['answerable_min']}  "
            f"trap_max {calibration['trap_max']}  separation {calibration['separation']}"
        )
        print(f"    suggested floor {calibration['floor']}   active floor {active_floor}")
        if show_calibration:
            print(
                "\n  To adopt the suggested floor, set in .env:\n"
                f"    LEXORA_REFUSAL_SCORE_FLOOR={calibration['floor']}"
            )


if __name__ == "__main__":
    raise SystemExit(main())
