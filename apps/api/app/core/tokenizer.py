"""Access to the embedding model's own tokenizer.

Chunk sizes are meaningless unless they are measured in the units the encoder actually
consumes. A "600 token" chunk counted with a different vocabulary can be 700 real tokens
— past ``bge-small-en-v1.5``'s 512-token window — and its tail is then silently dropped
from the vector while still being displayed to the user as retrieved evidence. So the
splitter counts with the exact tokenizer the embedder will use.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path

from tokenizers import Tokenizer

from app.core.settings import get_settings

logger = logging.getLogger(__name__)

# FastEmbed serves BAAI/bge-small-en-v1.5 from Qdrant's ONNX-quantised repackaging of
# the same weights, so the cache directory is named after that repo rather than BAAI's.
_CACHE_REPO_HINTS: tuple[str, ...] = ("bge-small-en-v1.5",)


class TokenizerUnavailableError(RuntimeError):
    """The embedding tokenizer could not be located or downloaded."""


def _find_cached_tokenizer(cache_dir: Path, hints: tuple[str, ...]) -> Path | None:
    if not cache_dir.exists():
        return None
    candidates = [
        path
        for path in cache_dir.rglob("tokenizer.json")
        if any(hint in str(path) for hint in hints)
    ]
    if not candidates:
        return None
    # Newest snapshot wins if a model was re-downloaded.
    return max(candidates, key=lambda p: p.stat().st_mtime)


@functools.lru_cache(maxsize=4)
def get_tokenizer(model_name: str | None = None) -> Tokenizer:
    """Return the tokenizer for the embedding model, preferring the local cache.

    Raises:
        TokenizerUnavailableError: when the tokenizer is neither cached nor downloadable.
            Guessing a substitute is not acceptable — every chunk-size guarantee in the
            index would silently become an approximation.
    """
    settings = get_settings()
    name = model_name or settings.embedding_model

    cached = _find_cached_tokenizer(settings.models_cache_dir, _CACHE_REPO_HINTS)
    if cached is not None:
        logger.debug("loading tokenizer from cache: %s", cached)
        return Tokenizer.from_file(str(cached))

    try:
        return Tokenizer.from_pretrained(name)
    except Exception as exc:
        raise TokenizerUnavailableError(
            f"could not load the tokenizer for {name!r}. It is normally cached under "
            f"{settings.models_cache_dir} by `make index`; with no network access, run "
            "`make warm` once on a connected machine first."
        ) from exc


def count_tokens(text: str, model_name: str | None = None) -> int:
    """Number of tokens the encoder will consume for ``text``, excluding [CLS]/[SEP]."""
    return len(get_tokenizer(model_name).encode(text, add_special_tokens=False).ids)
