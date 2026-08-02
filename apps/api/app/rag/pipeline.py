"""Pipeline B, assembled.

    question
      -> guard          rewrite follow-ups, screen for injection
      -> retrieve       Qdrant dense top-20 + BM25 top-20 -> RRF(k=60) -> top-20
      -> rerank         local cross-encoder -> top-5
      -> refusal gate   is the best passage good enough to answer from at all?
      -> generate       Claude Sonnet, temp 0, context-only, streamed
      -> verify         every cited article must be in the retrieved set

The path is **stateless**: all conversation state arrives in the request, the retriever
and the models are read-only shared singletons, and nothing here writes to an index. That
is what makes concurrent requests safe without a lock, and it is why index writes are
confined to the offline script.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.core.claude import Completion, engine_name
from app.core.models import (
    Answer,
    AnswerKind,
    GateDecision,
    GateResult,
    ScoredChunk,
    TimingBreakdown,
    Turn,
    Usage,
)
from app.core.observability import trace_span
from app.core.settings import Settings, get_settings
from app.guard.gate import run_gate
from app.rag.generate import looks_like_refusal, refusal_text, stream_answer
from app.rag.rerank import apply_refusal_gate, passthrough, rerank
from app.rag.retrieve import HybridRetriever
from app.rag.scope import ScopeVerdict, check_scope
from app.rag.verify import verify_answer

logger = logging.getLogger(__name__)

#: A jurisdiction check: question -> verdict, or None when the question is in scope.
#: Injectable so a workspace can run without one — see Pipeline.__init__.
ScopeCheck = Callable[[str], ScopeVerdict | None]


@dataclass(slots=True)
class PipelineEvent:
    """One step of the streamed response, mirrored 1:1 onto the SSE wire.

    ``result`` is populated only on the ``final`` event and carries the whole outcome as
    a live object. Passing it directly — rather than through a module-level registry
    keyed by id — keeps the pipeline free of shared mutable state, which is what lets
    concurrent requests share one Pipeline instance safely and leaves nothing to leak
    when a client disconnects mid-stream.
    """

    type: Literal["gate", "retrieval", "token", "final", "error"]
    data: dict[str, object] = field(default_factory=dict)
    result: PipelineResult | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """The complete outcome of one question."""

    answer: Answer
    gate: GateResult
    timings: TimingBreakdown
    usage: Usage
    candidates_considered: int
    dense_hits: int
    sparse_hits: int
    overlap: int


class Pipeline:
    """Holds the loaded indexes. Construct once per process; safe to share."""

    def __init__(
        self,
        settings: Settings | None = None,
        retriever: HybridRetriever | None = None,
        scope_check: ScopeCheck | None = check_scope,
    ) -> None:
        self.settings = settings or get_settings()
        self.retriever = retriever or HybridRetriever.load(self.settings)
        # Injectable, and `None` disables it. The jurisdiction check encodes which
        # legislatures *this corpus* covers; applied to a document the user uploaded
        # themselves it would refuse a question about their own Saudi contract on the
        # grounds that Saudi law is out of scope. That is right for the corpus and
        # nonsense for a workspace.
        self.scope_check = scope_check

    # ── the streaming path used by the API ───────────────────────────────
    def run_stream(
        self,
        question: str,
        history: Sequence[Turn] = (),
        law_id: str | None = None,
        use_rerank: bool = True,
    ) -> Generator[PipelineEvent, None, None]:
        """Run the pipeline, yielding events as each stage completes.

        Typed as a Generator rather than an Iterator on purpose: the HTTP layer
        calls ``.close()`` on it when a client disconnects mid-stream, and only a
        generator carries that method.
        """
        cfg = self.settings
        started = time.perf_counter()

        with trace_span("pipeline.ask", input={"question": question, "law_id": law_id}):
            gate = run_gate(question, history, cfg)
            gate_ms = gate.latency_ms
            yield PipelineEvent(
                "gate",
                {
                    "decision": gate.decision.value,
                    "search_query": gate.search_query,
                    "rewritten": gate.rewritten,
                    "signals": list(gate.signals),
                    "engine": gate.engine,
                },
            )

            if gate.decision is GateDecision.BLOCK:
                answer = Answer(
                    kind=AnswerKind.BLOCKED,
                    text=gate.reason,
                    gate=gate,
                    engine=engine_name(cfg),
                )
                # Emitted so the client renders the block message through the same
                # streaming path as every other answer. Without it a blocked request
                # arrives as an empty bubble.
                yield PipelineEvent("token", {"text": answer.text})
                yield PipelineEvent(
                    "final",
                    result=PipelineResult(
                        answer=answer,
                        gate=gate,
                        timings=TimingBreakdown(
                            gate_ms=gate_ms,
                            total_ms=(time.perf_counter() - started) * 1000,
                        ),
                        usage=Usage(),
                        candidates_considered=0,
                        dense_hits=0,
                        sparse_hits=0,
                        overlap=0,
                    ),
                )
                return

            retrieval_started = time.perf_counter()
            retrieval = self.retriever.retrieve(gate.search_query, law_id=law_id)
            retrieval_ms = (time.perf_counter() - retrieval_started) * 1000

            rerank_started = time.perf_counter()
            candidates = list(retrieval.candidates)
            top = (
                rerank(gate.search_query, candidates, cfg)
                if use_rerank
                else passthrough(candidates, cfg)
            )
            rerank_ms = (time.perf_counter() - rerank_started) * 1000

            outcome = apply_refusal_gate(
                top,
                cfg,
                best_dense=retrieval.best_dense,
                scope=self.scope_check(gate.search_query) if self.scope_check else None,
            )
            yield PipelineEvent(
                "retrieval",
                {
                    "candidates": len(candidates),
                    "dense_hits": retrieval.dense_hits,
                    "sparse_hits": retrieval.sparse_hits,
                    "overlap": retrieval.overlap,
                    "covered": outcome.covered,
                    "best_score": outcome.best_score,
                    "floor": outcome.floor,
                    "best_dense": outcome.best_dense,
                    "dense_floor": outcome.dense_floor,
                    "signal": outcome.signal,
                    "reranked": use_rerank,
                },
            )

            if not outcome.covered:
                base = refusal_text(cfg)
                refused = f"{base}\n\n{outcome.reason}" if outcome.reason else base
                answer = Answer(
                    kind=AnswerKind.REFUSAL,
                    text=refused,
                    near_misses=outcome.near_misses,
                    gate=gate,
                    engine=engine_name(cfg),
                )
                yield PipelineEvent("token", {"text": refused})
                yield PipelineEvent(
                    "final",
                    result=PipelineResult(
                        answer=answer,
                        gate=gate,
                        timings=TimingBreakdown(
                            gate_ms=gate_ms,
                            retrieval_ms=retrieval_ms,
                            rerank_ms=rerank_ms,
                            total_ms=(time.perf_counter() - started) * 1000,
                        ),
                        usage=Usage(),
                        candidates_considered=len(candidates),
                        dense_hits=retrieval.dense_hits,
                        sparse_hits=retrieval.sparse_hits,
                        overlap=retrieval.overlap,
                    ),
                )
                return

            evidence = outcome.evidence
            generation_started = time.perf_counter()
            completion: Completion | None = None
            collected: list[str] = []
            for item in stream_answer(gate.search_query, evidence, history, cfg):
                if isinstance(item, Completion):
                    completion = item
                    continue
                collected.append(item)
                yield PipelineEvent("token", {"text": item})
            generation_ms = (time.perf_counter() - generation_started) * 1000

            text = completion.text if completion else "".join(collected)
            verify_started = time.perf_counter()
            report = verify_answer(text, evidence)
            verify_ms = (time.perf_counter() - verify_started) * 1000

            # A grounded model that declines is refusing, and the UI must show it as one.
            # This is the only layer able to catch a near-domain miss — see generate.py.
            answered = not looks_like_refusal(text, citation_count=report.verified_count)
            answer = Answer(
                kind=AnswerKind.ANSWER if answered else AnswerKind.REFUSAL,
                text=text,
                citations=report.citations if answered else (),
                verification=report if answered else None,
                evidence=evidence if answered else (),
                near_misses=() if answered else evidence[: cfg.refusal_near_miss_count],
                gate=gate,
                engine=engine_name(cfg),
            )
            usage = (
                Usage(
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    usd=completion.usd,
                )
                if completion
                else Usage()
            )
            yield PipelineEvent(
                "final",
                result=PipelineResult(
                    answer=answer,
                    gate=gate,
                    timings=TimingBreakdown(
                        gate_ms=gate_ms,
                        retrieval_ms=retrieval_ms,
                        rerank_ms=rerank_ms,
                        generation_ms=generation_ms,
                        verify_ms=verify_ms,
                        total_ms=(time.perf_counter() - started) * 1000,
                    ),
                    usage=usage,
                    candidates_considered=len(candidates),
                    dense_hits=retrieval.dense_hits,
                    sparse_hits=retrieval.sparse_hits,
                    overlap=retrieval.overlap,
                ),
            )

    # ── non-streaming convenience, used by the eval harness ──────────────
    def run(
        self,
        question: str,
        history: Sequence[Turn] = (),
        law_id: str | None = None,
        use_rerank: bool = True,
    ) -> PipelineResult:
        """Run to completion and return the final result."""
        final: PipelineResult | None = None
        for event in self.run_stream(question, history, law_id, use_rerank):
            if event.type == "final" and event.result is not None:
                final = event.result
        if final is None:  # pragma: no cover - run_stream always emits a final event
            raise RuntimeError("pipeline produced no final event")
        return final

    def rank(self, question: str, retrieval: object, use_rerank: bool = True) -> list[ScoredChunk]:
        """Rerank (or pass through) an existing retrieval result.

        Split out so the eval harness can reuse one retrieval for the gate's dense signal
        instead of running retrieval twice and risking the two arms diverging.
        """
        candidates = list(getattr(retrieval, "candidates", ()))
        if not use_rerank:
            return passthrough(candidates, self.settings)
        return rerank(question, candidates, self.settings)

    def retrieve_only(
        self, question: str, law_id: str | None = None, use_rerank: bool = True
    ) -> list[ScoredChunk]:
        """Retrieval + optional reranking with no generation. Used by the eval harness."""
        retrieval = self.retriever.retrieve(question, law_id=law_id)
        candidates = list(retrieval.candidates)
        if not use_rerank:
            return passthrough(candidates, self.settings)
        return rerank(question, candidates, self.settings)
