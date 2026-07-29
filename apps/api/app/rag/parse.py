"""Structure-aware parsing of the corpus PDFs into numbered articles.

Why this file is not three lines of ``page.get_text()``
------------------------------------------------------
The corpus is real government typesetting. Five separate properties of it would each
silently poison every downstream metric if the text were extracted naively. Every one
was found by measuring the actual files, and every one is asserted in
``tests/test_parse.py`` against those files.

1. **Two-column layout.** The MOHRE Labour Law is a landscape two-column document.
   Reading blocks in y-order interleaves the columns, splicing Article 2 into Article 4
   mid-sentence. Blocks are assigned to a column and read column-major.

2. **A broken ``Th`` ligature.** The Labour Law embeds ``CronosPro`` with the ``Th``
   ligature mapped to the single codepoint ``T``, so the text layer literally reads
   "Te employer shall" and "Tis Decree-Law". A word list would be a guess ("Ten",
   "Tree" and "Tan" are real words). The defect is instead detected geometrically: the
   ligature glyph is drawn twice as wide as a real ``T``. Measured over the whole
   document the two populations are cleanly separated with no overlap —

       real ``T``        n=95   advance/font-size 0.52 - 0.62   ("Type of", "Their")
       ``Th`` ligature   n=306  advance/font-size 1.00 - 1.13   ("Te employer")

   so :data:`WIDE_GLYPH_RATIO` = 0.80 sits inside a 0.38-wide empty gap. Only the wide
   population is rewritten. ``scripts/glyph_audit.py`` is the tool that found this and
   re-runs on any new corpus to prove no other glyph is affected.

3. **Contents pages that look exactly like article headings.** Both instruments in the
   Labour Law PDF open with a table of contents listing ``Article (1) ... Article (74)``.
   Indexing those would create 113 empty articles. Contents pages are detected by their
   "Contents" heading and excluded from the body pass — then *reused* as an independent
   ground-truth index to validate what the body pass produced (see
   :func:`reconcile_with_contents`). The document checks the parser.

4. **One PDF, several instruments.** The Labour Law file also contains Cabinet
   Resolution No. 1 of 2022, whose article numbering restarts at 1. "Article 5" is
   ambiguous across them, so instruments are split into parts and every citation key
   carries its part.

5. **A missing heading in the source document.** Cabinet Resolution Article (2) has no
   ``Article (2)`` label on its body page — the official PDF prints only its section
   title, though its own table of contents lists the article. The gap is recovered from
   the section-title marker and reported in the diagnostics rather than being papered
   over.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import fitz

from app.core.models import Article, ParsedDocument
from app.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# ── glyph-level repair ───────────────────────────────────────────────────────
# A glyph whose advance exceeds this multiple of its font size is drawing more than
# one character. Only applied to codepoints listed in LIGATURE_REPAIRS: naturally wide
# letters such as m, W and M also exceed this ratio and must not be touched.
WIDE_GLYPH_RATIO: Final = 0.80
LIGATURE_REPAIRS: Final[dict[str, str]] = {"T": "Th"}

# ── line classification ──────────────────────────────────────────────────────
# An article heading is a line consisting of nothing but the article label, optionally
# followed by a footnote marker. Anchoring BOTH ends is what stops the in-body
# cross-reference "...in accordance with Article (9) of this Law." being read as a
# heading. Verified against every `Article ...` line in the corpus: 270 bare labels,
# 2 with a trailing asterisk, 1 contents range, 1 in-body cross-reference.
ARTICLE_HEADING_RE: Final = re.compile(
    r"^\s*Article\s*\(?\s*(\d{1,3})\s*\)?\s*[*†‡]?\s*[:.–—-]?\s*$",
    re.IGNORECASE,
)
# Contents rows may cover a span, e.g. "Article (58-64)".
CONTENTS_ENTRY_RE: Final = re.compile(
    r"^\s*Article\s*\(?\s*(\d{1,3})\s*(?:[-–—]\s*(\d{1,3}))?\s*\)?\s*$",
    re.IGNORECASE,
)
# A legal instrument's own title line: the designation and NOTHING else. Anchoring the
# end is essential — the recitals of every Dubai law list other instruments
# ("Law No. (16) of 2007 Establishing the Real Estate Regulatory Agency;") and those
# must not be mistaken for the title of the document being parsed.
INSTRUMENT_TITLE_RE: Final = re.compile(
    r"^\s*(?P<kind>(?:Federal\s+)?(?:Decree[-\s]?Law|Cabinet\s+Resolution|Law|Decree|Resolution))"
    r"\s+No\.?\s*\(?\s*(?P<number>\d{1,4})\s*\)?\s*of\s+(?P<year>\d{4})\s*[*†‡]?\s*\d{0,2}\s*$",
    re.IGNORECASE,
)
# Same designation, but allowed to be followed by descriptive text. Used only to derive
# the canonical key of the manifest title, never to detect a title line in the PDF.
INSTRUMENT_KEY_RE: Final = re.compile(
    r"(?P<kind>(?:Federal\s+)?(?:Decree[-\s]?Law|Cabinet\s+Resolution|Law|Decree|Resolution))"
    r"\s+No\.?\s*\(?\s*(?P<number>\d{1,4})\s*\)?\s*of\s+(?P<year>\d{4})",
    re.IGNORECASE,
)
PAGE_FURNITURE_RE: Final = re.compile(r"^\s*(?:page\s+\d+\s+of\s+\d+|\d{1,4})\s*$", re.IGNORECASE)
CONTENTS_HEADING_RE: Final = re.compile(r"^\s*contents\s*$", re.IGNORECASE)
# The Labour Law's definitions table sets the term, its colon and its definition as
# three separate blocks. A lone colon carries no letters, so the generic
# "drop punctuation-only lines" rule (which removes bullet glyphs and the Arabic rules
# that separate Dubai law headings) would silently delete every separator in the table,
# yielding "State United Arab Emirates." instead of "State: United Arab Emirates.".
PUNCTUATION_JOINER_RE: Final = re.compile(r"^[:;]$")
# Section-title bullet the Labour Law prefixes to every article title.
TITLE_MARKER_CHARS: Final = "►▶•●▪»"

# A line is page furniture when its font is meaningfully smaller than the body font.
# Relative, not absolute: body text is 8pt in the Labour Law and 11pt in the Dubai
# laws, while their footnotes are 6.5pt and 8pt respectively.
FURNITURE_SIZE_RATIO: Final = 0.90
FULL_WIDTH_RATIO: Final = 0.75
GUTTER_RATIO: Final = 0.02
MAX_TITLE_CHARS: Final = 90
CENTRED_TOLERANCE_RATIO: Final = 0.10
MIN_BLOCKS_FOR_COLUMN_DETECTION: Final = 4
# A contents row needs at least a label and a title to be usable.
MIN_CONTENTS_ROW_CELLS: Final = 2
# A page is either one column or two; nothing in this corpus uses three.
TWO_COLUMNS: Final = 2
# Baseline tolerance when reassembling table-of-contents rows from stacked blocks.
ROW_TOLERANCE_PT: Final = 3.0
# Blocks and lines whose tops fall in the same band of this height are one visual row
# and must read left-to-right. Without it, the Labour Law definitions table inverts:
# the definition block starts at y=121.6 and its own term at y=121.7, so ordering by
# raw y emits "United Arab Emirates." before the word "State" it defines. The band is
# far narrower than the tightest line pitch in the corpus (10pt), so genuine rows never
# merge.
ROW_QUANTUM_PT: Final = 3.0
# Largest article span a single contents row may cover, e.g. "Article (58-64)".
MAX_CONTENTS_SPAN: Final = 50
# A line reaching this fraction of the text-column width is a justified body line,
# never a title. See the docstring of `_looks_like_title`.
JUSTIFIED_LINE_RATIO: Final = 0.90
# When a title is recognised only by being centred, it must also be clearly short.
CENTRED_TITLE_WIDTH_RATIO: Final = 0.60
# Percentile of body-line widths used as the text-column width.
BODY_WIDTH_PERCENTILE: Final = 0.90
# Whitespace handling after NFKC. Written with escapes so a reviewer can read the exact
# ranges rather than trust invisible literals.
# Zero-width characters carry no spacing and must be DELETED, not collapsed to a
# space: turning U+200B into " " inserts a word break the document never had.
_ZERO_WIDTH_RE: Final = re.compile("[\\u200b-\\u200d\\ufeff]")
# Everything genuinely space-like collapses to one space.
_WHITESPACE_RE: Final = re.compile("[ \\t\\r\\n\\u00a0\\u2000-\\u200a\\u202f\\u205f\\u3000]+")

InstrumentKey = tuple[str, int, int]

# `ParsedDocument` is defined in app.core.models but is part of this module's public
# surface: callers parse a corpus and receive them. Declared explicitly because
# `no_implicit_reexport` (correctly) refuses to forward an import by accident.
__all__ = [
    "ARTICLE_HEADING_RE",
    "INSTRUMENT_TITLE_RE",
    "LIGATURE_REPAIRS",
    "WIDE_GLYPH_RATIO",
    "Article",
    "ContentsEntry",
    "ParseDiagnostics",
    "ParseError",
    "ParsedDocument",
    "load_manifest",
    "parse_corpus",
    "parse_pdf",
    "reconcile_with_contents",
]


class ParseError(RuntimeError):
    """The corpus could not be parsed into a well-formed article structure."""


@dataclass(frozen=True, slots=True)
class _Line:
    """One visual line of text with the geometry needed to classify it."""

    text: str
    page: int
    x0: float
    y0: float
    x1: float
    size: float
    bold: bool

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass(frozen=True, slots=True)
class _Block:
    """A layout block: the unit that column ordering operates on."""

    lines: tuple[_Line, ...]
    x0: float
    y0: float
    x1: float

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def width(self) -> float:
        return self.x1 - self.x0


@dataclass(frozen=True, slots=True)
class _Layout:
    """Document-wide typographic scale, measured once from the file itself.

    Nothing here is hardcoded per document: `body_size` is the font size carrying the
    most characters, and `body_line_width` is the 90th-percentile width of lines set in
    it — i.e. the width of a full justified line in the text column.
    """

    body_size: float
    body_line_width: float

    @property
    def furniture_ceiling(self) -> float:
        return self.body_size * FURNITURE_SIZE_RATIO


@dataclass(frozen=True, slots=True)
class ContentsEntry:
    """One row of a document's own table of contents — independent ground truth."""

    article_no: int
    title: str


@dataclass(slots=True)
class ParseDiagnostics:
    """Observable evidence that the parser did what it claims.

    Printed by ``make index`` and asserted in tests, so a regression in extraction
    quality fails a build instead of quietly degrading retrieval.
    """

    ligature_repairs: int = 0
    contents_pages_skipped: int = 0
    furniture_lines_dropped: int = 0
    duplicate_article_splits: int = 0
    parts_detected: int = 0
    inferred_headings: int = 0
    contents_entries_seen: int = 0
    titles_from_contents: int = 0
    contents_missing_in_body: list[str] = field(default_factory=list)
    contents_extra_in_body: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ligature_repairs": self.ligature_repairs,
            "contents_pages_skipped": self.contents_pages_skipped,
            "furniture_lines_dropped": self.furniture_lines_dropped,
            "duplicate_article_splits": self.duplicate_article_splits,
            "parts_detected": self.parts_detected,
            "inferred_headings": self.inferred_headings,
            "contents_entries_seen": self.contents_entries_seen,
            "titles_from_contents": self.titles_from_contents,
            "contents_missing_in_body": self.contents_missing_in_body,
            "contents_extra_in_body": self.contents_extra_in_body,
        }

    @property
    def contents_check_passed(self) -> bool:
        return not self.contents_missing_in_body and not self.contents_extra_in_body


# ─────────────────────────────────────────────────────────────────────────────
# Glyph -> text
# ─────────────────────────────────────────────────────────────────────────────


def _span_text(span: dict[str, Any], diagnostics: ParseDiagnostics) -> str:
    """Reconstruct a span's text, repairing ligatures that lost characters."""
    size = float(span.get("size") or 0.0)
    out: list[str] = []
    for char in span["chars"]:
        code: str = char["c"]
        replacement = LIGATURE_REPAIRS.get(code)
        if replacement is not None and size > 0:
            bbox = char["bbox"]
            if (bbox[2] - bbox[0]) / size > WIDE_GLYPH_RATIO:
                out.append(replacement)
                diagnostics.ligature_repairs += 1
                continue
        out.append(code)
    return "".join(out)


def _normalise(text: str) -> str:
    """NFKC-fold compatibility ligatures (fi, ffi, fl) and collapse whitespace.

    NFKC already maps U+00A0 to a plain space, so no separate pass is needed for it.
    """
    folded = _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFKC", text))
    return _WHITESPACE_RE.sub(" ", folded).strip()


def _has_letters_or_digits(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def _instrument_key(text: str) -> InstrumentKey | None:
    """Canonical identity of a legal instrument: ``(kind, number, year)``."""
    match = INSTRUMENT_KEY_RE.search(text)
    if match is None:
        return None
    kind = re.sub(r"[\s-]+", "", match.group("kind")).lower()
    return (kind, int(match.group("number")), int(match.group("year")))


# ─────────────────────────────────────────────────────────────────────────────
# Page -> ordered lines
# ─────────────────────────────────────────────────────────────────────────────


def _extract_blocks(page: fitz.Page, page_no: int, diagnostics: ParseDiagnostics) -> list[_Block]:
    raw: dict[str, Any] = page.get_text("rawdict")
    blocks: list[_Block] = []
    for raw_block in raw["blocks"]:
        if raw_block.get("type") != 0:
            continue
        lines: list[_Line] = []
        for raw_line in raw_block.get("lines", []):
            spans = raw_line["spans"]
            if not spans:
                continue
            text = _normalise("".join(_span_text(sp, diagnostics) for sp in spans))
            if not text:
                continue
            lx0, ly0, lx1, _ly1 = raw_line["bbox"]
            lines.append(
                _Line(
                    text=text,
                    page=page_no,
                    x0=lx0,
                    y0=ly0,
                    x1=lx1,
                    size=max(float(sp["size"]) for sp in spans),
                    bold=any(
                        "bold" in str(sp.get("font", "")).lower()
                        or bool(int(sp.get("flags", 0)) & 16)
                        for sp in spans
                    ),
                )
            )
        if not lines:
            continue
        # Within a block, horizontal text reads top-down then left-right. The PDF's
        # internal line order does not guarantee this (the Labour Law emits some
        # headings after their own subtitle), so it is imposed here.
        lines.sort(key=lambda ln: (_row_band(ln.y0), ln.x0))
        bx0, by0, bx1, _by1 = raw_block["bbox"]
        blocks.append(_Block(lines=tuple(lines), x0=bx0, y0=by0, x1=bx1))
    return blocks


def _row_band(y: float) -> int:
    """Quantise a top coordinate into a visual row band. See ROW_QUANTUM_PT."""
    return int(y // ROW_QUANTUM_PT)


def _is_two_column(blocks: list[_Block], page_width: float) -> bool:
    """True when the page is typeset in two columns separated by a clear gutter."""
    mid = page_width / 2.0
    margin = page_width * GUTTER_RATIO
    narrow = [b for b in blocks if b.width < page_width * FULL_WIDTH_RATIO]
    if len(narrow) < MIN_BLOCKS_FOR_COLUMN_DETECTION:
        return False
    if any(b.x0 < mid - margin and b.x1 > mid + margin for b in narrow):
        return False
    return any(b.x1 <= mid for b in narrow) and any(b.x0 >= mid for b in narrow)


def _order_blocks(blocks: list[_Block], page_width: float) -> list[_Block]:
    """Return blocks in human reading order."""
    if not _is_two_column(blocks, page_width):
        return sorted(blocks, key=lambda b: (_row_band(b.y0), b.x0))
    mid = page_width / 2.0
    return sorted(blocks, key=lambda b: (0 if b.centre < mid else 1, _row_band(b.y0), b.x0))


def _page_lines(page: fitz.Page, page_no: int, diagnostics: ParseDiagnostics) -> list[_Line]:
    blocks = _extract_blocks(page, page_no, diagnostics)
    return [line for block in _order_blocks(blocks, page.rect.width) for line in block.lines]


def _is_contents_page(lines: list[_Line], body_size: float) -> bool:
    return any(CONTENTS_HEADING_RE.match(ln.text) and ln.size >= body_size for ln in lines)


def _measure_layout(all_lines: list[_Line]) -> _Layout:
    """Measure the document's body font size and text-column width."""
    weight: Counter[float] = Counter()
    for line in all_lines:
        weight[round(line.size, 1)] += len(line.text)
    if not weight:
        raise ParseError("document contains no extractable text")
    body_size = weight.most_common(1)[0][0]

    widths = sorted(ln.x1 - ln.x0 for ln in all_lines if round(ln.size, 1) == body_size)
    if not widths:
        raise ParseError("document contains no body-size lines")
    index = min(len(widths) - 1, int(len(widths) * BODY_WIDTH_PERCENTILE))
    return _Layout(body_size=body_size, body_line_width=widths[index])


# ─────────────────────────────────────────────────────────────────────────────
# Contents pages -> independent ground truth
# ─────────────────────────────────────────────────────────────────────────────


def _parse_contents_page(lines: list[_Line], page_width: float) -> list[ContentsEntry]:
    """Read a table-of-contents page into ``(article_no, title)`` rows.

    A contents row reads ``Article (N) | Title | page``, but the PDF does not emit it as
    one block: the labels are one stacked block, the titles another, the page numbers a
    third. Rows are therefore reassembled geometrically — lines sharing a baseline
    within :data:`ROW_TOLERANCE_PT` form a row, and within a row the label is the entry,
    the trailing number is the page, and whatever lies between them is the title.
    """
    mid = page_width / 2.0
    columns: list[list[_Line]] = [[], []] if _is_two_column_lines(lines, page_width) else [[]]
    two_columns = len(columns) == TWO_COLUMNS
    for line in lines:
        columns[1 if two_columns and line.centre >= mid else 0].append(line)

    entries: list[ContentsEntry] = []
    for column in columns:
        for row in _group_rows(column):
            entries.extend(_row_to_entries(row))
    return entries


def _is_two_column_lines(lines: list[_Line], page_width: float) -> bool:
    mid = page_width / 2.0
    margin = page_width * GUTTER_RATIO
    if any(ln.x0 < mid - margin and ln.x1 > mid + margin for ln in lines):
        return False
    return any(ln.x1 <= mid for ln in lines) and any(ln.x0 >= mid for ln in lines)


def _group_rows(lines: list[_Line]) -> list[list[_Line]]:
    """Cluster lines that share a baseline into rows, each sorted left to right."""
    rows: list[list[_Line]] = []
    for line in sorted(lines, key=lambda ln: (ln.page, ln.y0, ln.x0)):
        if rows and abs(rows[-1][0].y0 - line.y0) <= ROW_TOLERANCE_PT:
            rows[-1].append(line)
        else:
            rows.append([line])
    for row in rows:
        row.sort(key=lambda ln: ln.x0)
    return rows


def _row_to_entries(row: list[_Line]) -> list[ContentsEntry]:
    """Turn one contents row into its article entries, expanding any span."""
    label_index = next(
        (i for i, ln in enumerate(row) if CONTENTS_ENTRY_RE.match(ln.text)),
        None,
    )
    if label_index is None:
        return []
    match = CONTENTS_ENTRY_RE.match(row[label_index].text)
    if match is None:  # pragma: no cover - guarded by the search above
        return []
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else first
    if last < first or last - first > MAX_CONTENTS_SPAN:
        return []

    title_parts = [
        ln.text.strip().lstrip(TITLE_MARKER_CHARS).strip()
        for ln in row[label_index + 1 :]
        if not PAGE_FURNITURE_RE.match(ln.text.strip())
        and not CONTENTS_ENTRY_RE.match(ln.text.strip())
    ]
    title = _normalise(" ".join(part for part in title_parts if part))
    return [ContentsEntry(article_no=n, title=title) for n in range(first, last + 1)]


def reconcile_with_contents(
    articles: list[Article],
    contents_by_part: list[list[ContentsEntry]],
    diagnostics: ParseDiagnostics,
    law_id: str,
) -> list[Article]:
    """Cross-check and enrich parsed articles using the document's own contents pages.

    The k-th contents page in a file describes the k-th instrument in it, which makes it
    an index produced by the publisher rather than by this parser — genuinely
    independent evidence. It is used twice:

    * **Validation.** Any article the contents promises but the body pass did not
      produce, or vice versa, is recorded by name. A layout change then surfaces as a
      named discrepancy instead of a quietly smaller index.
    * **Titles.** The contents carries the official title of every article, already
      de-hyphenated and never truncated, so it supersedes the title inferred from page
      geometry (which can clip a title that wraps onto a second line).
    """
    if not contents_by_part:
        return articles

    order: list[str] = []
    by_part: dict[str, list[Article]] = {}
    for article in articles:
        if article.part_id not in by_part:
            order.append(article.part_id)
            by_part[article.part_id] = []
        by_part[article.part_id].append(article)

    titles: dict[tuple[str, int], str] = {}
    for index, part_id in enumerate(order):
        if index >= len(contents_by_part):
            break
        entries = contents_by_part[index]
        expected = {entry.article_no for entry in entries}
        diagnostics.contents_entries_seen += len(expected)
        produced = {article.article_no for article in by_part[part_id]}
        for missing in sorted(expected - produced):
            diagnostics.contents_missing_in_body.append(f"{law_id}/{part_id}#art{missing}")
        for extra in sorted(produced - expected):
            diagnostics.contents_extra_in_body.append(f"{law_id}/{part_id}#art{extra}")
        for entry in entries:
            if entry.title:
                titles[(part_id, entry.article_no)] = entry.title

    enriched: list[Article] = []
    for article in articles:
        official = titles.get((article.part_id, article.article_no))
        if official and official != article.title:
            diagnostics.titles_from_contents += 1
            enriched.append(article.model_copy(update={"title": official}))
        else:
            enriched.append(article)
    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# Body pages -> articles
# ─────────────────────────────────────────────────────────────────────────────


def _looks_like_title(line: _Line, layout: _Layout, page_width: float) -> bool:
    """A short, heading-ish line that names the article beside it.

    The geometric width test is load-bearing, not cosmetic. Body text in the Dubai laws
    is fully justified, so the first line of an article spans the whole text column and
    its midpoint lands exactly on the page centre — indistinguishable from a centred
    title by position alone. Without the width test, Decree 43/2013 Article 1 loses its
    opening line ("When renewing Real Property Lease Contracts...") to the title field:
    silent corpus corruption that no downstream check would catch.

    A real title is therefore recognised by a *positive* typographic signal — a bullet
    marker, bold weight, or a larger font — and the centre-alignment fallback (needed
    only by Decree 43/2013, which sets titles in the same light face as its body) is
    additionally required to be narrow.
    """
    text = line.text.strip().lstrip(TITLE_MARKER_CHARS).strip()
    if not text or len(text) > MAX_TITLE_CHARS:
        return False
    if text.endswith((".", ";", ":", ",")):
        return False
    if ARTICLE_HEADING_RE.match(text) or PAGE_FURNITURE_RE.match(text):
        return False
    if not _has_letters_or_digits(text):
        return False

    width = line.x1 - line.x0
    if width >= layout.body_line_width * JUSTIFIED_LINE_RATIO:
        return False

    if line.bold or line.size > layout.body_size:
        return True
    if line.text.startswith(tuple(TITLE_MARKER_CHARS)):
        return True
    centred = abs(line.centre - page_width / 2.0) < page_width * CENTRED_TOLERANCE_RATIO
    return centred and width < layout.body_line_width * CENTRED_TITLE_WIDTH_RATIO


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "part"


@dataclass(slots=True)
class _ArticleAccumulator:
    part_id: str
    part_title: str
    article_no: int
    title: str
    page_start: int
    page_end: int
    body: list[str] = field(default_factory=list)

    def add(self, line: _Line) -> None:
        self.body.append(line.text)
        self.page_end = max(self.page_end, line.page)

    def text(self) -> str:
        return re.sub(r"\s{2,}", " ", " ".join(self.body)).strip()


@dataclass(slots=True)
class _Segmenter:
    """Walks the ordered line stream and emits articles.

    Kept as a class purely so the state machine's variables are named and inspectable
    rather than a dozen ``nonlocal`` bindings.
    """

    law_id: str
    law_label: str
    law_title: str
    layout: _Layout
    diagnostics: ParseDiagnostics

    articles: list[Article] = field(default_factory=list)
    current: _ArticleAccumulator | None = None
    pending_title: str = ""
    pending_key: InstrumentKey | None = None
    open_part_key: InstrumentKey | None = None
    open_part_id: str = ""
    open_part_title: str = ""
    part_ordinal: int = 0
    seen_in_part: set[int] = field(default_factory=set)
    unassigned_chars: int = 0
    part_opened: bool = False

    def __post_init__(self) -> None:
        self.pending_title = self.law_title
        self.pending_key = _instrument_key(self.law_title)

    # ── article lifecycle ────────────────────────────────────────────────
    def close(self) -> None:
        if self.current is None:
            return
        body = self.current.text()
        if body:
            self.articles.append(
                Article(
                    law_id=self.law_id,
                    law_label=self.law_label,
                    law_title=self.law_title,
                    part_id=self.current.part_id,
                    part_title=self.current.part_title,
                    article_no=self.current.article_no,
                    title=self.current.title,
                    text=body,
                    page_start=self.current.page_start,
                    page_end=self.current.page_end,
                )
            )
        self.current = None

    def _open_part_if_needed(self, article_no: int) -> None:
        same_instrument = self.part_opened and self.open_part_key == self.pending_key
        duplicate = article_no in self.seen_in_part
        if same_instrument and not duplicate:
            return
        if same_instrument and duplicate:
            # A repeated number inside one instrument means a new structural section
            # began: the Dubai amendment restates articles of the law it amends, then
            # closes with its own Article 2. Splitting keeps every citation key unique,
            # so a chip can never resolve to two different texts.
            self.diagnostics.duplicate_article_splits += 1
        self.part_ordinal += 1
        self.open_part_key = self.pending_key
        self.open_part_title = self.pending_title
        self.open_part_id = f"{_slugify(self.pending_title)}-{self.part_ordinal}"
        self.seen_in_part = set()
        self.part_opened = True
        self.diagnostics.parts_detected += 1

    def start_article(self, article_no: int, title: str, page: int) -> None:
        self.close()
        self._open_part_if_needed(article_no)
        self.seen_in_part.add(article_no)
        self.current = _ArticleAccumulator(
            part_id=self.open_part_id,
            part_title=self.open_part_title,
            article_no=article_no,
            title=title,
            page_start=page,
            page_end=page,
        )

    # ── stream handling ──────────────────────────────────────────────────
    def on_instrument_title(self, text: str) -> None:
        key = _instrument_key(text)
        if key is None or key == self.pending_key:
            return
        # A different instrument begins here; whatever article was open ended with the
        # previous one. Without this the Cabinet Resolution's recitals are appended to
        # Labour Law Article 74.
        self.close()
        self.pending_key = key
        self.pending_title = text

    def attach_punctuation(self, mark: str) -> None:
        """Glue a standalone separator onto the text it belongs to."""
        if self.current is not None and self.current.body:
            self.current.body[-1] = f"{self.current.body[-1]}{mark}"

    def on_body(self, line: _Line) -> None:
        if self.current is None:
            self.unassigned_chars += len(line.text)
            return
        self.current.add(line)


def parse_pdf(
    pdf_path: Path,
    *,
    law_id: str,
    law_label: str,
    law_title: str,
    diagnostics: ParseDiagnostics | None = None,
) -> ParsedDocument:
    """Parse one corpus PDF into its numbered articles.

    Raises:
        ParseError: if the file yields no articles, which means the layout changed and
            the extraction rules no longer describe it. Failing loudly here is
            deliberate: a silently empty index makes every downstream metric meaningless.
    """
    diag = diagnostics if diagnostics is not None else ParseDiagnostics()
    document = fitz.open(pdf_path)
    try:
        pages = [
            (index + 1, document[index].rect.width, _page_lines(document[index], index + 1, diag))
            for index in range(document.page_count)
        ]
        page_count = document.page_count
    finally:
        document.close()

    layout = _measure_layout([ln for _, _, lines in pages for ln in lines])

    contents_by_part: list[list[ContentsEntry]] = []
    segmenter = _Segmenter(
        law_id=law_id,
        law_label=law_label,
        law_title=law_title,
        layout=layout,
        diagnostics=diag,
    )

    for page_no, page_width, lines in pages:
        if _is_contents_page(lines, layout.body_size):
            diag.contents_pages_skipped += 1
            contents_by_part.append(_parse_contents_page(lines, page_width))
            continue

        for position, line in enumerate(lines):
            stripped = line.text.strip()

            if line.size < layout.furniture_ceiling or PAGE_FURNITURE_RE.match(stripped):
                diag.furniture_lines_dropped += 1
                continue
            if PUNCTUATION_JOINER_RE.match(stripped):
                segmenter.attach_punctuation(stripped)
                continue
            if not _has_letters_or_digits(stripped):
                diag.furniture_lines_dropped += 1
                continue

            heading = ARTICLE_HEADING_RE.match(stripped)
            if heading is not None:
                segmenter.start_article(
                    int(heading.group(1)),
                    _find_article_title(lines, position, layout, page_width),
                    page_no,
                )
                continue

            if INSTRUMENT_TITLE_RE.match(stripped) and line.size >= layout.body_size:
                segmenter.on_instrument_title(stripped)
                continue

            if _is_consumed_title(lines, position, segmenter.current):
                continue

            inferred = _infer_missing_heading(lines, position, segmenter, layout, page_width)
            if inferred is not None:
                diag.inferred_headings += 1
                segmenter.start_article(inferred[0], inferred[1], page_no)
                continue

            segmenter.on_body(line)

    segmenter.close()

    if not segmenter.articles:
        raise ParseError(
            f"{pdf_path.name}: no articles were extracted. The document layout no longer "
            "matches the extraction rules in app/rag/parse.py."
        )

    articles = _propagate_sections(
        reconcile_with_contents(segmenter.articles, contents_by_part, diag, law_id)
    )

    return ParsedDocument(
        law_id=law_id,
        law_label=law_label,
        law_title=law_title,
        filename=pdf_path.name,
        page_count=page_count,
        articles=tuple(articles),
        unassigned_chars=segmenter.unassigned_chars,
    )


def _propagate_sections(articles: list[Article]) -> list[Article]:
    """Give every article the nearest preceding heading inside its own instrument.

    Dubai Law 26/2007 prints chapter headings ("Term of Lease Contract", "Eviction
    Cases") above the first article of each chapter only, so Articles 6, 7 and 8 carry
    no heading of their own while plainly belonging to one. Carrying the chapter forward
    gives those articles topical context in the embedded text, which is precisely the
    signal a bare "Where the term of a Lease Contract expires..." otherwise lacks.

    The carry resets at every part boundary so a chapter can never leak from one
    instrument into another.
    """
    out: list[Article] = []
    current_part = ""
    section = ""
    for article in articles:
        if article.part_id != current_part:
            current_part = article.part_id
            section = ""
        if article.title:
            section = article.title
        out.append(article.model_copy(update={"section": section}))
    return out


def _find_article_title(
    lines: list[_Line], position: int, layout: _Layout, page_width: float
) -> str:
    """Locate the human title beside an article heading.

    The corpus uses both conventions: the Labour Law emits ``Article (6)`` and
    ``> Recruitment and Employment of Workers`` inside one block in either order, while
    the Dubai laws centre the title on the line above the number.
    """
    for neighbour in (position + 1, position - 1):
        if not 0 <= neighbour < len(lines):
            continue
        candidate = lines[neighbour]
        if candidate.size < layout.furniture_ceiling:
            continue
        if _looks_like_title(candidate, layout, page_width):
            return candidate.text.strip().lstrip(TITLE_MARKER_CHARS).strip()
    return ""


def _is_consumed_title(
    lines: list[_Line], position: int, current: _ArticleAccumulator | None
) -> bool:
    """True when this line was already taken as the current article's title."""
    if current is None or not current.title or current.body:
        return False
    text = lines[position].text.strip().lstrip(TITLE_MARKER_CHARS).strip()
    return text == current.title


def _infer_missing_heading(
    lines: list[_Line],
    position: int,
    segmenter: _Segmenter,
    layout: _Layout,
    page_width: float,
) -> tuple[int, str] | None:
    """Recover an article whose ``Article (N)`` label is absent from the source PDF.

    Cabinet Resolution No. 1 of 2022 prints the section title of its Article 2 but not
    the label, while its own table of contents lists the article. A bare section-title
    marker appearing mid-body, not attached to any label, is therefore treated as the
    start of the next article — but only when that number is unused and the next
    explicit label in the stream is further ahead, so a title that simply precedes its
    own label is never mistaken for a missing one.
    """
    line = lines[position]
    if segmenter.current is None or not segmenter.current.body:
        return None
    if not line.text.startswith(tuple(TITLE_MARKER_CHARS)):
        return None
    if not _looks_like_title(line, layout, page_width):
        return None

    candidate_no = segmenter.current.article_no + 1
    if candidate_no in segmenter.seen_in_part:
        return None

    for following in lines[position + 1 :]:
        heading = ARTICLE_HEADING_RE.match(following.text.strip())
        if heading is None:
            continue
        # The very next label is the one this title belongs to, or it re-uses the
        # number we would have inferred. Either way, do not invent an article.
        return None if int(heading.group(1)) <= candidate_no else _titled(candidate_no, line)
    return _titled(candidate_no, line)


def _titled(article_no: int, line: _Line) -> tuple[int, str]:
    return article_no, line.text.strip().lstrip(TITLE_MARKER_CHARS).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Corpus-level entry point
# ─────────────────────────────────────────────────────────────────────────────


def load_manifest(settings: Settings | None = None) -> list[dict[str, Any]]:
    cfg = settings or get_settings()
    if not cfg.manifest_path.exists():
        raise ParseError(
            f"{cfg.manifest_path} not found. Run `make corpus` first — it downloads the "
            "official law PDFs and writes the provenance manifest."
        )
    payload: dict[str, Any] = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    documents: list[dict[str, Any]] = payload["documents"]
    return documents


def parse_corpus(
    settings: Settings | None = None,
    diagnostics: ParseDiagnostics | None = None,
) -> list[ParsedDocument]:
    """Parse every document named in the corpus manifest."""
    cfg = settings or get_settings()
    diag = diagnostics if diagnostics is not None else ParseDiagnostics()
    parsed: list[ParsedDocument] = []
    for entry in load_manifest(cfg):
        pdf_path = cfg.pdf_dir / entry["filename"]
        if not pdf_path.exists():
            raise ParseError(f"{pdf_path} is missing. Run `make corpus`.")
        document = parse_pdf(
            pdf_path,
            law_id=entry["law_id"],
            law_label=entry["label"],
            law_title=entry["title"],
            diagnostics=diag,
        )
        logger.info(
            "parsed %s: %d articles across %d pages",
            document.law_id,
            len(document.articles),
            document.page_count,
        )
        parsed.append(document)
    return parsed
