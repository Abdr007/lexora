"""Bring-your-own-document endpoints.

A workspace is one browser session's private set of documents. It is held in memory,
never written to disk, and dropped when the session goes idle — see `workspace/store.py`
for why that is a decision and not a shortcut.

The session id is a credential: it is the only thing separating one person's uploaded
contract from another's. It is generated server-side from `secrets.token_urlsafe(32)`,
returned once, and sent back on the `X-Lexora-Session` header. A client-supplied id that
does not already exist creates an empty session rather than reading someone else's, and
because the id is 256 bits of urandom, guessing one is not a realistic attack.

**Workspace answers are not the corpus's answers, and the API says so.** Every response
carries `calibrated: false`. The corpus's refusal floor was fitted against 61 labelled
questions about *that* corpus; on a document uploaded a moment ago there is no labelled
set and no fitted threshold, so a confident refusal would be an overclaim. The floor used
here is deliberately permissive and the flag is what the UI reads to say so out loud.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Any

import anyio
import numpy as np
from fastapi import APIRouter, Depends, File, Header, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.core.settings import Settings, get_settings
from app.workspace.chunker import chunk_document, coverage
from app.workspace.extract import ExtractedDocument, ExtractionError, extract_bytes, fetch_url
from app.workspace.store import SessionStore, WorkspaceFullError, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["workspace"])

SESSION_HEADER = "X-Lexora-Session"


class LinkBody(BaseModel):
    url: str = Field(min_length=4, max_length=2048)


class DocumentView(BaseModel):
    doc_id: str
    title: str
    source: str
    kind: str
    pages: int
    chunks: int
    ocr_engine: str | None = None
    truncated: bool = False
    coverage: float = 1.0


class IngestTimings(BaseModel):
    """Per-stage cost of accepting a document, in milliseconds.

    `/api/ask` has reported its stage timings since the beginning; ingest reported
    nothing, so its cost could only be profiled locally and extrapolated to the Space.
    That extrapolation was wrong: embedding measured as ~80% of ingest on an 8-core
    laptop, a 4.7x embedding speedup moved the hosted number by 17%, and the real
    bottleneck was somewhere this endpoint never reported. Numbers, not inference.
    """

    extract_ms: float = 0.0
    chunk_ms: float = 0.0
    embed_ms: float = 0.0
    total_ms: float = 0.0


class WorkspaceView(BaseModel):
    session_id: str
    documents: list[DocumentView]
    total_chunks: int
    #: False, always. Present so the UI cannot forget to say it.
    calibrated: bool = False
    limits: dict[str, int]
    #: Populated on upload/link; absent when simply reading the workspace back.
    timings: IngestTimings | None = None


def _view(session: Any, settings: Settings) -> WorkspaceView:
    documents = [
        DocumentView(
            doc_id=doc.doc_id,
            title=doc.title,
            source=doc.source,
            kind=doc.kind,
            pages=doc.pages,
            chunks=doc.chunk_count,
            ocr_engine=doc.ocr_engine,
            truncated=doc.truncated,
            coverage=doc.coverage,
        )
        for doc in session.documents.values()
    ]
    return WorkspaceView(
        session_id=session.session_id,
        documents=documents,
        total_chunks=sum(d.chunks for d in documents),
        limits={
            "max_documents": settings.workspace_max_docs,
            "max_bytes": settings.workspace_max_bytes,
            "max_pages": settings.workspace_max_pages,
            "session_ttl_s": int(settings.workspace_session_ttl_s),
        },
    )


def _ingest(
    document: ExtractedDocument,
    session_id: str | None,
    store: SessionStore,
    settings: Settings,
) -> tuple[Any, Any, IngestTimings]:
    """Chunk, embed and register a document. Runs off the event loop; embedding is CPU."""
    from app.core.embedding import embed_documents

    doc_id = f"doc-{uuid.uuid4().hex[:12]}"
    started = time.perf_counter()
    chunks = chunk_document(document, doc_id, settings)
    if not chunks:
        raise WorkspaceFullError(f"no indexable text was found in {document.title}")

    kept = coverage(document, chunks)
    chunked_at = time.perf_counter()
    vectors = embed_documents([chunk.text for chunk in chunks], settings)
    embedded_at = time.perf_counter()

    session, entry = store.add(session_id, document, chunks, doc_id, kept)
    # The vectors live beside the chunks on the document entry so a query never re-embeds.
    entry_vectors = np.asarray(vectors, dtype=np.float32)
    _VECTORS[doc_id] = entry_vectors
    timings = IngestTimings(
        chunk_ms=(chunked_at - started) * 1000,
        embed_ms=(embedded_at - chunked_at) * 1000,
    )
    return session, entry, timings


# Vectors are kept out of the dataclass so the store stays a plain container and numpy
# never has to be imported to describe a session. Keyed by doc_id, which is a uuid, so
# entries cannot collide across sessions; cleaned up whenever a document is removed.
_VECTORS: dict[str, np.ndarray] = {}


def vectors_for(doc_ids: list[str]) -> np.ndarray:
    """Stacked vectors for the given documents, in the order requested."""
    parts = [_VECTORS[doc_id] for doc_id in doc_ids if doc_id in _VECTORS]
    if not parts:
        return np.zeros((0, 0), dtype=np.float32)
    return np.vstack(parts)


def forget(doc_ids: list[str]) -> None:
    for doc_id in doc_ids:
        _VECTORS.pop(doc_id, None)


@router.get("", response_model=WorkspaceView)
async def show(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    x_lexora_session: Annotated[str | None, Header()] = None,
) -> WorkspaceView:
    """The current workspace. Creates an empty session when there is not one yet."""
    store = get_store(settings)
    session = store.ensure(x_lexora_session)
    response.headers[SESSION_HEADER] = session.session_id
    return _view(session, settings)


@router.post("/upload", response_model=WorkspaceView, status_code=status.HTTP_201_CREATED)
async def upload(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile, File()],
    x_lexora_session: Annotated[str | None, Header()] = None,
) -> WorkspaceView:
    """Accept a PDF, DOCX, image, HTML or text file."""
    store = get_store(settings)
    # Read with a ceiling rather than trusting content-length, which the client sets.
    data = await file.read(settings.workspace_max_bytes + 1)
    if len(data) > settings.workspace_max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"that file is larger than the {settings.workspace_max_bytes // 1_000_000} MB limit",
        )

    try:
        started = time.perf_counter()
        document = await anyio.to_thread.run_sync(
            lambda: extract_bytes(data, file.filename or "document", file.content_type, settings)
        )
        extracted_at = time.perf_counter()
        session, _, timings = await anyio.to_thread.run_sync(
            lambda: _ingest(document, x_lexora_session, store, settings)
        )
        timings = timings.model_copy(
            update={
                "extract_ms": (extracted_at - started) * 1000,
                "total_ms": (time.perf_counter() - started) * 1000,
            }
        )
    except ExtractionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except WorkspaceFullError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    response.headers[SESSION_HEADER] = session.session_id
    return _view(session, settings).model_copy(update={"timings": timings})


@router.post("/link", response_model=WorkspaceView, status_code=status.HTTP_201_CREATED)
async def link(
    response: Response,
    body: LinkBody,
    settings: Annotated[Settings, Depends(get_settings)],
    x_lexora_session: Annotated[str | None, Header()] = None,
) -> WorkspaceView:
    """Fetch a public URL and index it. Private addresses are refused — see extract.py."""
    store = get_store(settings)
    try:
        started = time.perf_counter()
        document = await anyio.to_thread.run_sync(lambda: fetch_url(body.url, settings))
        extracted_at = time.perf_counter()
        session, _, timings = await anyio.to_thread.run_sync(
            lambda: _ingest(document, x_lexora_session, store, settings)
        )
        timings = timings.model_copy(
            update={
                "extract_ms": (extracted_at - started) * 1000,
                "total_ms": (time.perf_counter() - started) * 1000,
            }
        )
    except ExtractionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except WorkspaceFullError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    response.headers[SESSION_HEADER] = session.session_id
    return _view(session, settings).model_copy(update={"timings": timings})


@router.delete("/{doc_id}", response_model=WorkspaceView)
async def remove(
    doc_id: str,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    x_lexora_session: Annotated[str | None, Header()] = None,
) -> WorkspaceView:
    store = get_store(settings)
    if not store.remove(x_lexora_session, doc_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such document in this workspace")
    forget([doc_id])
    session = store.ensure(x_lexora_session)
    response.headers[SESSION_HEADER] = session.session_id
    return _view(session, settings)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear(
    settings: Annotated[Settings, Depends(get_settings)],
    x_lexora_session: Annotated[str | None, Header()] = None,
) -> Response:
    """Delete everything in this session. The 'forget me' button must actually forget."""
    store = get_store(settings)
    session = store.get(x_lexora_session)
    if session is not None:
        forget(list(session.documents))
    store.clear(x_lexora_session)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
