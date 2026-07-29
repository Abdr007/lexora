"""Wire format for the HTTP API.

Deliberately separate from the domain model in ``app.core.models``. The domain types are
free to change shape as the pipeline evolves; these are a contract with a deployed
frontend. Keeping them apart means a refactor cannot silently break the UI, and the
serialiser below is the single place where the mapping lives.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.models import (
    Answer,
    Citation,
    ScoredChunk,
    TimingBreakdown,
    Usage,
)
from app.rag.pipeline import PipelineResult


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChunkView(Wire):
    """A retrieved passage as the Evidence Panel needs it."""

    chunk_id: str
    law_id: str
    law_label: str
    law_title: str
    part_title: str
    article_no: int
    article_title: str
    section: str
    seq: int
    seq_total: int
    page_start: int
    page_end: int
    text: str
    heading: str
    citation_key: str

    # The full score trail, so the UI can show *why* this passage was selected.
    source: str
    dense_rank: int | None = None
    dense_score: float | None = None
    sparse_rank: int | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    fused_rank: int | None = None
    rerank_score: float | None = None
    rerank_probability: float | None = None
    final_rank: int | None = None


class CitationView(Wire):
    raw: str
    law_label: str
    article_no: int
    status: str
    chunk_id: str | None = None
    citation_key: str | None = None
    start: int
    end: int


class VerificationView(Wire):
    passed: bool
    verified_count: int
    unsupported_count: int
    uncited_sentences: list[str]


class GateView(Wire):
    decision: str
    search_query: str
    rewritten: bool
    reason: str
    signals: list[str]
    engine: str
    latency_ms: float


class TimingView(Wire):
    gate_ms: float
    retrieval_ms: float
    rerank_ms: float
    generation_ms: float
    verify_ms: float
    total_ms: float


class UsageView(Wire):
    input_tokens: int
    output_tokens: int
    usd: float


class RetrievalStatsView(Wire):
    candidates_considered: int
    dense_hits: int
    sparse_hits: int
    overlap: int


class AnswerView(Wire):
    """The `final` SSE event, and the body of the non-streaming endpoint."""

    kind: Literal["answer", "refusal", "blocked"]
    text: str
    engine: str
    citations: list[CitationView]
    evidence: list[ChunkView]
    near_misses: list[ChunkView]
    verification: VerificationView | None
    gate: GateView
    timings: TimingView
    usage: UsageView
    retrieval: RetrievalStatsView


class AskBody(Wire):
    """Request body for both ask endpoints."""

    question: str = Field(min_length=1, max_length=1000)
    history: list[TurnView] = Field(default_factory=list, max_length=20)
    law_id: str | None = Field(
        default=None,
        max_length=64,
        description="Restrict retrieval to one instrument, e.g. 'dubai-tenancy-law'.",
    )
    rerank: bool = True

    @model_validator(mode="after")
    def _question_is_not_blank(self) -> Self:
        """``min_length`` alone accepts "   ", which is not a question."""
        if not self.question.strip():
            raise ValueError("question must contain at least one non-whitespace character")
        return self


class TurnView(Wire):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class LawView(Wire):
    law_id: str
    label: str
    title: str
    jurisdiction: str
    publisher: str
    official_url: str
    article_count: int
    chunk_count: int


class HealthView(Wire):
    status: Literal["ok", "degraded"]
    version: str
    engine: str
    chunks_indexed: int
    vector_points: int
    laws: int
    reranker: str
    embedding_model: str
    langfuse: bool
    detail: str = ""


# ── serialisation ────────────────────────────────────────────────────────────


def chunk_view(item: ScoredChunk) -> ChunkView:
    chunk = item.chunk
    return ChunkView(
        chunk_id=chunk.chunk_id,
        law_id=chunk.law_id,
        law_label=chunk.law_label,
        law_title=chunk.law_title,
        part_title=chunk.part_title,
        article_no=chunk.article_no,
        article_title=chunk.article_title,
        section=chunk.section,
        seq=chunk.seq,
        seq_total=chunk.seq_total,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        text=chunk.text,
        heading=chunk.heading,
        citation_key=chunk.citation_key,
        source=item.source.value,
        dense_rank=item.dense_rank,
        dense_score=item.dense_score,
        sparse_rank=item.sparse_rank,
        sparse_score=item.sparse_score,
        rrf_score=item.rrf_score,
        fused_rank=item.fused_rank,
        rerank_score=item.rerank_score,
        rerank_probability=item.rerank_probability,
        final_rank=item.final_rank,
    )


def citation_view(citation: Citation) -> CitationView:
    return CitationView(
        raw=citation.raw,
        law_label=citation.law_label,
        article_no=citation.article_no,
        status=citation.status.value,
        chunk_id=citation.chunk_id,
        citation_key=citation.citation_key,
        start=citation.start,
        end=citation.end,
    )


def timing_view(timings: TimingBreakdown) -> TimingView:
    return TimingView(
        gate_ms=round(timings.gate_ms, 1),
        retrieval_ms=round(timings.retrieval_ms, 1),
        rerank_ms=round(timings.rerank_ms, 1),
        generation_ms=round(timings.generation_ms, 1),
        verify_ms=round(timings.verify_ms, 1),
        total_ms=round(timings.total_ms, 1),
    )


def usage_view(usage: Usage) -> UsageView:
    return UsageView(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usd=round(usage.usd, 6),
    )


def answer_view(result: PipelineResult) -> AnswerView:
    answer: Answer = result.answer
    verification = (
        VerificationView(
            passed=answer.verification.passed,
            verified_count=answer.verification.verified_count,
            unsupported_count=answer.verification.unsupported_count,
            uncited_sentences=list(answer.verification.uncited_sentences),
        )
        if answer.verification
        else None
    )
    return AnswerView(
        kind=answer.kind.value,
        text=answer.text,
        engine=answer.engine,
        citations=[citation_view(c) for c in answer.citations],
        evidence=[chunk_view(c) for c in answer.evidence],
        near_misses=[chunk_view(c) for c in answer.near_misses],
        verification=verification,
        gate=GateView(
            decision=result.gate.decision.value,
            search_query=result.gate.search_query,
            rewritten=result.gate.rewritten,
            reason=result.gate.reason,
            signals=list(result.gate.signals),
            engine=result.gate.engine,
            latency_ms=round(result.gate.latency_ms, 1),
        ),
        timings=timing_view(result.timings),
        usage=usage_view(result.usage),
        retrieval=RetrievalStatsView(
            candidates_considered=result.candidates_considered,
            dense_hits=result.dense_hits,
            sparse_hits=result.sparse_hits,
            overlap=result.overlap,
        ),
    )


AskBody.model_rebuild()
