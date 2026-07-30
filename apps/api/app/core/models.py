"""Domain model shared by both pipelines, the HTTP layer and the eval harness.

One typed vocabulary end to end: the parser emits `Article`s, the chunker turns them into
`Chunk`s, retrieval decorates them into `ScoredChunk`s, generation produces an `Answer`,
and the verifier annotates its `Citation`s. Nothing downstream re-parses a dict.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Frozen(BaseModel):
    """Immutable, extra-rejecting base. Typos become errors, not silent no-ops."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ─────────────────────────────────────────────────────────────────────────────
# Corpus / parsing
# ─────────────────────────────────────────────────────────────────────────────


class Article(Frozen):
    """One numbered article of one legal instrument, as parsed from the PDF."""

    law_id: str
    law_label: str
    law_title: str
    part_id: str = Field(
        description=(
            "Instrument within the PDF. Some official files bundle more than one "
            "instrument (the Labour Law PDF also carries Cabinet Resolution 1/2022), and "
            "their article numbering restarts, so 'Article 5' is ambiguous without this."
        )
    )
    part_title: str
    article_no: int = Field(ge=1)
    title: str = Field(
        description=(
            "The heading printed beside this article's label, or empty when the source "
            "prints none. In the Labour Law that is a per-article title; in the Dubai "
            "laws it is a chapter heading that also governs the articles below it."
        )
    )
    section: str = Field(
        default="",
        description=(
            "Nearest preceding heading within the same instrument, inclusive of this "
            "article's own. Gives an untitled article (Dubai Law 26/2007 Articles 6-8) "
            "the topical context of its chapter, which is embedded with the chunk."
        ),
    )
    text: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _pages_ordered(self) -> Self:
        if self.page_end < self.page_start:
            raise ValueError(f"page_end {self.page_end} precedes page_start {self.page_start}")
        return self

    @property
    def citation_key(self) -> str:
        """Stable identity of the *article* a citation points at."""
        return f"{self.law_id}#{self.part_id}#art{self.article_no}"

    @property
    def display_citation(self) -> str:
        """What the model is instructed to write inline, e.g. ``[Labour Law, Article 51]``."""
        return f"[{self.law_label}, Article {self.article_no}]"


class ParsedDocument(Frozen):
    """Everything extracted from one corpus PDF."""

    law_id: str
    law_label: str
    law_title: str
    filename: str
    page_count: int = Field(ge=1)
    articles: tuple[Article, ...]
    # Text that belongs to the instrument but sits outside any numbered article
    # (preamble, recitals, signature block). Kept for transparency, not indexed.
    unassigned_chars: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Chunking / indexing
# ─────────────────────────────────────────────────────────────────────────────


class Chunk(Frozen):
    """An indexed passage. Carries its article provenance forever."""

    chunk_id: str
    law_id: str
    law_label: str
    law_title: str
    part_id: str
    part_title: str
    article_no: int = Field(ge=1)
    article_title: str
    section: str = ""
    # Position of this chunk within its article (0-based) and how many there are.
    seq: int = Field(ge=0)
    seq_total: int = Field(ge=1)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    text: str
    token_count: int = Field(ge=1)
    # What `article_no` counts. Legislation has articles; an uploaded contract has
    # sections, and calling them articles would be a small lie repeated on every citation
    # chip. Defaulted so the committed corpus deserialises unchanged and `make_id` — which
    # never saw this field — keeps producing the same ids.
    unit_label: str = "Article"

    @property
    def citation_key(self) -> str:
        return f"{self.law_id}#{self.part_id}#art{self.article_no}"

    @property
    def display_citation(self) -> str:
        return f"[{self.law_label}, {self.unit_label} {self.article_no}]"

    @property
    def heading(self) -> str:
        base = f"{self.law_label} — {self.unit_label} {self.article_no}"
        if self.article_title:
            base = f"{base}: {self.article_title}"
        if self.seq_total > 1:
            base = f"{base} ({self.seq + 1}/{self.seq_total})"
        return base

    @staticmethod
    def make_id(law_id: str, part_id: str, article_no: int, seq: int, text: str) -> str:
        """Content-addressed id: re-indexing identical text yields identical ids."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        return f"{law_id}:{part_id}:a{article_no}:{seq}:{digest}"


class RetrievalSource(StrEnum):
    DENSE = "dense"
    SPARSE = "sparse"
    BOTH = "both"


class ScoredChunk(Frozen):
    """A chunk with every score that decided its fate, for the Evidence Panel.

    Keeping the whole score trail (not just the final one) is what lets the UI show
    *why* a passage was chosen, and lets the eval harness attribute a win to fusion
    versus reranking.
    """

    chunk: Chunk
    source: RetrievalSource
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    fused_rank: int | None = None
    rerank_score: float | None = None
    final_rank: int | None = None

    @property
    def rerank_probability(self) -> float | None:
        """Cross-encoder logit squashed to 0-1 purely for display."""
        if self.rerank_score is None:
            return None
        import math

        return 1.0 / (1.0 + math.exp(-self.rerank_score))


# ─────────────────────────────────────────────────────────────────────────────
# Query gate
# ─────────────────────────────────────────────────────────────────────────────


class GateDecision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class GateResult(Frozen):
    """Output of the Haiku query gate."""

    decision: GateDecision
    search_query: str
    original_query: str
    rewritten: bool
    reason: str = ""
    signals: tuple[str, ...] = ()
    engine: Literal["anthropic", "deterministic"] = "deterministic"
    latency_ms: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Generation / verification
# ─────────────────────────────────────────────────────────────────────────────

# Matches the inline form the system prompt mandates: [Labour Law, Article 51].
# Tolerates "Art." / "Art" / "Article", an optional "(n)", and stray inner spacing,
# because a verifier that only recognises the perfectly-formatted case is a verifier
# that silently passes malformed citations.
#
# The unit is an alternation rather than the literal "Article" because an uploaded
# document has sections or pages, not articles (see `Chunk.unit_label`). Matching only
# "Article" did not fail loudly on a workspace citation — it matched nothing, so every
# citation was dropped and the answer rendered as though the model had cited nothing.
# Longest alternatives come first: "Sec" before "Section" would match the prefix and
# leave "tion" to break the rest of the pattern.
#
# Digits and underscores are permitted in the label because the label is a filename for
# an uploaded document, and `contract_2024.pdf` is an ordinary name.
CITATION_RE = re.compile(
    r"\[\s*(?P<law>[A-Za-z0-9][A-Za-z0-9 ._'\-]{1,60}?)\s*,\s*"
    r"(?P<unit>Article|Art\.?|Section|Sec\.?|Clause|Paragraph|Para\.?|Page)"
    r"\s*\(?\s*(?P<article>\d{1,4})\s*\)?\s*\]",
    re.IGNORECASE,
)


class CitationStatus(StrEnum):
    VERIFIED = "verified"
    """Cited article is present in the set of chunks that were shown to the model."""

    UNSUPPORTED = "unsupported"
    """Model cited an article that was not in its context — a fabricated reference."""


class Citation(Frozen):
    """One inline citation found in the generated answer, after verification."""

    raw: str
    law_label: str
    article_no: int = Field(ge=1)
    status: CitationStatus
    chunk_id: str | None = None
    citation_key: str | None = None
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class VerificationReport(Frozen):
    """The citation verifier's verdict on a generated answer."""

    citations: tuple[Citation, ...]
    unsupported_count: int = Field(ge=0)
    uncited_sentences: tuple[str, ...] = ()
    passed: bool

    @property
    def verified_count(self) -> int:
        return sum(1 for c in self.citations if c.status is CitationStatus.VERIFIED)


class AnswerKind(StrEnum):
    ANSWER = "answer"
    REFUSAL = "refusal"
    BLOCKED = "blocked"


class Answer(Frozen):
    """A completed, verified response."""

    kind: AnswerKind
    text: str
    citations: tuple[Citation, ...] = ()
    verification: VerificationReport | None = None
    evidence: tuple[ScoredChunk, ...] = ()
    near_misses: tuple[ScoredChunk, ...] = ()
    gate: GateResult | None = None
    engine: Literal["anthropic", "offline-extractive"] = "offline-extractive"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP surface
# ─────────────────────────────────────────────────────────────────────────────

Role = Literal["user", "assistant"]


class Turn(Frozen):
    role: Role
    content: Annotated[str, Field(min_length=1, max_length=4000)]


class AskRequest(Frozen):
    question: Annotated[str, Field(min_length=1, max_length=1000)]
    history: tuple[Turn, ...] = ()
    law_id: str | None = Field(
        default=None,
        description="Restrict retrieval to a single instrument via a Qdrant payload filter.",
    )
    rerank: bool = True

    @model_validator(mode="after")
    def _question_is_not_blank(self) -> Self:
        if not self.question.strip():
            raise ValueError("question must contain at least one non-whitespace character")
        return self


class TimingBreakdown(Frozen):
    gate_ms: float = 0.0
    retrieval_ms: float = 0.0
    rerank_ms: float = 0.0
    generation_ms: float = 0.0
    verify_ms: float = 0.0
    total_ms: float = 0.0


class Usage(Frozen):
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
