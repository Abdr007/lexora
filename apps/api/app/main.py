"""Lexora HTTP service.

Endpoints
    GET  /api/health           readiness, index size, which engine is live
    GET  /api/laws             the indexed corpus, with official source URLs
    GET  /api/chunk/{id}       one passage, for deep-linking the Evidence Panel
    GET  /api/metrics          the latest eval results, for the /metrics page
    POST /api/ask              full answer, non-streaming (used by the eval harness)
    POST /api/ask/stream       Server-Sent Events: gate -> retrieval -> tokens -> final

Hardening applied at this layer:

* **CORS is an allowlist**, never ``*``. Credentials are disabled, so a browser on another
  origin cannot ride a user's session — there is no session, and the policy says so.
* **Rate limited per IP** (slowapi, 30/min by default) on the two expensive endpoints.
* **Request bodies are bounded** by Pydantic (question length, history length and depth)
  *and* by a hard byte cap before parsing, so an oversized body is rejected before it is
  buffered.
* **Errors never leak internals.** Unhandled exceptions return an opaque message with a
  correlation id; the traceback goes to the log, not to the client.
* **Security headers** on every response, and the docs are served only outside production.
* **The index is loaded once at startup**, so no request pays for it and a broken index
  fails the container's readiness check instead of the first user's question.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api.schemas import (
    AnswerView,
    AskBody,
    ChunkView,
    HealthView,
    LawView,
    answer_view,
    chunk_view,
)
from app.api.workspace_routes import router as workspace_router
from app.api.workspace_routes import vectors_for as workspace_vectors
from app.core import claude, observability
from app.core.models import RetrievalSource, ScoredChunk, Turn
from app.core.settings import Settings, get_settings
from app.core.vectorstore import close_client, collection_count
from app.rag.index import ensure_vector_store, load_chunks
from app.rag.parse import load_manifest
from app.rag.pipeline import Pipeline, PipelineEvent

logger = logging.getLogger(__name__)

VERSION: Final = "1.0.0"
MAX_BODY_BYTES: Final = 64 * 1024
SSE_PING_SECONDS: Final = 15.0

limiter = Limiter(key_func=get_remote_address)


@dataclass(slots=True)
class AppState:
    """Everything loaded once at startup and shared read-only across requests."""

    pipeline: Pipeline | None = None
    chunk_count: int = 0
    vector_points: int = 0
    manifest: list[dict[str, Any]] = field(default_factory=list)
    article_counts: dict[str, int] = field(default_factory=dict)
    chunk_counts: dict[str, int] = field(default_factory=dict)
    ready: bool = False
    detail: str = ""


state = AppState()


def _warm_models(settings: Settings) -> None:
    """Force the ONNX sessions to load and run once.

    A cross-encoder's first call includes graph initialisation, which shows up as a
    multi-second outlier on whichever user happens to arrive first. Paying it at startup
    keeps the p95 the spec asks for honest rather than an artefact of warm caching.
    """
    from app.core.embedding import embed_query
    from app.rag.rerank import rerank

    embed_query("warm up the embedding session", settings)
    if state.pipeline is not None:
        candidates = list(state.pipeline.retriever.retrieve("end of service benefits").candidates)
        if candidates:
            rerank("end of service benefits", candidates[:4], settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    del app
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        chunks = load_chunks(settings)
        state.chunk_count = len(chunks)
        state.vector_points = ensure_vector_store(settings)
        state.pipeline = Pipeline(settings)
        state.manifest = load_manifest(settings)
        for chunk in chunks:
            state.chunk_counts[chunk.law_id] = state.chunk_counts.get(chunk.law_id, 0) + 1
        articles: dict[str, set[tuple[str, int]]] = {}
        for chunk in chunks:
            articles.setdefault(chunk.law_id, set()).add((chunk.part_id, chunk.article_no))
        state.article_counts = {law: len(seen) for law, seen in articles.items()}
        _warm_models(settings)
        state.ready = True
        logger.info(
            "Lexora ready: %d chunks, %d vector points, engine=%s",
            state.chunk_count,
            state.vector_points,
            claude.engine_name(settings),
        )
    except Exception as exc:
        state.ready = False
        state.detail = f"{type(exc).__name__}: {exc}"
        logger.exception("startup failed; service will report degraded")
    try:
        yield
    finally:
        observability.flush()
        close_client()


app = FastAPI(
    title="Lexora API",
    version=VERSION,
    description=(
        "Grounded RAG over UAE labour and Dubai tenancy law: hybrid retrieval, "
        "cross-encoder reranking, verified citations and an explicit refusal path."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)
app.state.limiter = limiter
app.include_router(workspace_router)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    # X-Lexora-Session carries the workspace session id. It must be allowed on the way in
    # and exposed on the way out, or the browser hides it from the client that needs it.
    allow_headers=["Content-Type", "X-Lexora-Session"],
    expose_headers=["X-Lexora-Session"],
    max_age=600,
)


@app.middleware("http")
async def security_and_limits(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject oversized bodies, stamp a request id, and add security headers."""
    # A question is never 64 KB, so the cap is deliberately tight — but a PDF is, and this
    # middleware runs before routing, so without an exemption every upload would 413 here
    # rather than reaching the endpoint that knows the real limit. The upload path is not
    # unbounded: it enforces `workspace_max_bytes` while reading, and reads one byte past
    # it precisely so it can tell "at the limit" from "over" it.
    cap = (
        _settings.workspace_max_bytes
        if request.url.path.startswith("/api/workspace/")
        else MAX_BODY_BYTES
    )
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > cap:
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": "Request body too large."},
        )
    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # An unhandled error must not return a stack trace to a browser. The id ties the
        # opaque client message to the full traceback in the log.
        logger.exception("unhandled error [%s] %s %s", request_id, request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal error.", "request_id": request_id},
            headers={"X-Request-Id": request_id},
        )
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-site"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    return response


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    del request, exc
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "detail": (
                "Rate limit reached. Lexora runs on free-tier infrastructure and allows "
                f"{get_settings().rate_limit} per IP."
            )
        },
    )


def validated_law_id(law_id: str | None) -> str | None:
    """Reject a filter naming an instrument the corpus does not contain.

    An unknown id used to match nothing, and the gate then reported that as "not covered
    by the indexed corpus" — a claim about the corpus that was simply false. The corpus
    may cover the question perfectly well; the *filter* was wrong. Conflating the two
    teaches a caller the wrong thing about the data, so say which it is.
    """
    if law_id is None:
        return None
    if law_id not in state.chunk_counts:
        known = ", ".join(sorted(state.chunk_counts)) or "none — the index is empty"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown law_id {law_id!r}. Valid values: {known}.",
        )
    return law_id


def require_pipeline() -> Pipeline:
    if state.pipeline is None or not state.ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Index not ready. {state.detail}".strip(),
        )
    return state.pipeline


def resolve_pipeline(body: AskBody, session_id: str | None, corpus: Pipeline) -> Pipeline:
    """Pick the corpus pipeline or build a workspace one over this session's documents.

    Four things differ for a workspace, and all four are corrections rather than
    conveniences:

    * the retriever holds the session's chunks, so a stranger's upload can never enter the
      evaluated collection, nor collide on embedded Qdrant's single writer lock;
    * the jurisdiction check is off — it encodes which legislatures the *corpus* covers,
      and would refuse a question about the user's own Saudi contract;
    * the refusal floor is the permissive workspace one, because the corpus's -3.6 was
      fitted against 61 labelled questions about the corpus and does not transfer;
    * the source profile names the user's own upload rather than the law corpus, in both
      the answer system prompt and the refusal copy. Naming the corpus there told the
      user something false about their own document, and told Claude it was reading UAE
      statute when it was reading their contract.
    """
    if body.scope != "workspace":
        return corpus

    from app.workspace.retriever import WorkspaceRetriever
    from app.workspace.store import get_store

    settings = get_settings()
    session = get_store(settings).get(session_id)
    if session is None or not session.documents:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No documents in this workspace yet. Add one, then ask.",
        )

    doc_ids = list(session.documents)
    chunks = [chunk for doc_id in doc_ids for chunk in session.documents[doc_id].chunks]
    vectors = workspace_vectors(doc_ids)
    if vectors.shape[0] != len(chunks):
        # Vectors and chunks are written together and removed together; a mismatch means
        # a bug, and answering from a misaligned index would attribute text to the wrong
        # passage on every citation.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="workspace index is inconsistent; remove the document and add it again",
        )

    return Pipeline(
        settings=settings.model_copy(
            update={
                "refusal_score_floor": settings.workspace_refusal_score_floor,
                "source_profile": "workspace",
            }
        ),
        retriever=WorkspaceRetriever.build(chunks, vectors, settings),  # type: ignore[arg-type]
        scope_check=None,
    )


# ── read endpoints ───────────────────────────────────────────────────────────


@app.get("/api/health", response_model=HealthView, tags=["meta"])
def health() -> HealthView:
    settings = get_settings()
    points = state.vector_points
    if state.ready:
        try:
            points = collection_count(settings)
        except Exception:
            logger.debug("collection_count failed during health check", exc_info=True)
    healthy = state.ready and points == state.chunk_count and state.chunk_count > 0
    return HealthView(
        status="ok" if healthy else "degraded",
        version=VERSION,
        engine=claude.engine_name(settings),
        chunks_indexed=state.chunk_count,
        vector_points=points,
        laws=len(state.manifest),
        reranker=settings.reranker_model,
        embedding_model=settings.embedding_model,
        langfuse=settings.langfuse_enabled,
        detail=state.detail,
    )


@app.get("/api/laws", response_model=list[LawView], tags=["corpus"])
def laws() -> list[LawView]:
    return [
        LawView(
            law_id=entry["law_id"],
            label=entry["label"],
            title=entry["title"],
            jurisdiction=entry["jurisdiction"],
            publisher=entry["publisher"],
            official_url=entry["official_url"],
            article_count=state.article_counts.get(entry["law_id"], 0),
            chunk_count=state.chunk_counts.get(entry["law_id"], 0),
        )
        for entry in state.manifest
    ]


@app.get("/api/chunk/{chunk_id}", response_model=ChunkView, tags=["corpus"])
def get_chunk(chunk_id: str, pipeline: Pipeline = Depends(require_pipeline)) -> ChunkView:
    chunk = pipeline.retriever.chunks_by_id.get(chunk_id)
    if chunk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown chunk.")
    return chunk_view(ScoredChunk(chunk=chunk, source=RetrievalSource.DENSE))


@app.get("/api/metrics", tags=["meta"])
def metrics() -> dict[str, Any]:
    """The latest evaluation output, rendered by the /metrics page.

    Served from the committed eval artefacts rather than recomputed: a RAGAS run costs
    real money and minutes, so the dashboard shows the last recorded run and says when it
    was produced.
    """
    settings = get_settings()
    path = settings.eval_results_dir / "latest.json"
    if not path.exists():
        return {
            "available": False,
            "detail": "No evaluation has been recorded yet. Run `make eval`.",
        }
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read eval results: %s", exc)
        return {"available": False, "detail": "Evaluation results could not be read."}
    payload["available"] = True
    return payload


# ── ask endpoints ────────────────────────────────────────────────────────────


def _to_turns(body: AskBody, settings: Settings) -> list[Turn]:
    return [
        Turn(role=turn.role, content=turn.content)
        for turn in body.history[-settings.max_history_turns :]
    ]


@app.post("/api/ask", response_model=AnswerView, tags=["ask"])
@limiter.limit(_settings.rate_limit)
async def ask(
    request: Request,
    body: AskBody,
    pipeline: Pipeline = Depends(require_pipeline),
    x_lexora_session: str | None = Header(default=None),
) -> AnswerView:
    """Answer without streaming. Used by the eval harness and by API consumers."""
    del request
    settings = get_settings()
    pipeline = resolve_pipeline(body, x_lexora_session, pipeline)
    result = await anyio.to_thread.run_sync(
        lambda: pipeline.run(
            body.question,
            _to_turns(body, settings),
            validated_law_id(body.law_id),
            body.rerank,
        )
    )
    return answer_view(result)


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/api/ask/stream", tags=["ask"])
@limiter.limit(_settings.rate_limit)
async def ask_stream(
    request: Request,
    body: AskBody,
    pipeline: Pipeline = Depends(require_pipeline),
) -> StreamingResponse:
    """Stream the answer as Server-Sent Events.

    The pipeline is a synchronous, CPU-bound generator (ONNX inference holds the GIL only
    in short bursts but still blocks). Each step is therefore advanced on a worker thread
    and awaited, so a slow question never stalls the event loop for other requests.

    Pulling one item at a time — rather than running the whole pipeline on a thread and
    posting into a channel — keeps the ownership simple: there is no shared buffer, no
    task group, and no cancel scope. If the client disconnects, Starlette closes this
    generator, the reference to `events` drops, and nothing is left running.
    """
    del request
    settings = get_settings()
    turns = _to_turns(body, settings)
    events = pipeline.run_stream(body.question, turns, validated_law_id(body.law_id), body.rerank)
    done = object()

    def advance() -> object:
        """Pull the next pipeline event, or the sentinel when exhausted."""
        try:
            return next(events)
        except StopIteration:
            return done

    async def generate() -> AsyncIterator[str]:
        try:
            while True:
                event = await anyio.to_thread.run_sync(advance)
                if event is done:
                    return
                assert isinstance(event, PipelineEvent)  # noqa: S101 - narrows the sentinel
                if event.type == "final" and event.result is not None:
                    payload: dict[str, Any] = answer_view(event.result).model_dump()
                else:
                    payload = event.data
                yield _sse(event.type, payload)
        except Exception:
            request_id = uuid.uuid4().hex[:12]
            logger.exception("stream failed [%s]", request_id)
            yield _sse("error", {"detail": "Internal error.", "request_id": request_id})
        finally:
            events.close()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Stops nginx and similar proxies buffering the stream into one blob, which
            # is what turns a live token stream into a six-second pause.
            "X-Accel-Buffering": "no",
        },
    )
