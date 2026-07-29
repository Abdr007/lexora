"""Cross-encoder reranking, and the refusal gate that rides on its scores.

Bi-encoder vs cross-encoder — the distinction the whole stage rests on
---------------------------------------------------------------------
The retriever is a **bi-encoder**: query and passage are embedded *separately* and
compared by cosine. That is what makes it fast — every passage vector is precomputed —
and it is also its ceiling, because the passage was encoded without ever having seen the
query. "Can my employer keep my passport?" and a passage about custody of documents get
one shared summary vector each, and nuance is averaged away.

A **cross-encoder** concatenates the pair and runs them through the transformer together,
so attention operates across both. It cannot precompute anything — cost is O(candidates)
per query, not O(1) — which is precisely why it is used on 20 candidates and not on 181
chunks. Retrieve wide with the cheap model, judge narrow with the expensive one.

The output is an unbounded relevance logit, not a probability. That is useful twice: it
orders the candidates, and its absolute value is a calibrated signal of whether the
corpus contains an answer at all. :func:`apply_refusal_gate` uses the second property —
knowing when *not* to answer is the feature this project exists to demonstrate.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from tokenizers import Tokenizer

from app.core.models import ScoredChunk
from app.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ENCODER: object | None = None


class RerankerUnavailableError(RuntimeError):
    """The cross-encoder could not be loaded."""


def get_cross_encoder(settings: Settings | None = None) -> object:
    """Process-wide cross-encoder.

    Two interchangeable backends. ``fastembed`` runs the cross-encoder as ONNX on CPU
    with no PyTorch in the image; ``sentence-transformers`` offers the more familiar API
    at the cost of a multi-gigabyte torch dependency. ONNX is the default because image
    size is what decides whether this fits a free Cloud Run or Hugging Face Space at all.
    Either backend serves whichever checkpoint ``reranker_model`` names.
    """
    global _ENCODER  # noqa: PLW0603 - deliberate process-wide singleton
    if _ENCODER is not None:
        return _ENCODER
    cfg = settings or get_settings()
    with _LOCK:
        if _ENCODER is None:
            cfg.models_cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info("loading reranker %s via %s", cfg.reranker_model, cfg.reranker_backend)
            if cfg.reranker_backend == "fastembed":
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                _ENCODER = TextCrossEncoder(
                    model_name=cfg.reranker_model,
                    cache_dir=str(cfg.models_cache_dir),
                )
            else:  # pragma: no cover - optional heavyweight backend
                try:
                    from sentence_transformers import CrossEncoder
                except ImportError as exc:
                    raise RerankerUnavailableError(
                        "reranker_backend='sentence-transformers' requires the optional "
                        "`sentence-transformers` extra; the default 'fastembed' backend "
                        "runs the same weights as ONNX with no torch dependency."
                    ) from exc
                _ENCODER = CrossEncoder(cfg.reranker_model)
    return _ENCODER


def _prepare(documents: list[str], settings: Settings) -> list[tuple[str, int]]:
    """Cap each passage at ``rerank_max_tokens`` and return it with its token length.

    The length is returned rather than recomputed later so bucketing sorts on the real
    padded cost instead of a character-count proxy.
    """
    tokenizer = get_reranker_tokenizer(settings)
    if tokenizer is None:
        return [(document, len(document)) for document in documents]
    limit = settings.rerank_max_tokens
    prepared: list[tuple[str, int]] = []
    for document in documents:
        ids = tokenizer.encode(document, add_special_tokens=False).ids
        if 0 < limit < len(ids):
            prepared.append((tokenizer.decode(ids[:limit]), limit))
        else:
            prepared.append((document, len(ids)))
    return prepared


def _score_batch(query: str, documents: list[str], settings: Settings) -> list[float]:
    encoder = get_cross_encoder(settings)
    if settings.reranker_backend == "fastembed":
        scores = encoder.rerank(query, documents, batch_size=len(documents))  # type: ignore[attr-defined]
        return [float(value) for value in scores]
    pairs = [(query, document) for document in documents]  # pragma: no cover
    return [float(value) for value in encoder.predict(pairs)]  # type: ignore[attr-defined]


def _score(query: str, documents: list[str], settings: Settings) -> list[float]:
    """Score every (query, passage) pair, length-bucketed for throughput.

    A transformer batch is padded to its longest member, so scoring one 506-token
    passage alongside nineteen 90-token ones costs as much as twenty long ones. Sorting
    by length and scoring in small batches removes that waste. The transformation is
    purely a reordering — scores are bit-identical to a single large batch, which
    ``tests/test_rerank.py`` asserts — so it buys latency and changes nothing else.
    """
    if not documents:
        return []
    prepared = _prepare(documents, settings)
    batch_size = max(1, settings.rerank_batch_size)
    order = sorted(range(len(prepared)), key=lambda i: prepared[i][1])
    scores = [0.0] * len(prepared)
    for start in range(0, len(order), batch_size):
        indices = order[start : start + batch_size]
        batch = [prepared[i][0] for i in indices]
        for index, value in zip(indices, _score_batch(query, batch, settings), strict=True):
            scores[index] = value
    return scores


def rerank(
    query: str,
    candidates: list[ScoredChunk],
    settings: Settings | None = None,
    top_k: int | None = None,
) -> list[ScoredChunk]:
    """Rescore candidates with the cross-encoder and keep the best ``top_k``."""
    cfg = settings or get_settings()
    limit = top_k if top_k is not None else cfg.rerank_top_k
    if not candidates:
        return []

    scores = _score(query, [candidate.chunk.text for candidate in candidates], cfg)
    if len(scores) != len(candidates):  # pragma: no cover - defensive
        raise RerankerUnavailableError(
            f"reranker returned {len(scores)} scores for {len(candidates)} candidates"
        )

    scored = [
        candidate.model_copy(update={"rerank_score": score})
        for candidate, score in zip(candidates, scores, strict=True)
    ]
    # Ties broken by chunk_id so the same input always yields the same output order.
    scored.sort(key=lambda item: (-(item.rerank_score or 0.0), item.chunk.chunk_id))
    return [
        item.model_copy(update={"final_rank": position + 1})
        for position, item in enumerate(scored[:limit])
    ]


def passthrough(
    candidates: list[ScoredChunk],
    settings: Settings | None = None,
    top_k: int | None = None,
) -> list[ScoredChunk]:
    """Take the top fused candidates without reranking.

    This is the ``--no-rerank`` arm of the evaluation. It exists so the reranker's
    contribution is a measured delta rather than an assertion.
    """
    cfg = settings or get_settings()
    limit = top_k if top_k is not None else cfg.rerank_top_k
    return [
        candidate.model_copy(update={"final_rank": position + 1})
        for position, candidate in enumerate(candidates[:limit])
    ]


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """Whether the corpus covers the question, and the evidence either way."""

    covered: bool
    evidence: tuple[ScoredChunk, ...]
    near_misses: tuple[ScoredChunk, ...]
    best_score: float | None
    floor: float
    best_dense: float | None = None
    dense_floor: float | None = None
    reason: str = ""
    signal: str = ""


def apply_refusal_gate(
    reranked: list[ScoredChunk],
    settings: Settings | None = None,
    *,
    best_dense: float | None = None,
    scope: object | None = None,
) -> GateOutcome:
    """Decide whether the retrieved evidence is good enough to answer from.

    Retrieval always returns *something*: nearest-neighbour search over a non-empty index
    cannot return nothing, and a question the corpus has never heard of still comes back
    with five confidently-ranked passages. Generating from them is exactly how a RAG
    system produces a fluent, well-cited, wrong answer.

    Three independent signals must all pass, because each catches what the others miss —
    all three thresholds were fitted on the labelled eval set, not chosen by intuition:

    1. **Scope.** Does the question name a legal system the corpus does not contain?
       No similarity score can answer this; see ``app.rag.scope``.
    2. **Domain floor** on the best dense similarity. Answers "is this question even
       about the corpus's subject matter?"
    3. **Relevance floor** on the best cross-encoder score. Answers "is the single best
       passage actually responsive?"

    A refusal hands back the near misses, so it is auditable: the user sees what was
    considered and can judge the call themselves.

    With reranking disabled the cross-encoder signal does not exist, so only the scope
    and domain checks apply. AUDIT.md reports refusal accuracy per configuration rather
    than implying the un-reranked path is equally protected — measured, it is not.
    """
    cfg = settings or get_settings()
    if scope is not None:
        reason = getattr(scope, "reason", "")
        signal = getattr(scope, "signal", "out-of-scope")
        return GateOutcome(
            covered=False,
            evidence=(),
            near_misses=tuple(reranked[: cfg.refusal_near_miss_count]),
            best_score=reranked[0].rerank_score if reranked else None,
            floor=cfg.refusal_score_floor,
            best_dense=best_dense,
            dense_floor=cfg.refusal_dense_floor,
            reason=reason,
            signal=signal,
        )

    if not reranked:
        return GateOutcome(
            covered=False,
            evidence=(),
            near_misses=(),
            best_score=None,
            floor=cfg.refusal_score_floor,
            best_dense=best_dense,
            dense_floor=cfg.refusal_dense_floor,
            reason="Retrieval returned no candidates.",
            signal="empty-retrieval",
        )

    if best_dense is not None and best_dense < cfg.refusal_dense_floor:
        return GateOutcome(
            covered=False,
            evidence=(),
            near_misses=tuple(reranked[: cfg.refusal_near_miss_count]),
            best_score=reranked[0].rerank_score,
            floor=cfg.refusal_score_floor,
            best_dense=best_dense,
            dense_floor=cfg.refusal_dense_floor,
            reason="No indexed passage is close enough to this question's subject matter.",
            signal="below-domain-floor",
        )

    best = reranked[0].rerank_score
    if best is None or best >= cfg.refusal_score_floor:
        return GateOutcome(
            covered=True,
            evidence=tuple(reranked),
            near_misses=(),
            best_score=best,
            floor=cfg.refusal_score_floor,
            best_dense=best_dense,
            dense_floor=cfg.refusal_dense_floor,
        )
    return GateOutcome(
        covered=False,
        evidence=(),
        near_misses=tuple(reranked[: cfg.refusal_near_miss_count]),
        best_score=best,
        floor=cfg.refusal_score_floor,
        best_dense=best_dense,
        dense_floor=cfg.refusal_dense_floor,
        reason="The closest passage is not responsive enough to answer from.",
        signal="below-relevance-floor",
    )


_TOKENIZERS: dict[str, Tokenizer] = {}


def get_reranker_tokenizer(settings: Settings | None = None) -> Tokenizer | None:
    """The reranker's own tokenizer, used to cap passage length before scoring.

    Two details here are load-bearing, and both were found by a CI failure that could
    not be reproduced locally:

    1. **The cross-encoder is loaded first.** The tokenizer is located by globbing the
       model cache, so on a cold cache — a fresh container, a CI runner with no restored
       cache — the file does not exist yet because nothing has downloaded it. Forcing the
       encoder to load first guarantees the files are on disk before the search.

    2. **A miss is never cached.** With ``lru_cache`` a single early miss was memoised for
       the lifetime of the process, so truncation stayed silently disabled long after the
       model had arrived. Only successful lookups are cached.

    Why it matters that truncation actually happens: without it, passages exceed the
    model's window and the runtime truncates per batch, which makes a score depend on
    which other documents happen to share its batch. Length bucketing then stops being a
    pure reordering. With truncation on, scores are identical across batch sizes —
    measured at max |delta| 0.0000 over the corpus.
    """
    cfg = settings or get_settings()
    cached = _TOKENIZERS.get(cfg.reranker_model)
    if cached is not None:
        return cached

    # Ensure the model — and therefore its tokenizer.json — is on disk before looking.
    get_cross_encoder(cfg)

    stem = cfg.reranker_model.split("/")[-1]
    candidates = [
        path for path in cfg.models_cache_dir.rglob("tokenizer.json") if stem in str(path)
    ]
    if not candidates:
        logger.warning(
            "no cached tokenizer for %s under %s; reranking without truncation, which "
            "makes scores batch-dependent",
            cfg.reranker_model,
            cfg.models_cache_dir,
        )
        return None

    tokenizer = Tokenizer.from_file(str(max(candidates, key=lambda path: path.stat().st_mtime)))
    _TOKENIZERS[cfg.reranker_model] = tokenizer
    return tokenizer


def reset_cross_encoder() -> None:
    """Drop the cached encoder. Used by tests."""
    global _ENCODER  # noqa: PLW0603 - mirrors get_cross_encoder
    with _LOCK:
        _ENCODER = None
    _TOKENIZERS.clear()
