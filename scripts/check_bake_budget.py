#!/usr/bin/env python3
"""Fail the build when a bake stage drifts toward the memory ceiling.

CI builds the image on a runner with gigabytes to spare, so `docker build` succeeding
proves nothing about whether the image can be built on a constrained builder. Two
deploys were lost to that gap: the Hugging Face build container OOM-killed the bake
(exit 137) while CI stayed green, because CI had no ceiling to hit.

This reads the build log, extracts the peak RSS each stage prints, and fails if any stage
exceeds the budget — turning "did it OOM on someone else's builder" into a number that
regresses visibly here first.

It also fails when a stage is *missing*. A budget check over an empty list passes, so a
stage that silently stopped reporting would read as success — the same shape of failure
this script exists to catch.

    docker build --progress=plain -t lexora-api:ci . 2>&1 | tee build.log
    python scripts/check_bake_budget.py build.log
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Final

# Measured peaks at the time of writing: embedding 290 MB, reranker 254 MB,
# vectorstore 157 MB. The budget is deliberately loose — it is a regression guard, not a
# tuning target. Anything approaching it means a stage started holding something new.
BUDGET_MB: Final = 600

EXPECTED_STAGES: Final = ("embedding", "reranker", "vectorstore")

# Matches: baked [embedding] embedding model cached — peak RSS 290 MB
LINE: Final = re.compile(r"baked \[(?P<stage>\w+)].*?peak RSS (?P<mb>\d+(?:\.\d+)?) MB")


def parse(log: str) -> dict[str, float]:
    """Peak RSS per stage. A stage reported twice keeps its highest reading."""
    peaks: dict[str, float] = {}
    for match in LINE.finditer(log):
        stage = match.group("stage")
        peaks[stage] = max(peaks.get(stage, 0.0), float(match.group("mb")))
    return peaks


def problems(peaks: dict[str, float], budget_mb: int = BUDGET_MB) -> list[str]:
    found: list[str] = []
    for stage in EXPECTED_STAGES:
        if stage not in peaks:
            found.append(
                f"stage {stage!r} reported no peak RSS — it did not run, or it stopped "
                f"printing. A budget check over a missing stage passes vacuously."
            )
    for stage, mb in sorted(peaks.items()):
        if mb > budget_mb:
            found.append(f"stage {stage!r} peaked at {mb:.0f} MB, over the {budget_mb} MB budget")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="build log captured with --progress=plain")
    parser.add_argument("--budget-mb", type=int, default=BUDGET_MB)
    args = parser.parse_args(argv)

    peaks = parse(args.log.read_text(encoding="utf-8", errors="replace"))
    for stage in EXPECTED_STAGES:
        reading = f"{peaks[stage]:.0f} MB" if stage in peaks else "MISSING"
        print(f"  {stage:12} {reading}")

    found = problems(peaks, args.budget_mb)
    if found:
        print("\nbake memory check failed:")
        for problem in found:
            print(f"  - {problem}")
        return 1

    print(f"\nall stages within the {args.budget_mb} MB budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
