"""Jurisdiction scope check — the signal no similarity score can provide.

Measured on the labelled eval set, the four trap questions that defeat the cross-encoder
are all the same kind:

    "What is the notice period under Saudi Arabian labour law?"   CE +2.10
    "What does DIFC Employment Law say about end-of-service gratuity?"   CE +2.11
    "How do I apply for a UAE golden visa?"   CE +3.29

Topically these are *perfect* matches. The corpus is full of notice periods and gratuity.
A cross-encoder is trained to score topical relevance, and by that measure it is right —
which is exactly the problem. **Topical relevance is not legal applicability.** A correct
answer about UAE federal labour law is a wrong answer to a question about Saudi law, and
no amount of reranking can discover that, because the distinction is not semantic
similarity at all. It is a fact about which legal system governs.

So it is checked explicitly, against a closed list of legal systems the corpus does not
contain. The list is deliberately narrow:

* foreign jurisdictions (other GCC states, and countries named outright);
* UAE financial free zones that operate their own employment law — DIFC and ADGM are
  carved out of Federal Decree-Law 33/2021 and have separate regimes;
* other emirates' tenancy regimes, since Dubai Law 26/2007 is Dubai-only. Note this
  fires only when paired with a tenancy term: UAE labour law *is* federal, so
  "Abu Dhabi" alone must never take a labour question out of scope.

What it deliberately does NOT do is guess about topics. "Is there a minimum wage?" is in
scope even though the corpus gives no figure — answering "the Cabinet may set one" is the
correct, grounded response, and a topic blocklist would have suppressed it.
"""

from __future__ import annotations

import re
from typing import Final, NamedTuple


class ScopeVerdict(NamedTuple):
    """Why a question falls outside the indexed corpus."""

    reason: str
    signal: str


# Legal systems with their own labour or tenancy law that this corpus does not contain.
_FOREIGN_JURISDICTIONS: Final[tuple[tuple[str, str], ...]] = (
    (r"\bsaudi(?:\s+arabia\w*)?\b", "Saudi Arabia"),
    (r"\bqatar\w*\b", "Qatar"),
    (r"\boman\b|\bomani\b", "Oman"),
    (r"\bkuwait\w*\b", "Kuwait"),
    (r"\bbahrain\w*\b", "Bahrain"),
    (r"\begypt\w*\b", "Egypt"),
    (r"\bjordan\w*\b", "Jordan"),
    (r"\blebanon\b|\blebanese\b", "Lebanon"),
    (r"\bindia\b|\bindian\s+labou?r\b", "India"),
    (r"\bpakistan\w*\b", "Pakistan"),
    (r"\bphilippines?\b|\bfilipino\b", "the Philippines"),
    (r"\bsingapore\w*\b", "Singapore"),
    (r"\bjapan\w*\b", "Japan"),
    (r"\bchina\b|\bchinese\s+labou?r\b", "China"),
    (r"\bunited\s+kingdom\b|\bbritish\s+(?:labou?r|employment)\b|\buk\s+employment\b", "the UK"),
    (r"\bunited\s+states\b|\bus\s+(?:labor|employment)\s+law\b", "the United States"),
    (r"\beuropean\s+union\b|\beu\s+(?:labour|employment)\s+law\b", "the European Union"),
)

# UAE free zones carved out of the federal labour law with their own employment regime.
_FREE_ZONES: Final[tuple[tuple[str, str], ...]] = (
    (r"\bdifc\b|\bdubai\s+international\s+financial\s+cent(?:re|er)\b", "the DIFC"),
    (r"\badgm\b|\babu\s+dhabi\s+global\s+market\b", "the ADGM"),
)

# Other emirates. Only out of scope for TENANCY, because Dubai Law 26/2007 is Dubai-only
# while the labour law is federal and covers every emirate.
_OTHER_EMIRATES: Final[tuple[tuple[str, str], ...]] = (
    (r"\babu\s+dhabi\b", "Abu Dhabi"),
    (r"\bsharjah\b", "Sharjah"),
    (r"\bajman\b", "Ajman"),
    (r"\bras\s+al\s+khaimah\b|\brak\b", "Ras Al Khaimah"),
    (r"\bfujairah\b", "Fujairah"),
    (r"\bumm\s+al\s+quwain\b", "Umm Al Quwain"),
)
_TENANCY_TERMS: Final = re.compile(
    r"\b(?:tenan\w*|landlord\w*|lease\w*|rent\w*|eviction|ejari)\b", re.IGNORECASE
)

_COMPILED_FOREIGN: Final = tuple(
    (re.compile(pattern, re.IGNORECASE), name) for pattern, name in _FOREIGN_JURISDICTIONS
)
_COMPILED_ZONES: Final = tuple(
    (re.compile(pattern, re.IGNORECASE), name) for pattern, name in _FREE_ZONES
)
_COMPILED_EMIRATES: Final = tuple(
    (re.compile(pattern, re.IGNORECASE), name) for pattern, name in _OTHER_EMIRATES
)


# Any Unicode letter: word characters that are neither digits nor underscore. Written
# this way rather than [a-z] so Arabic questions are treated as the real questions they
# are -- the corpus is UAE law and its readers do not all type in English.
_LETTER_RE: Final = re.compile(r"[^\W\d_]", re.UNICODE)
_MIN_LETTERS: Final = 2


def check_scope(question: str) -> ScopeVerdict | None:
    """Return a verdict when the question names a legal system outside the corpus."""
    # Degenerate input first. A cross-encoder will happily score emoji or a bare number
    # against legal passages, and those scores are not small: "🙂🙂🙂" scored -1.16 and
    # "42" scored +1.83, both comfortably above the -3.6 refusal floor, so both were
    # answered with citations. No threshold on a relevance score can fix that, because
    # the score is meaningless for input that is not language. Refuse it deterministically
    # before it reaches retrieval.
    if len(_LETTER_RE.findall(question)) < _MIN_LETTERS:
        return ScopeVerdict(
            reason=(
                "That question contains no words to interpret. Ask about UAE federal "
                "labour law or Dubai tenancy law in a sentence, and the answer will cite "
                "the articles it comes from."
            ),
            signal="no-answerable-content",
        )
    for pattern, name in _COMPILED_FOREIGN:
        if pattern.search(question):
            return ScopeVerdict(
                reason=(
                    f"This question is about the law of {name}. The indexed corpus covers "
                    "UAE federal labour law and Dubai tenancy legislation only, so nothing "
                    "in it can answer this — regardless of how similar the wording looks."
                ),
                signal="foreign-jurisdiction",
            )
    for pattern, name in _COMPILED_ZONES:
        if pattern.search(question):
            return ScopeVerdict(
                reason=(
                    f"This question is about {name}, which operates its own employment law "
                    "outside Federal Decree-Law 33 of 2021. The indexed corpus does not "
                    "contain that regime."
                ),
                signal="free-zone-jurisdiction",
            )
    if _TENANCY_TERMS.search(question):
        for pattern, name in _COMPILED_EMIRATES:
            if pattern.search(question):
                return ScopeVerdict(
                    reason=(
                        f"This is a tenancy question about {name}. The indexed tenancy "
                        "legislation — Law 26/2007 and its amendments — applies to the "
                        "Emirate of Dubai only."
                    ),
                    signal="other-emirate-tenancy",
                )
    return None
