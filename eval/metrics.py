"""Metric definitions, kept apart from the runner so they are unit-testable.

Two families, and the distinction matters when reading a report:

**Retrieval metrics** (hit-rate, MRR, precision, recall) are computed against the
hand-written labels in ``questions.jsonl``. They need no model and no API key, so they
are exact, reproducible and free — the numbers do not move between runs.

**Generation metrics** (faithfulness, answer relevance) require a judge model. They are
defined below to match RAGAS, and are reported as ``None`` — rendered "pending" in the UI
— whenever no judge is configured. A retrieval number measured offline can therefore
never be mistaken for a faithfulness score.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any


@dataclass(frozen=True, slots=True)
class Label:
    """The ground truth for one question."""

    question_id: str
    question: str
    answerable: bool
    law_id: str | None
    article_no: int | None
    part: str | None
    answer: str

    def matches(self, law_id: str, article_no: int, part: str) -> bool:
        return (
            self.law_id == law_id
            and self.article_no == article_no
            and (self.part is None or self.part == part)
        )


@dataclass(slots=True)
class RetrievalOutcome:
    """What retrieval produced for one question."""

    label: Label
    #: 1-based rank of the labelled article, or None when it was not retrieved at all.
    gold_rank: int | None
    retrieved: int
    refused: bool
    best_score: float | None
    latency_ms: float


def reciprocal_rank(rank: int | None) -> float:
    return 0.0 if rank is None else 1.0 / rank


@dataclass(slots=True)
class RetrievalMetrics:
    """Aggregate retrieval quality over the answerable questions."""

    hit_rate_at_1: float = 0.0
    hit_rate_at_5: float = 0.0
    mrr: float = 0.0
    context_precision: float = 0.0
    context_recall: float = 0.0
    refusal_accuracy: float = 0.0
    false_refusals: int = 0
    missed_refusals: int = 0
    answerable: int = 0
    traps: int = 0
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    per_question: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "hit_rate_at_1": round(self.hit_rate_at_1, 4),
            "hit_rate_at_5": round(self.hit_rate_at_5, 4),
            "mrr": round(self.mrr, 4),
            "context_precision": round(self.context_precision, 4),
            "context_recall": round(self.context_recall, 4),
            "refusal_accuracy": round(self.refusal_accuracy, 4),
            "false_refusals": self.false_refusals,
            "missed_refusals": self.missed_refusals,
            "answerable": self.answerable,
            "traps": self.traps,
            "faithfulness": self.faithfulness,
            "answer_relevancy": self.answer_relevancy,
        }


def score(outcomes: list[RetrievalOutcome], top_k: int) -> RetrievalMetrics:
    """Aggregate per-question outcomes into the reported metrics.

    Definitions, stated explicitly because "hit-rate" means different things in
    different papers:

    * **hit-rate@k** — share of answerable questions whose labelled article appears
      anywhere in the top k passages.
    * **MRR** — mean of 1/rank of the labelled article, 0 when absent.
    * **context precision** — share of returned passages that belong to the labelled
      article. With a single gold article and k=5 the ceiling is below 1.0 by
      construction; it is reported because its *movement* between configurations is
      informative even though its absolute value is bounded.
    * **context recall** — share of answerable questions whose labelled article was
      retrieved at all, i.e. hit-rate at the full retrieved depth.
    * **refusal accuracy** — share of trap questions correctly declined. Reported
      alongside ``false_refusals``, because a system that refuses everything scores a
      perfect 1.0 here and is useless.
    """
    answerable = [o for o in outcomes if o.label.answerable]
    traps = [o for o in outcomes if not o.label.answerable]

    metrics = RetrievalMetrics(answerable=len(answerable), traps=len(traps))
    if answerable:
        metrics.hit_rate_at_1 = _mean(1.0 if o.gold_rank == 1 else 0.0 for o in answerable)
        metrics.hit_rate_at_5 = _mean(
            1.0 if o.gold_rank is not None and o.gold_rank <= top_k else 0.0 for o in answerable
        )
        metrics.mrr = _mean(reciprocal_rank(o.gold_rank) for o in answerable)
        metrics.context_precision = _mean(
            (1.0 / o.retrieved) if o.gold_rank is not None and o.retrieved else 0.0
            for o in answerable
        )
        metrics.context_recall = _mean(1.0 if o.gold_rank is not None else 0.0 for o in answerable)
        metrics.false_refusals = sum(1 for o in answerable if o.refused)
    if traps:
        correct = sum(1 for o in traps if o.refused)
        metrics.refusal_accuracy = correct / len(traps)
        metrics.missed_refusals = len(traps) - correct

    metrics.per_question = [
        {
            "id": o.label.question_id,
            "question": o.label.question,
            "answerable": o.label.answerable,
            "gold": (
                None if o.label.article_no is None else f"{o.label.law_id}#art{o.label.article_no}"
            ),
            "gold_rank": o.gold_rank,
            "refused": o.refused,
            "best_score": None if o.best_score is None else round(o.best_score, 3),
            "latency_ms": round(o.latency_ms, 1),
        }
        for o in outcomes
    ]
    return metrics


def latency_summary(outcomes: list[RetrievalOutcome]) -> dict[str, float]:
    values = sorted(o.latency_ms for o in outcomes)
    if not values:
        return {}
    return {
        "p50_ms": round(median(values), 1),
        "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 1),
        "max_ms": round(values[-1], 1),
    }


def calibrate_floor(outcomes: list[RetrievalOutcome]) -> dict[str, float]:
    """Choose the refusal floor from measured scores rather than intuition.

    The floor is placed midway between the worst answerable question's best score and
    the best trap's best score. Where those two populations overlap there is no floor
    that separates them, and the midpoint is still the choice that minimises total
    error — but ``separation`` goes negative and says so plainly, which is the number
    worth reporting.
    """
    answerable = [o.best_score for o in outcomes if o.label.answerable and o.best_score is not None]
    traps = [o.best_score for o in outcomes if not o.label.answerable and o.best_score is not None]
    if not answerable or not traps:
        return {}
    worst_answerable = min(answerable)
    best_trap = max(traps)
    return {
        "answerable_min": round(worst_answerable, 3),
        "trap_max": round(best_trap, 3),
        "separation": round(worst_answerable - best_trap, 3),
        "floor": round((worst_answerable + best_trap) / 2.0, 3),
    }


def _mean(values: Any) -> float:
    collected = list(values)
    return sum(collected) / len(collected) if collected else 0.0
