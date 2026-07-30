"""Session-scoped document storage. In memory, never on disk, purged on idle.

Nothing here is persisted, and that is a decision rather than a shortcut. The Space's
filesystem is ephemeral anyway, embedded Qdrant permits a single writer so per-session
collections would contend for one lock, and persisting means becoming the custodian of
whatever contract a stranger uploaded to a public demo. Holding documents in memory for
the life of a session answers "what happens to my file?" with something short and true.

Every limit in :class:`Settings` is therefore also a memory bound. The ceiling is
`workspace_max_sessions * workspace_max_docs` documents resident at once, and the
container has a measured budget (AUDIT.md §6.4) that this must not eat.

Purging happens on access, not on a timer. A sweeper thread would need its own lifecycle
and a shutdown path, and requests are the only thing that creates sessions — so the
arrival of one is exactly when it is worth checking for stale ones.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from app.core.models import Chunk
from app.core.settings import Settings, get_settings
from app.workspace.extract import ExtractedDocument

logger = logging.getLogger(__name__)


class WorkspaceFullError(Exception):
    """A limit was reached. The message is shown to the user verbatim."""


@dataclass(slots=True)
class WorkspaceDocument:
    doc_id: str
    title: str
    source: str
    kind: str
    pages: int
    chunks: list[Chunk]
    ocr_engine: str | None
    added_at: float
    #: True when the document was longer than `workspace_max_pages` and was cut. Surfaced
    #: rather than logged: an answer drawn from a silently truncated document is
    #: indistinguishable from one drawn from a complete one.
    truncated: bool = False
    #: Fraction of the document's words that reached the index. Anything below 1.0 means
    #: a question about the missing part would be refused for the wrong reason.
    coverage: float = 1.0

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


@dataclass(slots=True)
class WorkspaceSession:
    session_id: str
    created_at: float
    touched_at: float
    documents: dict[str, WorkspaceDocument] = field(default_factory=dict)

    @property
    def chunks(self) -> list[Chunk]:
        return [chunk for document in self.documents.values() for chunk in document.chunks]


class SessionStore:
    """Thread-safe. FastAPI runs handlers across a thread pool, so this is not optional."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._sessions: dict[str, WorkspaceSession] = {}
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _purge_locked(self, now: float) -> None:
        ttl = self._settings.workspace_session_ttl_s
        stale = [sid for sid, s in self._sessions.items() if now - s.touched_at > ttl]
        for session_id in stale:
            del self._sessions[session_id]
        if stale:
            logger.info("purged %d idle workspace session(s)", len(stale))

        # Still over the cap after purging: drop least-recently-used. A public demo has no
        # authentication, so the only backstop against unbounded growth is a hard ceiling.
        excess = len(self._sessions) - self._settings.workspace_max_sessions
        if excess > 0:
            for session_id, _ in sorted(self._sessions.items(), key=lambda kv: kv[1].touched_at)[
                :excess
            ]:
                del self._sessions[session_id]
            logger.warning("workspace at capacity; evicted %d least-recent session(s)", excess)

    def new_session_id(self) -> str:
        # 32 bytes of urandom: the id is the only thing separating one user's uploaded
        # contract from another's, so it is a credential and sized like one.
        return secrets.token_urlsafe(32)

    def get(self, session_id: str | None) -> WorkspaceSession | None:
        if not session_id:
            return None
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            session = self._sessions.get(session_id)
            if session is not None:
                session.touched_at = now
            return session

    def ensure(self, session_id: str | None) -> WorkspaceSession:
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session.touched_at = now
                return session
            new_id = session_id if session_id else self.new_session_id()
            session = WorkspaceSession(session_id=new_id, created_at=now, touched_at=now)
            self._sessions[new_id] = session
            return session

    # ── documents ────────────────────────────────────────────────────────────

    def add(
        self,
        session_id: str | None,
        document: ExtractedDocument,
        chunks: list[Chunk],
        doc_id: str,
        coverage: float = 1.0,
    ) -> tuple[WorkspaceSession, WorkspaceDocument]:
        if not chunks:
            raise WorkspaceFullError(f"nothing indexable was found in {document.title}")
        session = self.ensure(session_id)
        with self._lock:
            if len(session.documents) >= self._settings.workspace_max_docs:
                raise WorkspaceFullError(
                    f"this workspace already holds {self._settings.workspace_max_docs} documents. "
                    "Remove one before adding another."
                )
            entry = WorkspaceDocument(
                doc_id=doc_id,
                title=document.title,
                source=document.source,
                kind=document.kind,
                pages=len(document.pages),
                chunks=chunks,
                ocr_engine=document.ocr_engine,
                added_at=time.time(),
                truncated=bool(document.diagnostics.get("truncated")),
                coverage=coverage,
            )
            session.documents[doc_id] = entry
            return session, entry

    def remove(self, session_id: str | None, doc_id: str) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        with self._lock:
            return session.documents.pop(doc_id, None) is not None

    def clear(self, session_id: str | None) -> bool:
        """Drop the whole session. This is the 'delete my data' path and must be total."""
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "sessions": len(self._sessions),
                "documents": sum(len(s.documents) for s in self._sessions.values()),
                "chunks": sum(len(s.chunks) for s in self._sessions.values()),
            }


_STORE: SessionStore | None = None
_STORE_LOCK = threading.Lock()


def get_store(settings: Settings | None = None) -> SessionStore:
    global _STORE  # noqa: PLW0603 - deliberate process-wide singleton
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SessionStore(settings)
        return _STORE


def reset_store() -> None:
    """Drop every session. Used by tests; also the honest answer to a restart."""
    global _STORE  # noqa: PLW0603 - mirrors get_store
    with _STORE_LOCK:
        _STORE = None
