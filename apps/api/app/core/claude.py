"""Anthropic access, with an honest offline fallback.

Model routing, as the spec calls for: Haiku 4.5 does the cheap structural work (rewriting
a follow-up into a standalone query, screening it for injection) and Sonnet 4.6 writes the
grounded answer. Both are traced.

The offline mode
----------------
Lexora is built to run with no ``ANTHROPIC_API_KEY`` at all. In that mode the guard falls
back to a deterministic rewriter/screener and generation to an extractive composer that
quotes retrieved articles verbatim with their citations. This is **not** a simulation of
Claude and is never presented as one: every response carries ``engine="offline-extractive"``
through the API into the UI badge, and the eval harness refuses to report LLM-judged
metrics for it.

What that buys is real: retrieval, fusion, reranking, the refusal gate, the citation
verifier and the whole HTTP surface are exercised and measurable without a key, so the
numbers that do not depend on a language model are trustworthy today and the ones that do
are clearly marked as pending.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.core.observability import trace_generation
from app.core.settings import LLMMode, Settings, get_settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CLIENT: Any | None = None

Engine = Literal["anthropic", "offline-extractive"]

# Published per-million-token prices for the routed models, used only to show the user
# what a query cost. Kept together so a price change is a one-line edit.
_PRICING_USD_PER_MTOK: Final[dict[str, tuple[float, float]]] = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}
_DEFAULT_PRICING: Final[tuple[float, float]] = (3.00, 15.00)


class AnthropicUnavailableError(RuntimeError):
    """``llm_mode=anthropic`` was requested but the API cannot be used."""


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    engine: Engine = "offline-extractive"

    @property
    def usd(self) -> float:
        return estimate_cost_usd(self.model, self.input_tokens, self.output_tokens)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prompt_price, completion_price = _PRICING_USD_PER_MTOK.get(model, _DEFAULT_PRICING)
    return (input_tokens * prompt_price + output_tokens * completion_price) / 1_000_000


def get_client(settings: Settings | None = None) -> Any | None:
    """Anthropic client, or ``None`` when running offline."""
    global _CLIENT  # noqa: PLW0603 - deliberate process-wide singleton
    cfg = settings or get_settings()
    if not cfg.use_anthropic:
        return None
    if _CLIENT is not None:
        return _CLIENT
    with _LOCK:
        if _CLIENT is None:
            try:
                from anthropic import Anthropic
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise AnthropicUnavailableError("the `anthropic` package is not installed") from exc
            if not cfg.anthropic_api_key:
                if cfg.llm_mode is LLMMode.ANTHROPIC:
                    raise AnthropicUnavailableError(
                        "LEXORA_LLM_MODE=anthropic requires LEXORA_ANTHROPIC_API_KEY to be set"
                    )
                return None
            _CLIENT = Anthropic(api_key=cfg.anthropic_api_key, timeout=cfg.llm_timeout_s)
            logger.info("Anthropic client ready")
    return _CLIENT


def is_online(settings: Settings | None = None) -> bool:
    """True when real model calls will be made."""
    return get_client(settings) is not None


def engine_name(settings: Settings | None = None) -> Engine:
    return "anthropic" if is_online(settings) else "offline-extractive"


def complete(
    *,
    system: str,
    messages: Sequence[Message],
    model: str,
    max_tokens: int,
    settings: Settings | None = None,
    trace_name: str = "generation",
    metadata: dict[str, Any] | None = None,
) -> Completion:
    """One non-streaming model call. Raises only when the caller must know."""
    cfg = settings or get_settings()
    client = get_client(cfg)
    if client is None:
        raise AnthropicUnavailableError("no Anthropic client; caller must use its offline path")

    payload = [{"role": message.role, "content": message.content} for message in messages]
    with trace_generation(
        trace_name,
        model=model,
        input={"system": system, "messages": payload},
        metadata=metadata or {},
    ) as span:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=cfg.temperature,
            system=system,
            messages=payload,
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = getattr(response, "usage", None)
        completion = Completion(
            text=text,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=model,
            engine="anthropic",
        )
        span.update(
            output=text,
            usage_details={
                "input": completion.input_tokens,
                "output": completion.output_tokens,
            },
        )
    return completion


def stream(
    *,
    system: str,
    messages: Sequence[Message],
    model: str,
    max_tokens: int,
    settings: Settings | None = None,
    trace_name: str = "generation",
    metadata: dict[str, Any] | None = None,
) -> Iterator[str | Completion]:
    """Stream text deltas, then yield a final :class:`Completion` with usage.

    The mixed yield type keeps the caller from having to reconcile a stream with a
    separate usage lookup: the last item is always the completion record.
    """
    cfg = settings or get_settings()
    client = get_client(cfg)
    if client is None:
        raise AnthropicUnavailableError("no Anthropic client; caller must use its offline path")

    payload = [{"role": message.role, "content": message.content} for message in messages]
    chunks: list[str] = []
    with trace_generation(
        trace_name,
        model=model,
        input={"system": system, "messages": payload},
        metadata=metadata or {},
    ) as span:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            temperature=cfg.temperature,
            system=system,
            messages=payload,
        ) as streamed:
            for delta in streamed.text_stream:
                chunks.append(delta)
                yield delta
            final = streamed.get_final_message()
        usage = getattr(final, "usage", None)
        completion = Completion(
            text="".join(chunks),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=model,
            engine="anthropic",
        )
        span.update(
            output=completion.text,
            usage_details={
                "input": completion.input_tokens,
                "output": completion.output_tokens,
            },
        )
    yield completion


def reset_client() -> None:
    """Drop the cached client. Used by tests."""
    global _CLIENT  # noqa: PLW0603 - mirrors get_client
    with _LOCK:
        _CLIENT = None
