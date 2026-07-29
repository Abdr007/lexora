#!/usr/bin/env python3
"""Fail the build when retrieval quality regresses.

A quality regression should break the build like any other failing test. The floors
below are the numbers recorded in AUDIT.md, minus a small tolerance for run-to-run
variation in the reranker.

This lives in a file rather than inline in the workflow so that `make ci` and GitHub
Actions execute *the same* gate. A duplicated heredoc is a gate that drifts, and the
whole point is that what passes locally passes remotely.

    python scripts/quality_gate.py [--results eval/results/latest.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# name -> minimum acceptable value, from AUDIT.md §5.1
FLOORS: Final[dict[str, float]] = {
    "hit_rate_at_5": 0.90,
    "hit_rate_at_1": 0.70,
    "mrr": 0.78,
    "refusal_accuracy": 0.78,
}
# A false refusal is a correct answer withheld. Zero is the only acceptable count.
MAX_FALSE_REFUSALS: Final = 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results", type=Path, default=REPO_ROOT / "eval" / "results" / "latest.json"
    )
    args = parser.parse_args(argv)

    if not args.results.exists():
        print(f"QUALITY GATE FAILED  no results at {args.results} — run `make eval` first")
        return 1

    report = json.loads(args.results.read_text(encoding="utf-8"))
    arm = report.get("configurations", {}).get("with_rerank")
    if not arm:
        print("QUALITY GATE FAILED  results contain no `with_rerank` configuration")
        return 1

    failures: list[str] = []
    print(f"  {'metric':22} {'value':>8} {'floor':>8}")
    for name, floor in FLOORS.items():
        value = arm.get(name)
        if not isinstance(value, (int, float)):
            failures.append(f"{name}: missing from the report")
            continue
        mark = "ok" if value >= floor else "FAIL"
        print(f"  {name:22} {value:8.4f} {floor:8.2f}  {mark}")
        if value < floor:
            failures.append(f"{name}: {value:.4f} < {floor}")

    false_refusals = int(arm.get("false_refusals", 0))
    mark = "ok" if false_refusals <= MAX_FALSE_REFUSALS else "FAIL"
    print(f"  {'false_refusals':22} {false_refusals:8d} {MAX_FALSE_REFUSALS:8d}  {mark}")
    if false_refusals > MAX_FALSE_REFUSALS:
        failures.append(f"false_refusals: {false_refusals} > {MAX_FALSE_REFUSALS}")

    if failures:
        print()
        for line in failures:
            print(f"QUALITY GATE FAILED  {line}")
        return 1

    print("\n  quality gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
