"""Citation verifier — the last line of defence against a fabricated reference."""

from __future__ import annotations

import pytest

from app.core.models import CitationStatus
from app.rag.verify import extract_citations, find_uncited_claims, verify_answer
from tests.conftest import make_chunk, make_scored


@pytest.fixture
def evidence():
    return [
        make_scored(make_chunk(article_no=51, law_label="Labour Law")),
        make_scored(make_chunk(article_no=30, law_label="Labour Law")),
        make_scored(make_chunk(article_no=25, law_label="Tenancy Law", law_id="dubai-tenancy-law")),
    ]


class TestExtractCitations:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("x [Labour Law, Article 51] y", ("Labour Law", 51)),
            ("x [Labour Law, Art. 51] y", ("Labour Law", 51)),
            ("x [Labour Law, Art 51] y", ("Labour Law", 51)),
            ("x [labour law , article  (51) ] y", ("labour law", 51)),
            ("x [Tenancy Law, Article 25] y", ("Tenancy Law", 25)),
        ],
    )
    def test_accepts_the_tolerated_forms(self, text: str, expected: tuple[str, int]):
        """A verifier that only recognises perfect formatting silently passes bad output."""
        found = extract_citations(text)
        assert len(found) == 1
        assert (found[0][1], found[0][2]) == expected

    def test_ignores_prose_that_is_not_a_citation(self):
        assert extract_citations("Article 51 says something without brackets") == []
        assert extract_citations("[see the annex]") == []


class TestVerifyAnswer:
    def test_citation_present_in_context_is_verified(self, evidence):
        report = verify_answer("Gratuity is payable [Labour Law, Article 51].", evidence)
        assert report.passed
        assert report.verified_count == 1
        assert report.unsupported_count == 0
        assert report.citations[0].chunk_id is not None

    def test_fabricated_article_is_flagged_not_dropped(self, evidence):
        """Article 99 was never retrieved. The answer is flagged, and the citation kept.

        Silently deleting it would hand the reader a redacted answer with no way to know
        something was removed.
        """
        report = verify_answer("It says X [Labour Law, Article 99].", evidence)
        assert not report.passed
        assert report.unsupported_count == 1
        assert report.citations[0].status is CitationStatus.UNSUPPORTED
        assert report.citations[0].chunk_id is None

    def test_right_article_wrong_law_is_unsupported(self, evidence):
        """Article 51 exists — in the Labour Law, not the Tenancy Law."""
        report = verify_answer("See [Tenancy Law, Article 51].", evidence)
        assert not report.passed
        assert report.unsupported_count == 1

    def test_label_matching_ignores_case_and_punctuation(self, evidence):
        report = verify_answer("See [labour-law, Article 51].", evidence)
        assert report.passed

    def test_matches_at_article_level_not_chunk_level(self):
        """A model given chunk 2 of Article 30 and citing Article 30 has cited correctly."""
        chunk = make_chunk(article_no=30, text="Labour Law - Article 30 (2/2)\ntail of the article")
        report = verify_answer("See [Labour Law, Article 30].", [make_scored(chunk)])
        assert report.passed

    def test_no_citations_at_all_still_passes_but_reports_uncited(self, evidence):
        """Zero citations is not a fabrication; it is reported as unverified prose."""
        report = verify_answer("The law is generally quite protective of workers here.", evidence)
        assert report.passed
        assert report.verified_count == 0
        assert report.uncited_sentences

    def test_empty_evidence_makes_every_citation_unsupported(self):
        report = verify_answer("See [Labour Law, Article 51].", [])
        assert not report.passed
        assert report.unsupported_count == 1


class TestUncitedClaims:
    def test_short_fragments_are_not_claims(self):
        assert find_uncited_claims("Yes. No. Maybe.") == []

    def test_cited_sentences_are_not_reported(self):
        text = "A worker gets thirty days of annual leave each year [Labour Law, Article 29]."
        assert find_uncited_claims(text) == []

    def test_uncited_assertion_is_reported(self):
        text = "The employer must always pay double wages on public holidays without exception."
        assert len(find_uncited_claims(text)) == 1
