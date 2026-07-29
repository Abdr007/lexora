"""Qdrant access.

One client API covers both deployment shapes:

* **Embedded** (default) — Qdrant runs in-process against ``var/qdrant``. No account, no
  credentials, no network, $0. This is what runs locally and inside the container.
* **Qdrant Cloud** — set ``LEXORA_QDRANT_URL`` and ``LEXORA_QDRANT_API_KEY`` and the same
  code path talks to the managed free tier instead.

The payload carries every field the Evidence Panel needs, so answering a question never
requires a second store to be consulted for the text behind a citation.

Note on the embedded mode: Qdrant takes an exclusive lock on its storage directory, so
the indexer and the API server cannot hold it simultaneously. That is why the portable
index artefacts (``chunks.jsonl`` and ``bm25.json``) are the source of truth and the
vector collection is rebuilt from them on demand — see :func:`ensure_collection`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Final

from qdrant_client import QdrantClient, models

from app.core.models import Chunk
from app.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CLIENT: QdrantClient | None = None

# Qdrant point ids must be an unsigned integer or a UUID, while chunk ids are readable
# strings. The string is kept in the payload and the id is derived from it, so a rebuild
# is idempotent and a point can always be traced back to its chunk.
_ID_BITS: Final = 63
UPSERT_BATCH: Final = 128


def get_client(settings: Settings | None = None) -> QdrantClient:
    """Process-wide Qdrant client."""
    global _CLIENT  # noqa: PLW0603 - deliberate process-wide singleton
    if _CLIENT is not None:
        return _CLIENT
    cfg = settings or get_settings()
    with _LOCK:
        if _CLIENT is None:
            if cfg.qdrant_url:
                logger.info("connecting to Qdrant Cloud at %s", cfg.qdrant_url)
                _CLIENT = QdrantClient(
                    url=cfg.qdrant_url,
                    api_key=cfg.qdrant_api_key,
                    timeout=int(cfg.request_timeout_s),
                )
            else:
                cfg.qdrant_path.mkdir(parents=True, exist_ok=True)
                logger.info("opening embedded Qdrant at %s", cfg.qdrant_path)
                _CLIENT = QdrantClient(path=str(cfg.qdrant_path))
    return _CLIENT


def close_client() -> None:
    """Release the client and, in embedded mode, the storage lock."""
    global _CLIENT  # noqa: PLW0603 - mirrors get_client
    with _LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
            _CLIENT = None


def point_id(chunk_id: str) -> int:
    """Stable non-negative integer id derived from a chunk id."""
    import hashlib

    digest = hashlib.sha256(chunk_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> (64 - _ID_BITS)


def chunk_payload(chunk: Chunk) -> dict[str, Any]:
    """Everything the UI and the verifier need, stored beside the vector."""
    return {
        "chunk_id": chunk.chunk_id,
        "law_id": chunk.law_id,
        "law_label": chunk.law_label,
        "law_title": chunk.law_title,
        "part_id": chunk.part_id,
        "part_title": chunk.part_title,
        "article_no": chunk.article_no,
        "article_title": chunk.article_title,
        "section": chunk.section,
        "seq": chunk.seq,
        "seq_total": chunk.seq_total,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "text": chunk.text,
        "token_count": chunk.token_count,
    }


def chunk_from_payload(payload: dict[str, Any]) -> Chunk:
    return Chunk.model_validate(payload)


def recreate_collection(settings: Settings | None = None) -> None:
    """Drop and recreate the collection, with the payload indexes retrieval relies on."""
    cfg = settings or get_settings()
    client = get_client(cfg)
    if client.collection_exists(cfg.qdrant_collection):
        client.delete_collection(cfg.qdrant_collection)
    client.create_collection(
        collection_name=cfg.qdrant_collection,
        vectors_config=models.VectorParams(
            size=cfg.embedding_dim,
            distance=models.Distance.COSINE,
        ),
    )
    # Payload indexes let the UI restrict a search to one instrument inside the engine,
    # rather than post-filtering results and silently returning fewer than top-k.
    #
    # They are a server-side feature: embedded Qdrant scans payloads directly and emits
    # a UserWarning if asked to build one. Filtering still works identically in embedded
    # mode, so the index is created only where it does something.
    if cfg.qdrant_url:
        for field, schema in (
            ("law_id", models.PayloadSchemaType.KEYWORD),
            ("part_id", models.PayloadSchemaType.KEYWORD),
            ("article_no", models.PayloadSchemaType.INTEGER),
        ):
            client.create_payload_index(
                collection_name=cfg.qdrant_collection,
                field_name=field,
                field_schema=schema,
            )
    logger.info("created collection %s (dim=%d)", cfg.qdrant_collection, cfg.embedding_dim)


def upsert_chunks(
    chunks: list[Chunk], vectors: list[list[float]], settings: Settings | None = None
) -> int:
    """Write chunks and their vectors. Returns the number of points written."""
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    cfg = settings or get_settings()
    client = get_client(cfg)
    written = 0
    for start in range(0, len(chunks), UPSERT_BATCH):
        batch = chunks[start : start + UPSERT_BATCH]
        batch_vectors = vectors[start : start + UPSERT_BATCH]
        client.upsert(
            collection_name=cfg.qdrant_collection,
            points=[
                models.PointStruct(
                    id=point_id(chunk.chunk_id),
                    vector=vector,
                    payload=chunk_payload(chunk),
                )
                for chunk, vector in zip(batch, batch_vectors, strict=True)
            ],
            wait=True,
        )
        written += len(batch)
    return written


def collection_count(settings: Settings | None = None) -> int:
    cfg = settings or get_settings()
    client = get_client(cfg)
    if not client.collection_exists(cfg.qdrant_collection):
        return 0
    return int(client.count(cfg.qdrant_collection, exact=True).count)


def search(
    vector: list[float],
    limit: int,
    settings: Settings | None = None,
    law_id: str | None = None,
) -> list[tuple[Chunk, float]]:
    """Dense nearest neighbours, optionally restricted to one instrument."""
    cfg = settings or get_settings()
    client = get_client(cfg)
    query_filter = (
        models.Filter(
            must=[models.FieldCondition(key="law_id", match=models.MatchValue(value=law_id))]
        )
        if law_id
        else None
    )
    response = client.query_points(
        collection_name=cfg.qdrant_collection,
        query=vector,
        limit=limit,
        query_filter=query_filter,
        with_payload=True,
    )
    results: list[tuple[Chunk, float]] = []
    for point in response.points:
        if point.payload is None:  # pragma: no cover - with_payload=True guarantees it
            continue
        results.append((chunk_from_payload(point.payload), float(point.score)))
    return results
