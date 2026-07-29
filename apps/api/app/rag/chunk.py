"""Article-aware chunking.

Two decisions in this file do most of the work for retrieval quality.

**Chunks never straddle an article.** An article is the unit a citation points at, so a
chunk that spans two of them cannot be cited honestly — half its text would support a
claim attributed to the wrong provision. Splitting starts from the article and works
down, never up.

**Every chunk carries a context header.** The body of Dubai Law 26/2007 Article 6 begins
"Where the term of a Lease Contract expires, but the Tenant continues to occupy..." — it
never says which law it is in, and it never says "Article 6". A user asking "what does
Article 6 of the tenancy law say?" would therefore miss it on both retrieval paths at
once: the words are absent from the sparse index and from the dense vector. Prefixing
``Tenancy Law - Article 6 (Term of Lease Contract)`` puts the exact terms a person
searches with into the text that is actually indexed.

The splitter measures in the embedding model's own tokens (see ``app.core.tokenizer``)
and, in production, caps a chunk at the encoder's 512-token window. Beyond that window
``bge-small-en-v1.5`` truncates its input, so the tail of an over-long chunk contributes
nothing to the vector while still being shown to the user as retrieved evidence. The
cap is a correctness property, not a tuning knob; ``eval/chunking_experiment.py``
quantifies what it is worth.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

from tokenizers import Tokenizer

from app.core.models import Article, Chunk, ParsedDocument
from app.core.settings import Settings, get_settings
from app.core.tokenizer import get_tokenizer

logger = logging.getLogger(__name__)

# Clause boundaries in UAE/Dubai statutory drafting, most structural first. Splitting
# here keeps "a. ... b. ... c. ..." ladders intact instead of cutting mid-enumeration.
_CLAUSE_BOUNDARY: Final = re.compile(
    r"(?=(?:^|\s)(?:\d{1,2}\.\s|[a-z]\.\s|\([a-z]\)\s|\(\d{1,2}\)\s))",
    re.MULTILINE,
)
_SENTENCE_BOUNDARY: Final = re.compile(r"(?<=[.;:])\s+")
_WORD_BOUNDARY: Final = re.compile(r"(?<=\s)")


@dataclass(frozen=True, slots=True)
class ChunkingStats:
    """What the splitter actually produced — reported by ``make index``."""

    chunks: int
    articles: int
    tokens_total: int
    tokens_max: int
    tokens_mean: float
    articles_split: int
    over_window: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "chunks": self.chunks,
            "articles": self.articles,
            "tokens_total": self.tokens_total,
            "tokens_max": self.tokens_max,
            "tokens_mean": round(self.tokens_mean, 1),
            "articles_split": self.articles_split,
            "over_window": self.over_window,
        }


def build_header(article: Article) -> str:
    """The context line prefixed to every chunk of ``article``."""
    header = f"{article.law_label} - Article {article.article_no}"
    if article.title:
        header = f"{header}: {article.title}"
    elif article.section:
        header = f"{header} ({article.section})"
    return header


def _segments(text: str) -> list[str]:
    """Break body text into the smallest units the packer is allowed to keep whole."""
    pieces = [piece for piece in _CLAUSE_BOUNDARY.split(text) if piece.strip()]
    return pieces or [text]


def _split_oversized(segment: str, budget: int, tokenizer: Tokenizer) -> list[str]:
    """Break a single segment that cannot fit the budget on its own.

    Falls back through sentence boundaries and finally whole words. Words are never
    split: a half-word helps neither the encoder nor a human reading the Evidence Panel.
    """
    for pattern in (_SENTENCE_BOUNDARY, _WORD_BOUNDARY):
        parts = [p for p in pattern.split(segment) if p.strip()]
        if len(parts) <= 1:
            continue
        out: list[str] = []
        buffer = ""
        for part in parts:
            candidate = f"{buffer}{part}" if buffer else part
            if buffer and _count(candidate, tokenizer) > budget:
                out.append(buffer.strip())
                buffer = part
            else:
                buffer = candidate
        if buffer.strip():
            out.append(buffer.strip())
        if all(_count(piece, tokenizer) <= budget for piece in out):
            return out
        segment = " ".join(out)
    return [segment]


def _count(text: str, tokenizer: Tokenizer) -> int:
    return len(tokenizer.encode(text, add_special_tokens=False).ids)


def _overlap_segments(packed: list[str], overlap_tokens: int, tokenizer: Tokenizer) -> list[str]:
    """Trailing whole segments of the previous chunk, up to the overlap budget.

    Overlap is taken at segment granularity rather than by slicing tokens, so a chunk
    never opens mid-clause. That keeps every chunk independently readable, which matters
    because chunks are shown verbatim in the Evidence Panel.
    """
    if overlap_tokens <= 0:
        return []
    carried: list[str] = []
    budget = overlap_tokens
    for segment in reversed(packed):
        cost = _count(segment, tokenizer)
        if cost > budget:
            break
        carried.insert(0, segment)
        budget -= cost
    # Never carry the entire previous chunk: that would stall forward progress.
    return carried if len(carried) < len(packed) else carried[1:]


def split_article(
    article: Article,
    *,
    target_tokens: int,
    overlap_tokens: int,
    tokenizer: Tokenizer,
) -> list[str]:
    """Split one article's body into chunk texts, each already carrying its header."""
    header = build_header(article)
    header_cost = _count(header, tokenizer) + 1
    budget = max(target_tokens - header_cost, 16)

    segments: list[str] = []
    for segment in _segments(article.text):
        cleaned = segment.strip()
        if not cleaned:
            continue
        if _count(cleaned, tokenizer) <= budget:
            segments.append(cleaned)
        else:
            segments.extend(_split_oversized(cleaned, budget, tokenizer))

    if not segments:
        return [header]

    chunks: list[str] = []
    packed: list[str] = []
    packed_tokens = 0
    for segment in segments:
        cost = _count(segment, tokenizer)
        if packed and packed_tokens + cost > budget:
            chunks.append(f"{header}\n{' '.join(packed)}")
            packed = _overlap_segments(packed, overlap_tokens, tokenizer)
            packed_tokens = sum(_count(piece, tokenizer) for piece in packed)
        packed.append(segment)
        packed_tokens += cost
    if packed:
        chunks.append(f"{header}\n{' '.join(packed)}")
    return chunks


def chunk_documents(
    documents: list[ParsedDocument],
    settings: Settings | None = None,
    *,
    target_tokens: int | None = None,
    overlap_tokens: int | None = None,
    enforce_encoder_window: bool = True,
) -> tuple[list[Chunk], ChunkingStats]:
    """Turn parsed documents into indexable chunks.

    Args:
        target_tokens: chunk size budget. Defaults to ``settings.chunk_target_tokens``.
        overlap_tokens: tokens of context carried between adjacent chunks of one article.
        enforce_encoder_window: cap the budget at the embedding model's context window.
            Always true in production. ``eval/chunking_experiment.py`` sets it false to
            measure what un-capped chunking costs, which is the whole point of the
            experiment — the answer is not "nothing".
    """
    cfg = settings or get_settings()
    tokenizer = get_tokenizer(cfg.embedding_model)
    requested = target_tokens if target_tokens is not None else cfg.chunk_target_tokens
    budget = min(requested, cfg.embedding_max_tokens) if enforce_encoder_window else requested
    overlap = overlap_tokens if overlap_tokens is not None else cfg.chunk_overlap_tokens
    overlap = min(overlap, max(budget - 1, 0))

    if enforce_encoder_window and requested > cfg.embedding_max_tokens:
        logger.info(
            "chunk target %d exceeds the %s context window (%d); capping at %d so the "
            "indexed text and the embedded text stay identical",
            requested,
            cfg.embedding_model,
            cfg.embedding_max_tokens,
            budget,
        )

    chunks: list[Chunk] = []
    articles = 0
    articles_split = 0
    over_window = 0
    token_counts: list[int] = []

    for document in documents:
        for article in document.articles:
            articles += 1
            texts = split_article(
                article,
                target_tokens=budget,
                overlap_tokens=overlap,
                tokenizer=tokenizer,
            )
            if len(texts) > 1:
                articles_split += 1
            for seq, text in enumerate(texts):
                tokens = _count(text, tokenizer)
                token_counts.append(tokens)
                if tokens > cfg.embedding_max_tokens:
                    over_window += 1
                chunks.append(
                    Chunk(
                        chunk_id=Chunk.make_id(
                            article.law_id, article.part_id, article.article_no, seq, text
                        ),
                        law_id=article.law_id,
                        law_label=article.law_label,
                        law_title=article.law_title,
                        part_id=article.part_id,
                        part_title=article.part_title,
                        article_no=article.article_no,
                        article_title=article.title,
                        section=article.section,
                        seq=seq,
                        seq_total=len(texts),
                        page_start=article.page_start,
                        page_end=article.page_end,
                        text=text,
                        token_count=max(tokens, 1),
                    )
                )

    if not chunks:
        raise ValueError("chunking produced no chunks; the parsed corpus is empty")

    stats = ChunkingStats(
        chunks=len(chunks),
        articles=articles,
        tokens_total=sum(token_counts),
        tokens_max=max(token_counts),
        tokens_mean=sum(token_counts) / len(token_counts),
        articles_split=articles_split,
        over_window=over_window,
    )
    if over_window and enforce_encoder_window:  # pragma: no cover - defensive
        raise ValueError(
            f"{over_window} chunks exceed the encoder window despite the cap being "
            "enforced; this is a bug in split_article, not a tuning problem"
        )
    return chunks, stats
