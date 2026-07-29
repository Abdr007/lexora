# ─────────────────────────────────────────────────────────────────────────────
# Lexora
#
#   make setup     install both toolchains
#   make corpus    download the official law PDFs (digest-pinned)
#   make index     parse -> chunk -> embed -> Qdrant + BM25
#   make dev       run the API and the web app
#   make check     every quality gate, exactly as CI runs them
#   make eval      evaluate retrieval with and without reranking
#
# Ports are deliberately non-default (API 7861, web 3020): 7860 and 3000 are
# commonly already taken, and silently binding a neighbouring project's port is a
# confusing way to fail.
# ─────────────────────────────────────────────────────────────────────────────

SHELL := /bin/bash
PY    := apps/api/.venv/bin/python
PIP   := uv pip install --python apps/api/.venv/bin/python
API_PORT ?= 7861
WEB_PORT ?= 3020
export PYTHONPATH := apps/api

.DEFAULT_GOAL := help
.PHONY: help setup setup-api setup-web corpus index reindex warm dev api web stop status \
        check lint typecheck test fmt web-check eval eval-judge calibrate chunking \
        questions docker-build docker-run clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

# ── setup ────────────────────────────────────────────────────────────────────
setup: setup-api setup-web ## install both toolchains

setup-api: ## create the Python 3.12 venv and install the API
	uv venv --python 3.12 apps/api/.venv
	$(PIP) -e "apps/api[dev]"

setup-web: ## install the web dependencies
	cd apps/web && npm ci || (cd apps/web && npm install)

# ── corpus and index (Pipeline A) ────────────────────────────────────────────
corpus: ## download the official law PDFs and verify their digests
	$(PY) corpus/download.py

index: ## parse, chunk, embed and build both indexes
	$(PY) -m app.rag.index

reindex: corpus index ## re-download and rebuild from scratch

warm: ## pre-download the ONNX models (do this before an offline demo)
	$(PY) -c "from app.core.embedding import embed_query; from app.rag.rerank import get_cross_encoder; embed_query('warm'); get_cross_encoder(); print('models cached')"

questions: ## rebuild eval/questions.jsonl and verify every label
	$(PY) eval/build_questions.py

# ── run ──────────────────────────────────────────────────────────────────────
dev: ## run the API and the web app together
	./scripts/dev.sh start

api: ## run the API only
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(API_PORT) --reload

web: ## run the web app only
	cd apps/web && npm run dev

stop: ## stop both (leaves other projects alone)
	./scripts/dev.sh stop

status: ## show what is running
	./scripts/dev.sh status

# ── quality gates ────────────────────────────────────────────────────────────
check: lint typecheck test web-check ## every gate, as CI runs them
	@echo ""
	@echo "  all gates passed"

lint: ## ruff, zero warnings
	apps/api/.venv/bin/ruff check .
	apps/api/.venv/bin/ruff format --check .

fmt: ## apply ruff formatting
	apps/api/.venv/bin/ruff format .
	apps/api/.venv/bin/ruff check --fix .

typecheck: ## mypy --strict
	apps/api/.venv/bin/mypy

test: ## pytest, warnings are errors
	$(PY) -m pytest

web-check: ## tsc, eslint and a production build
	cd apps/web && npx tsc --noEmit && npx eslint . --max-warnings 0 && npm run build

# ── evaluation ───────────────────────────────────────────────────────────────
eval: ## retrieval metrics, with and without reranking
	$(PY) eval/ragas_run.py

eval-judge: ## add Claude-judged faithfulness and answer relevance
	$(PY) eval/ragas_run.py --judge

calibrate: ## recompute the refusal floor from the labelled set
	$(PY) eval/ragas_run.py --calibrate

chunking: ## re-index at 300/600/1000 tokens and compare hit-rate
	$(PY) eval/chunking_experiment.py

# ── container ────────────────────────────────────────────────────────────────
docker-build: ## build the API image (models baked in)
	docker build -t lexora-api:latest .

docker-run: ## run the image on the Hugging Face Spaces port
	docker run --rm -p 7860:7860 --env-file .env lexora-api:latest

clean: ## remove build artefacts, keep the corpus and the portable index
	rm -rf var/qdrant .pytest_cache .mypy_cache .ruff_cache apps/web/.next
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
