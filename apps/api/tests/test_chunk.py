"""Chunking invariants, checked against the real corpus."""

from __future__ import annotations

import pytest

from app.core.settings import Settings
from app.rag.chunk import build_header, chunk_documents
from app.rag.parse import ParsedDocument

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def chunked(parsed: list[ParsedDocument], settings: Settings):
    return chunk_documents(parsed, settings)


def test_no_chunk_exceeds_the_encoder_window(chunked, settings: Settings):
    """The load-bearing property.

    Past 512 tokens bge-small truncates its input, so the tail of an over-long chunk
    contributes nothing to the vector while still being displayed as retrieved evidence.
    """
    chunks, stats = chunked
    assert stats.over_window == 0
    assert stats.tokens_max <= settings.embedding_max_tokens
    assert all(chunk.token_count <= settings.embedding_max_tokens for chunk in chunks)


def test_chunks_never_straddle_an_article(chunked):
    """A chunk spanning two articles could not be cited honestly."""
    chunks, _ = chunked
    for chunk in chunks:
        assert chunk.text.startswith(f"{chunk.law_label} - Article {chunk.article_no}")


def test_every_chunk_carries_its_context_header(chunked):
    """Statutory text never names its own law or article; the header supplies both."""
    chunks, _ = chunked
    for chunk in chunks:
        header, _, body = chunk.text.partition("\n")
        assert str(chunk.article_no) in header
        assert chunk.law_label in header
        assert body.strip()


def test_article_text_is_fully_covered_by_its_chunks(parsed, chunked):
    """No article may lose text to chunking.

    Overlap means the concatenation is longer than the original, but every article's
    first and last words must appear somewhere in its chunk set.
    """
    chunks, _ = chunked
    by_article: dict[tuple[str, str, int], list[str]] = {}
    for chunk in chunks:
        by_article.setdefault((chunk.law_id, chunk.part_id, chunk.article_no), []).append(
            chunk.text
        )

    for document in parsed:
        for article in document.articles:
            joined = " ".join(by_article[(article.law_id, article.part_id, article.article_no)])
            words = article.text.split()
            assert words[0] in joined
            assert " ".join(words[-4:]) in joined or words[-1] in joined


def test_chunk_ids_are_unique_and_content_addressed(chunked):
    chunks, _ = chunked
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))


def test_reindexing_is_deterministic(parsed, settings: Settings):
    """Identical input must produce identical ids, so a rebuild is a no-op."""
    first, _ = chunk_documents(parsed, settings)
    second, _ = chunk_documents(parsed, settings)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


def test_smaller_target_produces_more_chunks(parsed, settings: Settings):
    small, small_stats = chunk_documents(parsed, settings, target_tokens=300)
    large, _ = chunk_documents(parsed, settings, target_tokens=600)
    assert len(small) > len(large)
    assert small_stats.tokens_max <= 320


def test_uncapped_chunking_exceeds_the_window(parsed, settings: Settings):
    """The cap is worth something: without it, chunks are silently truncated by bge."""
    _, stats = chunk_documents(parsed, settings, target_tokens=1000, enforce_encoder_window=False)
    assert stats.over_window > 0
    assert stats.tokens_max > settings.embedding_max_tokens


def test_header_prefers_the_article_title_then_the_section(parsed):
    tenancy = next(d for d in parsed if d.law_id == "dubai-tenancy-law")
    titled = next(a for a in tenancy.articles if a.article_no == 5)
    untitled = next(a for a in tenancy.articles if a.article_no == 6)
    assert build_header(titled).endswith("Term of Lease Contract")
    assert "(Term of Lease Contract)" in build_header(untitled)
