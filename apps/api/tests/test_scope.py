"""Jurisdiction scope — the signal no similarity score can provide."""

from __future__ import annotations

import pytest

from app.rag.scope import check_scope


class TestOutOfScope:
    @pytest.mark.parametrize(
        "question",
        [
            "What is the notice period under Saudi Arabian labour law?",
            "How does Qatar regulate end of service benefits?",
            "What is the capital gains tax rate in Singapore?",
            "What are the visa requirements for tourists visiting Japan?",
            "What does UK employment law say about notice?",
        ],
    )
    def test_foreign_jurisdictions(self, question: str):
        verdict = check_scope(question)
        assert verdict is not None
        assert verdict.signal == "foreign-jurisdiction"

    @pytest.mark.parametrize(
        "question",
        [
            "What does DIFC Employment Law say about end-of-service gratuity?",
            "What is the process for company liquidation in the DIFC?",
            "How does the ADGM treat probation periods?",
        ],
    )
    def test_free_zones_with_their_own_regime(self, question: str):
        verdict = check_scope(question)
        assert verdict is not None
        assert verdict.signal == "free-zone-jurisdiction"

    def test_other_emirates_only_for_tenancy(self):
        """Dubai Law 26/2007 is Dubai-only, so an Abu Dhabi tenancy question is out."""
        verdict = check_scope("What are the rent increase rules for tenants in Abu Dhabi?")
        assert verdict is not None
        assert verdict.signal == "other-emirate-tenancy"

    def test_the_reason_names_the_jurisdiction(self):
        verdict = check_scope("What is the notice period under Saudi Arabian labour law?")
        assert verdict is not None
        assert "Saudi Arabia" in verdict.reason


class TestInScope:
    @pytest.mark.parametrize(
        "question",
        [
            "How much end-of-service gratuity after 6 years?",
            "What does Article 30 say about maternity leave?",
            "Can my landlord increase the rent when I renew?",
            "When can a landlord evict a tenant in Dubai?",
            "Is there a statutory minimum wage in the UAE?",
        ],
    )
    def test_ordinary_questions_pass(self, question: str):
        assert check_scope(question) is None

    def test_labour_law_is_federal_so_other_emirates_stay_in_scope(self):
        """The labour law applies in every emirate; only tenancy is Dubai-specific.

        Firing on "Abu Dhabi" alone would wrongly refuse a valid federal-law question.
        """
        assert check_scope("How many working hours per day are allowed in Abu Dhabi?") is None
        assert check_scope("What is the probation period for a job in Sharjah?") is None

    def test_topic_is_never_used_to_refuse(self):
        """A minimum-wage question is answerable ("the Cabinet may set one").

        A topic blocklist would have suppressed a correct grounded answer, which is why
        the check is restricted to jurisdictions.
        """
        assert check_scope("Is there a minimum wage?") is None


class TestDegenerateInput:
    """Input that is not language at all.

    A cross-encoder scores emoji and bare numbers against legal passages, and the scores
    are not small: "🙂🙂🙂" measured -1.16 and "42" measured +1.83 against a refusal floor
    of -3.6, so both were answered with citations before this check existed. No threshold
    can fix that, because the score is meaningless for input that is not language.
    """

    @pytest.mark.parametrize(
        "question", ["🙂🙂🙂", "42", "...", "?", "q", "....  ....", "٣٤", "  -  ", "1000"]
    )
    def test_input_without_words_is_refused(self, question: str):
        verdict = check_scope(question)
        assert verdict is not None, f"{question!r} must not reach retrieval"
        assert verdict.signal == "no-answerable-content"

    @pytest.mark.parametrize(
        "question",
        [
            "Is there a minimum wage?",
            "What does Article 30 say?",
            "ما هي مكافأة نهاية الخدمة؟",
            "Do I get 30 days?",
        ],
    )
    def test_real_questions_are_untouched(self, question: str):
        """Including Arabic: the rule counts Unicode letters, not ASCII ones."""
        verdict = check_scope(question)
        assert verdict is None or verdict.signal != "no-answerable-content"
