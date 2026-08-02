"""Chunk an arbitrary document into the same :class:`Chunk` the law corpus produces.

`rag/chunk.py` splits *articles*, because the corpus is legislation and an article is a
real semantic unit with a number the reader can look up. An uploaded contract has no
articles. It has headings, or it has paragraphs, or it has neither.

So this finds the best structure available and degrades honestly:

    markdown / DOCX headings   sections named by their heading
    numbered clauses           sections named by their number
    neither                    page-sized sections, labelled "Page N"

The unit is recorded on the chunk (``unit_label``) so a citation says "Section 4" or
"Page 2" rather than inventing an article number. Producing the same `Chunk` type is the
point: retrieval, reranking, the refusal gate, citation verification and the whole
frontend are then shared with the corpus rather than reimplemented for uploads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from tokenizers import Tokenizer

from app.core.models import Chunk
from app.core.settings import Settings, get_settings
from app.core.tokenizer import get_tokenizer
from app.workspace.extract import ExtractedDocument

# `## Heading`, `1.2 Heading`, `Article 4 —`, `SECTION 3:` and bare ALL-CAPS lines: the
# five ways documents in this space actually mark a section. Anchored and length-capped
# so a long sentence that happens to start with a number is not mistaken for a heading.
HEADING_PATTERNS: Final = (
    re.compile(r"^(#{1,6})\s+(?P<title>.{1,120})$"),
    re.compile(r"^(?P<num>\d+(?:\.\d+)*)[.)]?\s+(?P<title>[A-Z][^.!?]{2,120})$"),
    re.compile(
        r"^(?:ARTICLE|Article|SECTION|Section|CLAUSE|Clause)\s+(?P<num>[\dIVXLC]+)"
        r"\s*[-—:.]?\s*(?P<title>.{0,120})$"
    ),
    re.compile(r"^(?P<title>[A-Z][A-Z \d&',()/-]{3,80})$"),
)

MIN_SECTION_CHARS: Final = 40
# A heading is short. Beyond this it is a sentence that happens to start like one.
MAX_HEADING_CHARS: Final = 140


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    text: str
    page_start: int
    page_end: int
    #: The number as printed in the document ("Article 24" -> 24), when the heading
    #: carries one. Kept separate from the reading-order index because they diverge:
    #: the UDHR prints 30 articles but yields 36+ detected headings, so section #35
    #: holds the text of Article 24. Citing the index told the reader to look up an
    #: article that does not exist, and the verifier -- matching on the number -- could
    #: not resolve the citation the model had taken, correctly, from the passage text.
    number: int | None = None


def _heading_number(raw: str | None) -> int | None:
    """The leading integer of a heading number, or None.

    Only a plain integer counts. "1.2" is a subsection of 1 and numbering it 1 would
    collide with the parent, so nested numbering keeps the reading-order index instead;
    Roman numerals are left alone for the same reason.
    """
    if not raw or "." in raw:
        return None
    return int(raw) if raw.isdigit() else None


def _match_heading(line: str) -> tuple[str, int | None] | None:
    """Return ``(label, printed number)`` for a heading line, or None."""
    stripped = line.strip()
    if not stripped or len(stripped) > MAX_HEADING_CHARS:
        return None
    for pattern in HEADING_PATTERNS:
        match = pattern.match(stripped)
        if match:
            title = (match.groupdict().get("title") or "").strip(" -—:.")
            number = match.groupdict().get("num")
            printed = _heading_number(number)
            if number and title:
                return f"{number} {title}", printed
            return (title or (f"Clause {number}" if number else stripped)), printed
    return None


def split_sections(document: ExtractedDocument) -> list[Section]:
    """Find sections. Falls back to one section per page when there is no structure."""
    sections: list[Section] = []
    title: str | None = None
    number: int | None = None
    buffer: list[str] = []
    start_page = document.pages[0].page_no if document.pages else 1
    end_page = start_page

    def flush() -> None:
        nonlocal title, number, buffer
        body = "\n".join(buffer).strip()
        if not body:
            title, number, buffer = None, None, []
            return
        if len(body) >= MIN_SECTION_CHARS or not sections:
            # Short, but there is nothing to merge into. Keeping it as its own section is
            # the only way it survives: an earlier version dropped it here, which silently
            # lost the opening lines of any document that began with a short paragraph.
            sections.append(Section(title or "", body, start_page, end_page, number))
        else:
            # Too short to stand alone — a stray heading or a page footer. Append it to
            # the previous section rather than emitting a chunk that carries no meaning.
            previous = sections[-1]
            sections[-1] = Section(
                previous.title,
                f"{previous.text}\n{body}",
                previous.page_start,
                max(previous.page_end, end_page),
                previous.number,
            )
        title, number, buffer = None, None, []

    for page in document.pages:
        end_page = page.page_no
        for line in page.text.splitlines():
            matched = _match_heading(line)
            if matched is not None:
                flush()
                start_page = page.page_no
                title, number = matched
                # The raw line goes into the body as well. `_match_heading` returns a
                # cleaned label — markers stripped, numbering reordered — so using it
                # alone would drop the original wording from the searchable text. That is
                # a heading per document lost, and headings carry the terms people search
                # for. Keeping both costs one repeated line and makes coverage exact.
                buffer.append(line.strip())
            else:
                buffer.append(line)
    flush()

    if sections:
        return sections
    # No structure at all: one section per page keeps citations pointing somewhere real.
    return [
        Section("", page.text.strip(), page.page_no, page.page_no)
        for page in document.pages
        if page.text.strip()
    ]


def _count(text: str, tokenizer: Tokenizer) -> int:
    return len(tokenizer.encode(text, add_special_tokens=False).ids)


def _pack(paragraphs: list[str], budget: int, tokenizer: Tokenizer) -> list[str]:
    """Greedily fill chunks to the token budget, splitting any paragraph that exceeds it."""
    packed: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for paragraph in paragraphs:
        tokens = _count(paragraph, tokenizer)
        if tokens > budget:
            if current:
                packed.append("\n\n".join(current))
                current, current_tokens = [], 0
            words = paragraph.split()
            piece: list[str] = []
            for word in words:
                piece.append(word)
                if _count(" ".join(piece), tokenizer) >= budget:
                    packed.append(" ".join(piece))
                    piece = []
            if piece:
                current, current_tokens = [" ".join(piece)], _count(" ".join(piece), tokenizer)
            continue
        if current_tokens + tokens > budget and current:
            packed.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(paragraph)
        current_tokens += tokens

    if current:
        packed.append("\n\n".join(current))
    return packed


def coverage(document: ExtractedDocument, chunks: list[Chunk]) -> float:
    """Fraction of the document's words that survive into the chunks.

    Chunking is the one stage that can lose text silently: a paragraph dropped here does
    not raise, it simply never becomes searchable, and the only symptom is a question that
    should have been answerable coming back refused. Nothing downstream can detect that,
    because nothing downstream ever saw the missing words.

    Compared as a word multiset, not as a string. Chunk text carries an added heading line
    and overlapping pieces repeat words, so an equality check would fail on a correct
    result; what matters is that every word of the source appears at least as often in the
    chunks as it did in the document.
    """
    from collections import Counter

    source = Counter(document.text.split())
    if not source:
        return 1.0
    produced = Counter(word for chunk in chunks for word in chunk.text.split())
    kept = sum(min(count, produced.get(word, 0)) for word, count in source.items())
    return kept / sum(source.values())


def _assign_numbers(sections: list[Section]) -> list[int]:
    """One citable number per section: the printed one wherever it is trustworthy.

    A citation is only useful if the reader can find what it points at, so a section
    headed "Article 24" must cite as 24 even when it is the 35th heading detected. Two
    things stop this being a straight substitution:

    * a printed number can repeat (two "Article 1"s across an appendix, a restated
      clause), and a duplicate would let a citation resolve to the wrong passage;
    * most documents number only some of their headings, so the unnumbered ones still
      need a number, and it must not collide with a printed one.

    So: keep printed numbers that appear exactly once, and number everything else from
    above the highest printed number upward. Collisions are impossible by construction.
    """
    from collections import Counter

    counts = Counter(section.number for section in sections if section.number is not None)
    unique = {number for number, seen in counts.items() if seen == 1}
    next_free = max(unique, default=0) + 1

    resolved: list[int] = []
    for section in sections:
        if section.number is not None and section.number in unique:
            resolved.append(section.number)
        else:
            resolved.append(next_free)
            next_free += 1
    return resolved


def chunk_document(
    document: ExtractedDocument,
    doc_id: str,
    settings: Settings | None = None,
) -> list[Chunk]:
    """Chunk one uploaded document. Section numbers are 1-based and stable per document."""
    cfg = settings or get_settings()
    tokenizer = get_tokenizer(cfg.embedding_model)
    budget = cfg.effective_chunk_max_tokens

    sections = split_sections(document)
    unit = "Section" if any(section.title for section in sections) else "Page"
    label = document.title
    numbers = _assign_numbers(sections)

    chunks: list[Chunk] = []
    for index, section in enumerate(sections, start=1):
        number = numbers[index - 1]
        paragraphs = [block.strip() for block in section.text.split("\n\n") if block.strip()]
        if not paragraphs:
            continue
        pieces = _pack(paragraphs, budget, tokenizer)
        for seq, piece in enumerate(pieces):
            token_count = _count(piece, tokenizer)
            if token_count < 1:
                continue
            heading = section.title or f"{unit} {number}"
            body = f"{label} — {heading}\n{piece}"
            chunks.append(
                Chunk(
                    chunk_id=Chunk.make_id(doc_id, "doc", number, seq, piece),
                    law_id=doc_id,
                    law_label=label,
                    law_title=document.title,
                    part_id="doc",
                    part_title=document.source,
                    article_no=number,
                    article_title=section.title,
                    seq=seq,
                    seq_total=len(pieces),
                    page_start=section.page_start,
                    page_end=section.page_end,
                    text=body,
                    token_count=_count(body, tokenizer) or token_count,
                    unit_label=unit,
                )
            )
    return chunks
