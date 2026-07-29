"""The citation verifier — the last line of defence against a confident wrong answer.

Everything upstream reduces the *probability* of an ungrounded claim. This stage detects
one after the fact, deterministically, without asking a model anything.

The check
---------
Parse every inline ``[Law Label, Article N]`` out of the generated text and require that
the article resolve to a passage that was actually placed in the model's context. A
citation that does not resolve is a fabricated reference — the single most damaging
failure mode for a legal assistant, because a fabricated citation is *more* convincing
than an uncited claim, not less.

Matching is on ``(law label, article number)`` rather than on chunk identity. A model
handed chunk 2 of Article 30 and citing "[Labour Law, Article 30]" has cited correctly;
demanding chunk-level precision would manufacture failures out of correct behaviour.

What happens to a bad citation
------------------------------
It is marked ``unsupported`` and the answer is flagged, but the text is not silently
rewritten. Deleting a sentence from a legal answer creates a new document nobody reviewed,
and a user reading a redacted answer has no way to know something was removed. Surfacing
the failure loudly — in the API response and in the UI — is both safer and more honest.
The one exception is the uncited-claim sweep, which is advisory: it reports sentences
carrying no citation so they can be shown as unverified rather than treated as supported.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Final

from app.core.models import (
    CITATION_RE,
    Citation,
    CitationStatus,
    ScoredChunk,
    VerificationReport,
)

logger = logging.getLogger(__name__)

# A "claim" sentence is one that asserts something about the law. Headings, list markers
# and very short fragments are not claims and are excluded from the uncited sweep so it
# does not drown in false positives.
_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?])\s+")
_MIN_CLAIM_WORDS: Final = 6
_MARKDOWN_NOISE: Final = re.compile(r"^\s*(?:[-*+•]|\d+[.)]|#{1,6})\s*")


def _normalise_label(label: str) -> str:
    """Fold a law label for comparison: case, spacing and punctuation insensitive."""
    return re.sub(r"[^a-z0-9]+", "", label.lower())


def extract_citations(text: str) -> list[tuple[str, str, int, int, int]]:
    """Return ``(raw, law_label, article_no, start, end)`` for each inline citation."""
    found: list[tuple[str, str, int, int, int]] = []
    for match in CITATION_RE.finditer(text):
        found.append(
            (
                match.group(0),
                match.group("law").strip(),
                int(match.group("article")),
                match.start(),
                match.end(),
            )
        )
    return found


def find_uncited_claims(text: str) -> list[str]:
    """Sentences that assert something but carry no citation.

    Advisory only. Reported so the UI can mark them unverified; never used to fail the
    answer on its own, because a legitimate connective sentence ("Two rules apply here:")
    carries no citation and needs none.
    """
    uncited: list[str] = []
    for raw_sentence in _SENTENCE_SPLIT.split(text):
        sentence = _MARKDOWN_NOISE.sub("", raw_sentence.strip())
        if len(sentence.split()) < _MIN_CLAIM_WORDS:
            continue
        if CITATION_RE.search(sentence):
            continue
        uncited.append(sentence)
    return uncited


def verify_answer(
    text: str,
    evidence: Sequence[ScoredChunk],
) -> VerificationReport:
    """Check every citation in ``text`` against the passages the model was given."""
    # (normalised label, article number) -> the chunk that supports it.
    supported: dict[tuple[str, int], ScoredChunk] = {}
    # Article numbers alone, so a citation with a mangled label can still be diagnosed
    # rather than reported identically to a wholly invented one.
    for item in evidence:
        supported[(_normalise_label(item.chunk.law_label), item.chunk.article_no)] = item

    citations: list[Citation] = []
    unsupported = 0
    for raw, label, article_no, start, end in extract_citations(text):
        match = supported.get((_normalise_label(label), article_no))
        if match is not None:
            citations.append(
                Citation(
                    raw=raw,
                    law_label=match.chunk.law_label,
                    article_no=article_no,
                    status=CitationStatus.VERIFIED,
                    chunk_id=match.chunk.chunk_id,
                    citation_key=match.chunk.citation_key,
                    start=start,
                    end=end,
                )
            )
            continue
        unsupported += 1
        logger.warning(
            "unsupported citation %r: article %d of %r was not in the retrieved set",
            raw,
            article_no,
            label,
        )
        citations.append(
            Citation(
                raw=raw,
                law_label=label,
                article_no=article_no,
                status=CitationStatus.UNSUPPORTED,
                start=start,
                end=end,
            )
        )

    return VerificationReport(
        citations=tuple(citations),
        unsupported_count=unsupported,
        uncited_sentences=tuple(find_uncited_claims(text)),
        passed=unsupported == 0,
    )
