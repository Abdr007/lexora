"""LLM-judged generation metrics: faithfulness and answer relevance.

Why these are implemented here rather than imported from ``ragas``
-----------------------------------------------------------------
The ``ragas`` package (0.4.3, the current release) fails at import time against current
``langchain-community``::

    File ".../ragas/llms/base.py", line 12, in <module>
      from langchain_community.chat_models.vertexai import ChatVertexAI
    ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'

Pinning the whole LangChain stack back far enough to satisfy it would drag a large,
conflicting dependency tree into a service whose entire selling point is that it runs on
a free CPU tier. So the two metric *definitions* are implemented directly against the
Anthropic SDK, following the published RAGAS formulations:

**Faithfulness** — decompose the answer into atomic claims; for each, ask whether the
retrieved context entails it. Score = supported claims / total claims. This is the metric
that catches a fluent answer built on something the corpus never said.

**Answer relevance** — ask the judge to generate the questions the answer would be a good
reply to, then measure their similarity to the real question. A technically faithful
answer that addresses the wrong question scores low.

Both are reported as ``None`` when no API key is configured, never as 0.0 — an absent
measurement and a bad measurement must not look alike in a report.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
# Both the repo root and the API package must be importable: these scripts import
# `eval.*` (sibling modules) and `app.*` (the service). Doing it here rather than
# relying on PYTHONPATH means the script runs correctly from any working directory
# and under any runner — a missing PYTHONPATH previously broke the CI eval step only.
for _path in (REPO_ROOT, REPO_ROOT / "apps" / "api"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.core.claude import Message, complete, is_online  # noqa: E402
from app.core.embedding import embed_query  # noqa: E402
from app.core.settings import Settings, get_settings  # noqa: E402

logger = logging.getLogger(__name__)

FAITHFULNESS_PROMPT: Final = """\
You are grading whether an answer is supported by the context it was given.

Break the ANSWER into atomic factual claims. A claim is one assertion that could be \
checked on its own. Ignore hedging, transitions and restatements of the question.

For each claim decide whether the CONTEXT entails it. Entailment means the context \
states it or makes it necessarily true — not that the claim is plausible, and not that \
you happen to know it is correct. Outside knowledge is irrelevant here.

Reply with ONLY this JSON object, no prose and no code fence:
{"claims": [{"claim": "<text>", "supported": true|false}]}\
"""

RELEVANCE_PROMPT: Final = """\
Read the ANSWER below. Write the three questions this answer would be a direct and \
complete reply to. Do not use the original question; infer the questions from the answer \
alone. If the answer is evasive or declines to answer, say so by returning an empty list.

Reply with ONLY this JSON object, no prose and no code fence:
{"questions": ["<q1>", "<q2>", "<q3>"]}\
"""


@dataclass(frozen=True, slots=True)
class JudgeResult:
    faithfulness: float | None
    answer_relevancy: float | None
    claims_total: int = 0
    claims_supported: int = 0


def _parse_json(raw: str) -> dict[str, object] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def judge_faithfulness(
    answer: str, context: list[str], settings: Settings | None = None
) -> tuple[float | None, int, int]:
    """Share of the answer's atomic claims entailed by the retrieved context."""
    cfg = settings or get_settings()
    if not is_online(cfg) or not answer.strip() or not context:
        return None, 0, 0
    joined = "\n\n".join(f"<passage>{text}</passage>" for text in context)
    completion = complete(
        system=FAITHFULNESS_PROMPT,
        messages=[Message(role="user", content=f"CONTEXT:\n{joined}\n\nANSWER:\n{answer}")],
        model=cfg.answer_model,
        max_tokens=1500,
        settings=cfg,
        trace_name="eval.faithfulness",
        metadata={"stage": "eval"},
    )
    parsed = _parse_json(completion.text)
    claims = parsed.get("claims") if parsed else None
    if not isinstance(claims, list) or not claims:
        return None, 0, 0
    total = len(claims)
    supported = sum(1 for claim in claims if isinstance(claim, dict) and claim.get("supported"))
    return supported / total, total, supported


def judge_answer_relevance(
    question: str, answer: str, settings: Settings | None = None
) -> float | None:
    """Cosine similarity between the real question and questions reverse-engineered
    from the answer.

    The local embedding model is reused as the similarity function, so this metric costs
    one cheap generation and no embedding spend.
    """
    cfg = settings or get_settings()
    if not is_online(cfg) or not answer.strip():
        return None
    completion = complete(
        system=RELEVANCE_PROMPT,
        messages=[Message(role="user", content=f"ANSWER:\n{answer}")],
        model=cfg.guard_model,
        max_tokens=400,
        settings=cfg,
        trace_name="eval.answer_relevance",
        metadata={"stage": "eval"},
    )
    parsed = _parse_json(completion.text)
    generated = parsed.get("questions") if parsed else None
    if not isinstance(generated, list) or not generated:
        return 0.0
    reference = embed_query(question, cfg)
    scores = [
        _cosine(reference, embed_query(str(candidate), cfg))
        for candidate in generated
        if str(candidate).strip()
    ]
    return sum(scores) / len(scores) if scores else 0.0


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


def judge(
    question: str, answer: str, context: list[str], settings: Settings | None = None
) -> JudgeResult:
    faithfulness, total, supported = judge_faithfulness(answer, context, settings)
    relevance = judge_answer_relevance(question, answer, settings)
    return JudgeResult(
        faithfulness=faithfulness,
        answer_relevancy=relevance,
        claims_total=total,
        claims_supported=supported,
    )
