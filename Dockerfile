# ─────────────────────────────────────────────────────────────────────────────
# Lexora API — one image, two targets.
#
#   Hugging Face Spaces  expects port 7860 and no card. Works as-is.
#   GCP Cloud Run        injects $PORT. The entrypoint honours it.
#
# The ONNX models are baked in at BUILD time, not fetched at boot. On a free tier
# a cold container that downloads ~200 MB before it can answer looks broken, and
# an outbound fetch on the startup path is one more thing that can fail in front
# of a user. The image is larger; the cold start is a model load, not a download.
#
# The portable index artefacts (chunks.jsonl + bm25.json) are copied in and the
# vector collection is materialised from them on first boot, so deployment never
# depends on two government web servers being up.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    LEXORA_VAR_DIR=/app/var

WORKDIR /app

# curl is used only by HEALTHCHECK below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first so a code change does not invalidate the wheel layer.
COPY apps/api/pyproject.toml /app/apps/api/pyproject.toml
RUN pip install --upgrade pip && pip install /app/apps/api

COPY apps/api/app /app/apps/api/app
COPY corpus/sources.json corpus/manifest.json /app/corpus/
COPY var/index /app/var/index

ENV PYTHONPATH=/app/apps/api

# Bake the embedding and reranking models into the image.
RUN python -c "\
from app.core.embedding import embed_query; \
from app.rag.rerank import get_cross_encoder; \
embed_query('warm up the session'); \
get_cross_encoder(); \
print('models cached in image')"

# Run as a non-root user. Spaces and Cloud Run both allow it, and there is no
# reason for an inference service to be root.
RUN useradd --create-home --uid 1000 lexora \
 && chown -R lexora:lexora /app
USER lexora

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-7860}/api/health" || exit 1

# Cloud Run sets $PORT; Hugging Face Spaces expects 7860.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1"]
