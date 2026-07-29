"""Reranking and the refusal gate."""

from __future__ import annotations

import pytest

from app.core.settings import Settings
from app.rag.rerank import apply_refusal_gate, passthrough, rerank
from app.rag.scope import check_scope
from tests.conftest import make_chunk, make_scored


def scored(score: float, article_no: int = 51):
    return make_scored(make_chunk(article_no=article_no), rerank_score=score)


class TestRefusalGate:
    def test_covers_when_the_best_passage_clears_the_floor(self, settings: Settings):
        outcome = apply_refusal_gate([scored(2.0), scored(-1.0, 30)], settings)
        assert outcome.covered
        assert outcome.evidence
        assert not outcome.near_misses

    def test_refuses_below_the_relevance_floor(self, settings: Settings):
        low = settings.refusal_score_floor - 1.0
        outcome = apply_refusal_gate([scored(low), scored(low - 1)], settings)
        assert not outcome.covered
        assert outcome.evidence == ()
        assert outcome.near_misses
        assert outcome.signal == "below-relevance-floor"

    def test_refusal_returns_near_misses_so_the_decision_is_auditable(self, settings: Settings):
        low = settings.refusal_score_floor - 5
        candidates = [scored(low - i, 30 + i) for i in range(5)]
        outcome = apply_refusal_gate(candidates, settings)
        assert len(outcome.near_misses) == settings.refusal_near_miss_count

    def test_empty_retrieval_refuses(self, settings: Settings):
        outcome = apply_refusal_gate([], settings)
        assert not outcome.covered
        assert outcome.signal == "empty-retrieval"

    def test_scope_overrides_a_high_score(self, settings: Settings):
        """The whole point: a topically perfect match for the wrong jurisdiction.

        The cross-encoder scores "notice period under Saudi labour law" above many real
        questions, because topically it is a perfect match. Scope must win anyway.
        """
        verdict = check_scope("What is the notice period under Saudi Arabian labour law?")
        outcome = apply_refusal_gate([scored(9.0)], settings, scope=verdict)
        assert not outcome.covered
        assert outcome.signal == "foreign-jurisdiction"
        assert "Saudi Arabia" in outcome.reason

    def test_dense_floor_is_disabled_by_default(self, settings: Settings):
        """It was measured, found to overfit the eval set's phrasing, and turned off."""
        assert settings.refusal_dense_floor == 0.0
        outcome = apply_refusal_gate([scored(2.0)], settings, best_dense=0.10)
        assert outcome.covered

    def test_dense_floor_refuses_when_enabled(self, settings: Settings):
        tuned = settings.model_copy(update={"refusal_dense_floor": 0.7})
        outcome = apply_refusal_gate([scored(2.0)], tuned, best_dense=0.10)
        assert not outcome.covered
        assert outcome.signal == "below-domain-floor"


class TestPassthrough:
    def test_keeps_fused_order_and_assigns_ranks(self, settings: Settings):
        candidates = [scored(0.0, n) for n in (1, 2, 3, 4, 5, 6, 7)]
        kept = passthrough(candidates, settings)
        assert len(kept) == settings.rerank_top_k
        assert [item.final_rank for item in kept] == list(range(1, settings.rerank_top_k + 1))
        assert [item.chunk.article_no for item in kept] == [1, 2, 3, 4, 5]


@pytest.mark.integration
class TestCrossEncoder:
    def test_length_bucketing_matches_a_single_batch(self, settings: Settings, chunks):
        """Bucketing is a pure reordering — it must not change a single score.

        This is what makes the ~39% latency saving free rather than a quality trade.
        """
        from app.rag.rerank import _score

        documents = [chunk.text for chunk in chunks[:12]]
        query = "end of service benefits for a full-time worker"
        bucketed = _score(query, documents, settings)
        unbucketed = _score(query, documents, settings.model_copy(update={"rerank_batch_size": 64}))
        assert bucketed == pytest.approx(unbucketed, abs=1e-4)

    def test_ranks_a_relevant_passage_above_an_irrelevant_one(self, settings: Settings, chunks):
        gratuity = next(c for c in chunks if c.article_no == 51 and c.law_id == "uae-labour-law")
        maternity = next(c for c in chunks if c.article_no == 30 and c.law_id == "uae-labour-law")
        ranked = rerank(
            "how much end of service gratuity",
            [make_scored(maternity), make_scored(gratuity)],
            settings,
        )
        assert ranked[0].chunk.article_no == 51
        assert ranked[0].final_rank == 1

    def test_output_is_deterministic(self, settings: Settings, chunks):
        candidates = [make_scored(chunk) for chunk in chunks[:10]]
        first = rerank("annual leave entitlement", candidates, settings)
        second = rerank("annual leave entitlement", candidates, settings)
        assert [c.chunk.chunk_id for c in first] == [c.chunk.chunk_id for c in second]
