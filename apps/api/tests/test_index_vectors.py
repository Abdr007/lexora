"""The dense vectors as a committed, portable artefact.

Embedding is not batch-invariant: padding length varies with how a batch is composed, so
the order of the reductions varies with it, and re-embedding the same corpus reproduces
the vectors only to ~4e-4 per component. Measured on this build of FastEmbed against the
real 181 chunks, versus the default batch:

    batch_size=8    max abs delta 4.271e-04
    batch_size=16   max abs delta 2.126e-04
    batch_size=32   max abs delta 1.063e-04

That is small, and it is not nothing. These vectors decide which passages the reranker is
handed, and the refusal floor is a fitted threshold on the reranker's score — so a
perturbation here can move a borderline passage across it. Shipping the vectors means the
deployed collection is the one the evaluation scored rather than a near-copy, and it keeps
the ONNX session out of the image build entirely (AUDIT.md §6.5).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.core.settings import Settings
from app.rag.index import load_chunks, load_vectors, write_vectors


@pytest.fixture
def scratch_settings(tmp_path: Path) -> Settings:
    settings = Settings(var_dir=tmp_path)
    settings.index_dir.mkdir(parents=True, exist_ok=True)
    return settings


class TestRoundTrip:
    def test_write_then_load_is_exact(self, scratch_settings: Settings):
        rng = np.random.default_rng(0)
        original = rng.standard_normal(
            (7, scratch_settings.embedding_dim), dtype=np.float32
        ).tolist()

        write_vectors(original, scratch_settings)
        loaded = load_vectors(7, scratch_settings)

        assert loaded is not None
        # float32 in, float32 out: a lossy round-trip would defeat the entire point of
        # committing the file.
        assert np.array_equal(
            np.asarray(loaded, dtype=np.float32), np.asarray(original, dtype=np.float32)
        )

    def test_wrong_dimension_is_refused_on_write(self, scratch_settings: Settings):
        with pytest.raises(ValueError, match="expected"):
            write_vectors([[0.0, 1.0, 2.0]], scratch_settings)


class TestStaleness:
    def test_missing_file_returns_none(self, scratch_settings: Settings):
        assert load_vectors(3, scratch_settings) is None

    def test_count_mismatch_returns_none(self, scratch_settings: Settings):
        """Stale vectors beside fresh chunks would misattribute every citation.

        Returning None re-embeds, which is slow and correct. Using them anyway would be
        fast and wrong, and nothing downstream would notice.
        """
        rng = np.random.default_rng(1)
        write_vectors(
            rng.standard_normal((5, scratch_settings.embedding_dim), dtype=np.float32).tolist(),
            scratch_settings,
        )
        assert load_vectors(6, scratch_settings) is None


class TestCommittedArtefact:
    """The file that actually ships. These guard the repository, not the functions."""

    def test_vectors_are_present_and_match_the_chunks(self, index_available: bool):
        if not index_available:
            pytest.skip("index not built; run `make index`")
        settings = Settings()
        chunks = load_chunks(settings)
        vectors = load_vectors(len(chunks), settings)

        assert vectors is not None, (
            f"{settings.vectors_path} is missing or does not match "
            f"{len(chunks)} chunks — the image would re-embed at build time"
        )
        assert len(vectors) == len(chunks)
        assert all(len(v) == settings.embedding_dim for v in vectors)

    def test_vectors_are_normalised(self, index_available: bool):
        """bge vectors are unit length; the collection uses cosine distance."""
        if not index_available:
            pytest.skip("index not built; run `make index`")
        settings = Settings()
        vectors = load_vectors(len(load_chunks(settings)), settings)
        assert vectors is not None
        norms = np.linalg.norm(np.asarray(vectors, dtype=np.float32), axis=1)
        assert np.allclose(norms, 1.0, atol=1e-3), f"norms range {norms.min()}–{norms.max()}"
