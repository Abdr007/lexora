#!/usr/bin/env python3
"""Bake the models and the vector store into the image — one stage per process.

Doing all three in a single `RUN` OOM-killed the Hugging Face Spaces build container
(exit code 137). Both ONNX sessions and the materialisation of all 181 vectors were
resident at the same time, and the *build* container's ceiling is lower than the memory
the Space is given at runtime — a different limit from the one AUDIT.md §6.4 measured,
which is why a service known to fit in 1 GB still failed to build.

Each stage leaves its artefact on disk — the model cache, then `var/qdrant` — so running
them as separate processes splits the memory without splitting the result. Peak RSS
becomes the largest single stage instead of the sum of all three.

Every stage prints its own peak RSS, because a build one OOM away from failing should
say so while it still succeeds. An OOM kill is SIGKILL: there is no traceback, no error
line, and the log simply stops. Without these numbers the only evidence is silence.

    python scripts/bake.py embedding
    python scripts/bake.py reranker
    python scripts/bake.py vectorstore
"""

from __future__ import annotations

import argparse
import platform
import resource
import sys
from collections.abc import Callable
from typing import Final


def peak_rss_mb() -> float:
    """Peak resident set size of this process, in MB.

    ``ru_maxrss`` is kilobytes on Linux and bytes on macOS. This runs on both — inside
    the image, and on whatever machine is testing the image before it is pushed.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 1024 if platform.system() == "Linux" else peak / 1024 / 1024


def bake_embedding() -> str:
    # Imported inside the stage, not at module scope: importing every backend up front
    # would load into one process exactly what this script exists to keep apart.
    from app.core.embedding import embed_query

    embed_query("warm up the session")
    return "embedding model cached"


def bake_reranker() -> str:
    from app.rag.rerank import get_cross_encoder

    get_cross_encoder()
    return "cross-encoder cached"


def bake_vectorstore() -> str:
    from app.core.vectorstore import close_client
    from app.rag.index import ensure_vector_store

    points = ensure_vector_store()
    # Embedded Qdrant takes an advisory lock. Leaving it held would make the collection
    # unopenable by the next layer, and by the container at boot.
    close_client()
    return f"{points} vector points"


# Ordered as the Dockerfile runs them: the vector store stage reuses the embedding model
# the first stage cached, so it must not run before it.
STAGES: Final[dict[str, Callable[[], str]]] = {
    "embedding": bake_embedding,
    "reranker": bake_reranker,
    "vectorstore": bake_vectorstore,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=list(STAGES), help="which artefact to bake")
    args = parser.parse_args(argv)

    result = STAGES[args.stage]()
    print(f"baked [{args.stage}] {result} — peak RSS {peak_rss_mb():.0f} MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
