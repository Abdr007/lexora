"""Central configuration for Lexora.

Every tunable in the retrieval and generation pipeline is declared here so that a
reviewer can see the entire behavioural surface of the system in one file, and so the
eval harness can vary knobs (chunk size, rerank on/off) without touching pipeline code.

Values are read from the environment, then `.env` at the repo root. See `.env.example`.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/app/core/settings.py -> apps/api/app/core -> app -> apps/api -> apps -> repo
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]


class LLMMode(StrEnum):
    """How Lexora obtains its language-model behaviour.

    ``auto``      use the Anthropic API when ``ANTHROPIC_API_KEY`` is set, else ``offline``.
    ``anthropic`` require the Anthropic API; fail loudly at startup if the key is absent.
    ``offline``   never call a network model. The query gate falls back to a deterministic
                  rewriter/screener and generation to a deterministic extractive composer
                  that quotes retrieved articles verbatim. The retrieval stack, the
                  citation verifier and the refusal gate are fully exercised; only the
                  natural-language phrasing of the final answer differs. Every response
                  produced this way is labelled ``offline-extractive`` end to end, so a
                  number measured in this mode can never be mistaken for a Claude number.
    """

    AUTO = "auto"
    ANTHROPIC = "anthropic"
    OFFLINE = "offline"


class Settings(BaseSettings):
    """Runtime configuration. Immutable once constructed."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="LEXORA_",
        extra="ignore",
        frozen=True,
    )

    # ── paths ────────────────────────────────────────────────────────────────
    repo_root: Path = REPO_ROOT
    corpus_dir: Path = REPO_ROOT / "corpus"
    var_dir: Path = REPO_ROOT / "var"

    # ── embedding (local, ONNX, zero cost) ───────────────────────────────────
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # bge-small-en-v1.5 truncates its input at 512 tokens. Chunking must respect this
    # or the tail of every long chunk is silently dropped from the vector.
    embedding_max_tokens: int = 512
    # bge asks for this prefix on the QUERY side only; documents are embedded bare.
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "

    # ── reranking (local cross-encoder, ONNX) ────────────────────────────────
    # Default chosen by measurement, not by reputation. Benchmarked on this corpus
    # against BAAI/bge-reranker-base (the larger, better-known checkpoint):
    #
    #   model                          hit@1   worst trap score   ms/query
    #   ms-marco-MiniLM-L-6-v2          6/7          -4.55           2314
    #   bge-reranker-base               5/7          -1.95          12452
    #
    # The smaller cross-encoder wins on ranking accuracy, is 5x faster, and — the part
    # that matters most here — separates out-of-corpus questions far more sharply, which
    # is what gives the refusal gate a usable margin. bge-reranker-base stays fully
    # supported: set LEXORA_RERANKER_MODEL to switch. See AUDIT.md "Reranker selection".
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    reranker_backend: Literal["fastembed", "sentence-transformers"] = "fastembed"
    # Cross-encoder cost is linear in padded sequence length. Capping the passage side
    # bounds worst-case latency; 384 leaves the p90 chunk (414 tokens) almost untouched.
    rerank_max_tokens: int = 384
    # Candidates are sorted by length and scored in small batches so a single long
    # passage cannot pad the whole batch up to its length. Verified to produce
    # bit-identical scores to one large batch, for a ~39% latency reduction.
    rerank_batch_size: int = 4

    # ── chunking ─────────────────────────────────────────────────────────────
    chunk_target_tokens: int = 600
    chunk_overlap_tokens: int = 80
    chunk_min_tokens: int = 24

    # ── retrieval ────────────────────────────────────────────────────────────
    dense_top_k: int = 20
    sparse_top_k: int = 20
    rrf_k: int = 60
    fused_top_k: int = 20
    rerank_top_k: int = 5

    # ── refusal gate ─────────────────────────────────────────────────────────
    # Raw cross-encoder logit the best chunk must clear for the corpus to be considered
    # to cover the question. Model-specific — the scales of two rerankers are unrelated —
    # and calibrated against the labelled eval set rather than guessed. Regenerate with
    # `make calibrate` after changing `reranker_model`; see AUDIT.md "Refusal calibration".
    refusal_score_floor: float = -3.6
    # Domain floor on the best DENSE similarity in the candidate set — an orthogonal
    # signal to the cross-encoder. Measured on the labelled set, the bi-encoder separates
    # in-corpus from out-of-corpus questions far better than the cross-encoder does
    # (0.951 vs 0.918 best achievable accuracy), because it captures whether a question
    # belongs to the corpus's subject domain at all rather than how topically relevant
    # the best passage is. See AUDIT.md "Refusal calibration".
    # Set to 0.0 = disabled. A dense floor was fitted (0.706) and REJECTED: it separated
    # the eval set well (0.951 accuracy) but was overfitted to that set's phrasing. Short
    # colloquial queries score low cosine against everything regardless of domain —
    # "How much end-of-service gratuity after 6 years?" scores 0.641 while retrieving
    # Article 51 at rank 1 — so the floor refused correct answers. Kept as a knob, off by
    # default, because the measurement is worth preserving; see AUDIT.md.
    refusal_dense_floor: float = 0.0
    # How many near-miss chunks the amber refusal card shows, to prove it searched.
    refusal_near_miss_count: int = 3

    # ── vector store ─────────────────────────────────────────────────────────
    # Unset -> embedded on-disk Qdrant under var/qdrant (zero credentials, zero cost).
    # Set -> Qdrant Cloud free tier. Identical client API either way.
    qdrant_url: str | None = None
    qdrant_api_key: str | None = None
    qdrant_collection: str = "lexora_chunks"

    # ── models ───────────────────────────────────────────────────────────────
    llm_mode: LLMMode = LLMMode.AUTO
    anthropic_api_key: str | None = None
    answer_model: str = "claude-sonnet-4-6"
    guard_model: str = "claude-haiku-4-5-20251001"
    answer_max_tokens: int = 1200
    guard_max_tokens: int = 400
    # Factual QA over statutes: never sample.
    temperature: float = 0.0
    llm_timeout_s: float = 60.0

    # ── observability ────────────────────────────────────────────────────────
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # ── service ──────────────────────────────────────────────────────────────
    cors_allow_origins: str = "http://localhost:3000"
    rate_limit: str = "10/minute"
    max_question_chars: int = 1000
    max_history_turns: int = 6
    request_timeout_s: float = 30.0

    @field_validator("temperature")
    @classmethod
    def _temperature_must_be_zero_for_factual_qa(cls, v: float) -> float:
        if v != 0.0:
            raise ValueError(
                "Lexora is a grounded QA system over statutes; temperature must be 0. "
                "Sampling trades faithfulness for fluency, which is the exact failure "
                "mode this project exists to eliminate."
            )
        return v

    def model_post_init(self, context: object, /) -> None:
        del context
        if self.chunk_overlap_tokens < 0:
            raise ValueError("chunk_overlap_tokens must be >= 0")
        if self.chunk_overlap_tokens >= self.chunk_target_tokens:
            raise ValueError(
                f"chunk_overlap_tokens ({self.chunk_overlap_tokens}) must be smaller than "
                f"chunk_target_tokens ({self.chunk_target_tokens}); otherwise the splitter "
                "cannot make forward progress."
            )

    # ── derived paths ────────────────────────────────────────────────────────
    @property
    def pdf_dir(self) -> Path:
        return self.corpus_dir / "pdf"

    @property
    def manifest_path(self) -> Path:
        return self.corpus_dir / "manifest.json"

    @property
    def index_dir(self) -> Path:
        return self.var_dir / "index"

    @property
    def qdrant_path(self) -> Path:
        return self.var_dir / "qdrant"

    @property
    def models_cache_dir(self) -> Path:
        return self.var_dir / "models"

    @property
    def chunks_path(self) -> Path:
        """Canonical chunk store: every chunk with full text and metadata."""
        return self.index_dir / "chunks.jsonl"

    @property
    def bm25_path(self) -> Path:
        """Persisted sparse index (tokenised corpus + doc-id order)."""
        return self.index_dir / "bm25.json"

    @property
    def index_meta_path(self) -> Path:
        return self.index_dir / "index_meta.json"

    @property
    def eval_dir(self) -> Path:
        return self.repo_root / "eval"

    @property
    def eval_results_dir(self) -> Path:
        return self.repo_root / "eval" / "results"

    # ── derived behaviour ────────────────────────────────────────────────────
    @property
    def effective_chunk_max_tokens(self) -> int:
        """Hard ceiling on a chunk, in embedding-model tokens.

        A chunk longer than the encoder's context window is truncated *inside* the
        encoder, so its tail contributes nothing to the vector while still being shown
        to the user as if it had been retrieved on its merits. Capping here keeps the
        indexed text and the embedded text identical.
        """
        return min(self.chunk_target_tokens, self.embedding_max_tokens)

    @property
    def use_anthropic(self) -> bool:
        if self.llm_mode is LLMMode.OFFLINE:
            return False
        if self.llm_mode is LLMMode.ANTHROPIC:
            return True
        return bool(self.anthropic_api_key)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
