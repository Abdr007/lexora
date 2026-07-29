"""Shared fixtures.

The suite runs against the REAL corpus and the REAL index rather than fixtures. A parser
tested on a synthetic PDF proves nothing about the government typesetting it actually has
to survive, and a retrieval test over invented chunks measures nothing about retrieval
quality. Tests that need the index are marked `integration` and skip cleanly when it has
not been built.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.models import Chunk, ScoredChunk
from app.core.settings import Settings, get_settings
from app.rag.parse import ParsedDocument, ParseDiagnostics, parse_corpus


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture(scope="session")
def corpus_available(settings: Settings) -> bool:
    return settings.pdf_dir.exists() and any(settings.pdf_dir.glob("*.pdf"))


def _lock_held_by_another_process(lock: Path) -> bool:
    """Whether another process holds the embedded store's advisory lock.

    Deliberately does *not* construct a ``QdrantClient`` to find out. That constructor
    opens the lock file and only then raises, leaving the caller no reference with which
    to close the handle; the orphan surfaces as an unraisable ``ResourceWarning`` at
    interpreter shutdown, which under ``-W error`` fails the whole run and points at a
    lock file rather than at the process actually holding it. The previous version of
    this fixture did exactly that, and its docstring claimed it *prevented* the warning.

    Taking the same advisory lock on our own file object keeps ownership of the handle
    here, where ``with`` closes it on every path. ``portalocker`` is what qdrant-client
    itself uses, so this tests the same lock and not merely a similar one.
    """
    try:
        import portalocker
    except ImportError:  # pragma: no cover - qdrant-client always provides it
        return False
    try:
        with lock.open("r+", encoding="utf-8") as handle:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.BaseLockException:
                return True
            portalocker.unlock(handle)
    except OSError:
        return False
    return False


@pytest.fixture(scope="session")
def index_available(settings: Settings) -> bool:
    """Whether the on-disk index is present AND usable by this process.

    Embedded Qdrant allows a single writer, so a running `make dev` makes the store
    unopenable here. Detect that and skip with an instruction, rather than failing
    somewhere unrelated.
    """
    if not (settings.chunks_path.exists() and settings.bm25_path.exists()):
        return False
    # A configured remote Qdrant is not lock-protected and supports concurrent readers.
    if getattr(settings, "qdrant_url", None):
        return True
    lock = settings.qdrant_path / ".lock"
    if lock.exists() and _lock_held_by_another_process(lock):
        pytest.skip(
            "var/qdrant is locked by another process. Embedded Qdrant allows one "
            "writer at a time — run `make stop` before `make test`."
        )
    return True


@pytest.fixture(scope="session")
def parsed(corpus_available: bool) -> list[ParsedDocument]:
    if not corpus_available:
        pytest.skip("corpus PDFs not present; run `make corpus`")
    return parse_corpus(diagnostics=ParseDiagnostics())


@pytest.fixture(scope="session")
def diagnostics(corpus_available: bool) -> ParseDiagnostics:
    if not corpus_available:
        pytest.skip("corpus PDFs not present; run `make corpus`")
    diag = ParseDiagnostics()
    parse_corpus(diagnostics=diag)
    return diag


@pytest.fixture(scope="session")
def chunks(settings: Settings, index_available: bool) -> list[Chunk]:
    if not index_available:
        pytest.skip("index not built; run `make index`")
    from app.rag.index import load_chunks

    return load_chunks(settings)


def make_chunk(
    *,
    law_id: str = "uae-labour-law",
    law_label: str = "Labour Law",
    article_no: int = 51,
    text: str = "Labour Law - Article 51\nEnd of service benefits are payable.",
    part_id: str = "part-1",
) -> Chunk:
    """A minimal chunk for pure-logic tests that must not touch the index."""
    return Chunk(
        chunk_id=Chunk.make_id(law_id, part_id, article_no, 0, text),
        law_id=law_id,
        law_label=law_label,
        law_title=f"{law_label} title",
        part_id=part_id,
        part_title="Part",
        article_no=article_no,
        article_title="End of Service Benefits",
        section="",
        seq=0,
        seq_total=1,
        page_start=1,
        page_end=1,
        text=text,
        token_count=12,
    )


def make_scored(chunk: Chunk | None = None, **kwargs: object) -> ScoredChunk:
    from app.core.models import RetrievalSource

    return ScoredChunk(chunk=chunk or make_chunk(), source=RetrievalSource.BOTH, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def tmp_index_dir(tmp_path: Path) -> Path:
    """A scratch directory for tests that write index artefacts."""
    return tmp_path
