#!/usr/bin/env python3
"""Detect PDFs whose embedded font drops characters from a ligature.

This is the tool that found the defect described in AUDIT.md §3.2. The Labour Law's
font maps the ``Th`` ligature to the single codepoint ``T``, so its text layer reads
"Te employer shall" — a corruption that damages BM25 (wrong token) and the embedding
(wrong word) while looking perfectly fine in a PDF viewer.

The signal is geometric, not lexical. A glyph that draws two characters is drawn about
twice as wide as one that draws one, so a character whose advance-width distribution is
**bimodal** is drawing more than it claims. Run this against any new corpus before
trusting it:

    python scripts/glyph_audit.py corpus/pdf/*.pdf
"""

from __future__ import annotations

import argparse
import collections
import itertools
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

import fitz  # noqa: E402

# Widths are normalised by font size, so they are comparable across a document.
GAP_THRESHOLD: Final = 0.25
MIN_SAMPLES: Final = 8
MIN_CLUSTER: Final = 3


def _collect(
    path: Path,
) -> tuple[dict[str, list[float]], dict[tuple[str, int], list[str]]]:
    """Measure every alphabetic glyph's advance width, normalised by font size."""
    widths: dict[str, list[float]] = collections.defaultdict(list)
    samples: dict[tuple[str, int], list[str]] = collections.defaultdict(list)

    document = fitz.open(path)
    try:
        for page_index in range(document.page_count):
            for block in document[page_index].get_text("rawdict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        _measure_span(span, widths, samples)
    finally:
        document.close()
    return widths, samples


def _measure_span(
    span: dict[str, Any],
    widths: dict[str, list[float]],
    samples: dict[tuple[str, int], list[str]],
) -> None:
    chars: list[dict[str, Any]] = span["chars"]
    size: float = float(span["size"] or 1.0)
    for position, char in enumerate(chars):
        code = char["c"]
        if not code.isalpha():
            continue
        ratio = (char["bbox"][2] - char["bbox"][0]) / size
        widths[code].append(ratio)
        samples[(code, int(ratio * 10))].append(
            "".join(c["c"] for c in chars[position : position + 7])
        )


def audit(path: Path) -> list[str]:
    """Return a human-readable finding per suspicious character."""
    widths, samples = _collect(path)

    findings: list[str] = []
    for code, observed in sorted(widths.items()):
        if len(observed) < MIN_SAMPLES:
            continue
        ordered = sorted(observed)
        # Largest gap between consecutive widths: a real bimodality shows up as one
        # wide empty band, not as gradual spread.
        gaps = [(b - a, a, b) for a, b in itertools.pairwise(ordered)]
        if not gaps:
            continue
        gap, low, high = max(gaps)
        if gap < GAP_THRESHOLD:
            continue
        narrow = [w for w in ordered if w <= low]
        wide = [w for w in ordered if w >= high]
        if len(narrow) < MIN_CLUSTER or len(wide) < MIN_CLUSTER:
            continue
        narrow_example = samples[(code, int(low * 10))][:2]
        wide_example = samples[(code, int(high * 10))][:2]
        findings.append(
            f"  {code!r}: narrow n={len(narrow)} (max {low:.2f}) e.g. {narrow_example}\n"
            f"       wide   n={len(wide)} (min {high:.2f}) e.g. {wide_example}\n"
            f"       gap {gap:.2f} — the wide cluster is drawing more than one character"
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdfs", nargs="+", type=Path)
    args = parser.parse_args(argv)

    total = 0
    for path in args.pdfs:
        findings = audit(path)
        print(f"\n### {path.name}")
        if findings:
            total += len(findings)
            print("\n".join(findings))
        else:
            print("  no bimodal glyph-width anomalies")

    print(
        f"\n{total} suspicious character(s). Each needs a rule in "
        "app/rag/parse.py:LIGATURE_REPAIRS, or the corpus text is wrong."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
