"""Reciprocal Rank Fusion — the maths the whole retrieval story rests on."""

from __future__ import annotations

import pytest

from app.rag.retrieve import reciprocal_rank_fusion


class TestReciprocalRankFusion:
    def test_single_list_is_monotonically_decreasing(self):
        fused = reciprocal_rank_fusion({"dense": ["a", "b", "c"]}, k=60)
        assert fused["a"] > fused["b"] > fused["c"]

    def test_exact_values_match_the_formula(self):
        """RRF(d) = sum over retrievers of 1 / (k + rank), rank 1-based."""
        fused = reciprocal_rank_fusion({"dense": ["a", "b"]}, k=60)
        assert fused["a"] == pytest.approx(1 / 61)
        assert fused["b"] == pytest.approx(1 / 62)

    def test_agreement_beats_a_single_strong_vote(self):
        """A document both retrievers rank first must outrank one retriever's favourite.

        This is the property that makes fusion worth doing: it rewards agreement rather
        than any single retriever's confidence.
        """
        fused = reciprocal_rank_fusion(
            {"dense": ["both", "dense_only"], "sparse": ["both", "sparse_only"]}, k=60
        )
        assert fused["both"] == pytest.approx(2 / 61)
        assert fused["both"] > fused["dense_only"]
        assert fused["both"] > fused["sparse_only"]

    def test_scores_are_never_compared_across_retrievers(self):
        """Only ranks matter — a retriever's score scale cannot influence the result."""
        a = reciprocal_rank_fusion({"dense": ["x", "y"], "sparse": ["y", "x"]}, k=60)
        b = reciprocal_rank_fusion({"sparse": ["y", "x"], "dense": ["x", "y"]}, k=60)
        assert a == b

    def test_k_damps_the_head_of_each_list(self):
        """Larger k flattens the gap between rank 1 and rank 2."""
        small = reciprocal_rank_fusion({"d": ["a", "b"]}, k=1)
        large = reciprocal_rank_fusion({"d": ["a", "b"]}, k=1000)
        assert (small["a"] - small["b"]) > (large["a"] - large["b"])

    def test_empty_input_yields_empty_output(self):
        assert reciprocal_rank_fusion({}, k=60) == {}
        assert reciprocal_rank_fusion({"dense": []}, k=60) == {}

    @pytest.mark.parametrize("k", [0, -1, -60])
    def test_rejects_k_below_one(self, k: int):
        """k=-1 divides by zero at rank 1 and small k inverts the ordering."""
        with pytest.raises(ValueError, match="RRF k must be"):
            reciprocal_rank_fusion({"dense": ["a"]}, k=k)

    def test_missing_from_one_list_still_scores(self):
        fused = reciprocal_rank_fusion({"dense": ["a"], "sparse": ["b"]}, k=60)
        assert fused["a"] == pytest.approx(1 / 61)
        assert fused["b"] == pytest.approx(1 / 61)
