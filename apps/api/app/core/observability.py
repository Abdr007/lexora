"""Langfuse tracing, wrapped so it can never take the service down.

Observability is the first thing to fail in production — an expired key, a network
partition, a provider outage — and the last thing that should be allowed to fail a user's
request because of it. Every entry point here is therefore total: if Langfuse is not
configured, or its SDK raises for any reason, the wrapper degrades to a no-op and the
pipeline proceeds. Nothing in the answer path depends on a trace being written.

What is traced, per the spec, is *every model call*: the Haiku query gate and the Sonnet
generation, each with its inputs, outputs, model name and token usage — which is what
makes "why did it answer that?" answerable from the retrieval set rather than guessed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from app.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_CLIENT: Any | None = None
_INITIALISED = False


class Span(Protocol):
    """The subset of the Langfuse span surface this codebase uses."""

    def update(self, **kwargs: Any) -> Any: ...


class _NullSpan:
    """Stand-in used whenever tracing is unavailable."""

    def update(self, **kwargs: Any) -> None:
        """Accept and discard everything: this stands in for a span that is not there."""
        del kwargs


NULL_SPAN = _NullSpan()


def get_langfuse(settings: Settings | None = None) -> Any | None:
    """Return a configured Langfuse client, or ``None`` when tracing is off."""
    global _CLIENT, _INITIALISED  # noqa: PLW0603 - process-wide singleton
    if _INITIALISED:
        return _CLIENT
    cfg = settings or get_settings()
    _INITIALISED = True
    if not cfg.langfuse_enabled:
        logger.info("Langfuse not configured; tracing disabled")
        _CLIENT = None
        return None
    try:
        from langfuse import Langfuse

        _CLIENT = Langfuse(
            public_key=cfg.langfuse_public_key,
            secret_key=cfg.langfuse_secret_key,
            host=cfg.langfuse_host,
        )
        logger.info("Langfuse tracing enabled (%s)", cfg.langfuse_host)
    except Exception:
        logger.warning("Langfuse initialisation failed; continuing untraced", exc_info=True)
        _CLIENT = None
    return _CLIENT


@contextmanager
def trace_span(name: str, **attributes: Any) -> Iterator[Span]:
    """Trace a pipeline stage. Yields a span that is safe to call unconditionally."""
    client = get_langfuse()
    if client is None:
        yield NULL_SPAN
        return
    try:
        with client.start_as_current_span(name=name, **attributes) as span:
            yield span
    except Exception:
        logger.debug("trace_span(%s) failed; continuing untraced", name, exc_info=True)
        yield NULL_SPAN


@contextmanager
def trace_generation(name: str, **attributes: Any) -> Iterator[Span]:
    """Trace a model call, recording model name, input, output and usage."""
    client = get_langfuse()
    if client is None:
        yield NULL_SPAN
        return
    try:
        with client.start_as_current_generation(name=name, **attributes) as span:
            yield span
    except Exception:
        logger.debug("trace_generation(%s) failed; continuing untraced", name, exc_info=True)
        yield NULL_SPAN


def flush() -> None:
    """Best-effort flush of pending traces, called on shutdown."""
    client = get_langfuse()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        logger.debug("Langfuse flush failed", exc_info=True)


def reset() -> None:
    """Forget the cached client. Used by tests."""
    global _CLIENT, _INITIALISED  # noqa: PLW0603 - mirrors get_langfuse
    _CLIENT = None
    _INITIALISED = False
