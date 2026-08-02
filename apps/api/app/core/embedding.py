"""Local embeddings via FastEmbed (ONNX, CPU, zero cost).

Nothing leaves the process. That is the compliance answer for a UAE government or legal
client — the corpus and every query stay inside the deployment boundary — and it is why
inference cost per query is dominated by one Claude call rather than by an embedding API.

`bge-small-en-v1.5` is asymmetric: queries are embedded with an instruction prefix and
documents without one. Getting that backwards costs real recall, so the two directions
are separate functions here rather than one `embed(text)` that callers must remember to
parameterise.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Iterable, Sequence
from typing import Final

from fastembed import TextEmbedding

from app.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_MODEL: TextEmbedding | None = None


def get_embedding_model(settings: Settings | None = None) -> TextEmbedding:
    """Process-wide embedding model.

    Loaded once and shared: the ONNX session is thread-safe for inference, and holding a
    single instance keeps a cold container from paying the load cost per request.
    """
    global _MODEL  # noqa: PLW0603 - deliberate process-wide singleton
    if _MODEL is not None:
        return _MODEL
    cfg = settings or get_settings()
    with _LOCK:
        if _MODEL is None:
            cfg.models_cache_dir.mkdir(parents=True, exist_ok=True)
            logger.info("loading embedding model %s", cfg.embedding_model)
            _MODEL = TextEmbedding(
                model_name=cfg.embedding_model,
                cache_dir=str(cfg.models_cache_dir),
            )
    return _MODEL


#: Small enough that a batch is uniform in length once sorted, large enough to keep the
#: per-call overhead amortised. Measured: 8 beat 16 (1.08s) and 32 (1.19s) on a 46-chunk
#: document; the win comes from uniformity, not from the size itself.
_EMBED_BATCH: Final = 8


def embed_documents(texts: Sequence[str], settings: Settings | None = None) -> list[list[float]]:
    """Embed corpus passages. No instruction prefix — this is the document side.

    Batches are filled by length. Every text in a batch is padded to the longest one in
    it, so a batch mixing a 75-character heading with a 2,092-character article computes
    the short one at the long one's width and throws the difference away. Documents are
    exactly this lumpy: the UDHR's 46 chunks run 75 to 2,092 characters around a median of
    233. Sorting first makes each batch nearly uniform and cut ingest embedding from
    2.96s to 0.67s — the single biggest cost in accepting an uploaded document.

    Order is restored before returning, because the caller pairs vectors with chunks
    positionally and a permuted result would attribute every passage to the wrong text.

    This changes no vector meaningfully. Batch composition perturbs float32 arithmetic, so
    a vector shifts by at most ~2e-4 elementwise (cosine similarity 0.9999999) and the
    retrieved ranking is unchanged — verified against the unsorted path. It is therefore
    safe on the existing corpus index without a re-embed.
    """
    if not texts:
        return []
    model = get_embedding_model(settings)
    ordered = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    embedded = list(model.embed([texts[i] for i in ordered], batch_size=_EMBED_BATCH))

    restored: list[list[float]] = [[] for _ in texts]
    for position, original_index in enumerate(ordered):
        restored[original_index] = embedded[position].tolist()
    return restored


def embed_query(text: str, settings: Settings | None = None) -> list[float]:
    """Embed a search query, with the instruction prefix bge was trained to expect."""
    cfg = settings or get_settings()
    return _embed_query_cached(f"{cfg.embedding_query_prefix}{text}")


@functools.lru_cache(maxsize=512)
def _embed_query_cached(prefixed: str) -> list[float]:
    """Cache identical queries.

    Repeated questions are the norm in a demo, and the eval harness embeds the same
    query once per configuration. The key is the fully prefixed string, so changing the
    prefix can never return a vector built with the old one.

    ``TextEmbedding.embed`` is used deliberately in place of ``query_embed``: measured
    against this build of FastEmbed, ``query_embed(q)`` is bit-identical to ``embed(q)``
    (cosine 1.000000) — it does *not* add bge's instruction prefix, despite the name.
    Relying on it to do so would leave every query embedded on the document side of an
    asymmetric model, losing recall with nothing in the logs to show for it.
    """
    model = get_embedding_model()
    vectors: Iterable[object] = model.embed([prefixed])
    for vector in vectors:
        return list(vector.tolist())  # type: ignore[attr-defined]
    raise RuntimeError("embedding model returned no vector")


def reset_embedding_model() -> None:
    """Drop the cached model and query cache. Used by tests."""
    global _MODEL  # noqa: PLW0603 - mirrors get_embedding_model
    with _LOCK:
        _MODEL = None
    _embed_query_cached.cache_clear()
