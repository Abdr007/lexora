"""Parser tests, asserted against the real government PDFs.

Each test pins a property that was found by inspecting the actual files, so a regression
in extraction shows up as a named failure rather than as quietly worse retrieval.
"""

from __future__ import annotations

import re

import pytest

from app.rag.parse import (
    ARTICLE_HEADING_RE,
    INSTRUMENT_TITLE_RE,
    LIGATURE_REPAIRS,
    WIDE_GLYPH_RATIO,
    ParsedDocument,
    _instrument_key,
    _normalise,
)

pytestmark = pytest.mark.integration


def test_article_counts_match_the_published_instruments(parsed: list[ParsedDocument]):
    """The Labour Law has 74 articles; Cabinet Resolution 1/2022 has 39; Law 26/2007 has 37."""
    by_law = {d.law_id: d for d in parsed}
    labour = by_law["uae-labour-law"]
    decree = [a for a in labour.articles if "cabinet" not in a.part_id]
    cabinet = [a for a in labour.articles if "cabinet" in a.part_id]
    assert len(decree) == 74
    assert len(cabinet) == 39
    assert {a.article_no for a in decree} == set(range(1, 75))
    assert {a.article_no for a in cabinet} == set(range(1, 40))
    assert len(by_law["dubai-tenancy-law"].articles) == 37
    assert len(by_law["dubai-rent-decree"].articles) == 4


def test_contents_cross_check_passes(diagnostics):
    """The documents' own tables of contents agree with what the body pass produced."""
    assert diagnostics.contents_missing_in_body == []
    assert diagnostics.contents_extra_in_body == []
    assert diagnostics.contents_check_passed
    assert diagnostics.contents_entries_seen == 113


def test_broken_th_ligature_is_repaired(parsed: list[ParsedDocument], diagnostics):
    """The Labour Law's font maps the `Th` ligature to a bare `T`; every one is restored.

    Without the repair the corpus reads "Te employer shall" throughout, which damages both
    BM25 (the token is wrong) and the embedding (the word is wrong).
    """
    broken = re.compile(
        r"\b(?:Te|Tis|Tese|Tose|Teir|Tere|Tey|Tem|Tus|Terefore|Tereof|Tereto|Terein)\b"
    )
    for document in parsed:
        for article in document.articles:
            assert not broken.search(article.text), (
                f"{document.law_id} art {article.article_no}: unrepaired ligature"
            )
    assert diagnostics.ligature_repairs > 300
    assert LIGATURE_REPAIRS == {"T": "Th"}
    assert 0.7 < WIDE_GLYPH_RATIO < 0.9


def test_the_word_the_survives(parsed: list[ParsedDocument]):
    """A repair that produced nothing would also pass the negative test above."""
    text = " ".join(a.text for d in parsed for a in d.articles)
    assert text.count("The ") > 250


def test_two_instruments_are_split_into_parts(parsed: list[ParsedDocument]):
    """One PDF holds the Decree-Law and Cabinet Resolution; their article numbers collide."""
    labour = next(d for d in parsed if d.law_id == "uae-labour-law")
    parts = {a.part_id for a in labour.articles}
    assert len(parts) == 2
    keys = {a.citation_key for a in labour.articles}
    assert len(keys) == len(labour.articles), "citation keys must be unique"


def test_amendment_duplicate_article_is_split(parsed: list[ParsedDocument]):
    """Law 33/2008 restates Article 2 of the law it amends AND has its own Article 2."""
    amendment = next(d for d in parsed if d.law_id == "dubai-tenancy-amendment")
    twos = [a for a in amendment.articles if a.article_no == 2]
    assert len(twos) == 2
    assert twos[0].part_id != twos[1].part_id


def test_missing_heading_is_recovered(parsed: list[ParsedDocument], diagnostics):
    """Cabinet Resolution Article 2 has no printed label in the source PDF."""
    labour = next(d for d in parsed if d.law_id == "uae-labour-law")
    article = next(a for a in labour.articles if a.article_no == 2 and "cabinet" in a.part_id)
    assert article.title == "Classification of Establishments"
    assert "classified" in article.text.lower()
    assert diagnostics.inferred_headings == 1


def test_no_article_body_was_eaten_by_the_title(parsed: list[ParsedDocument]):
    """A justified first line must not be mistaken for a centred title.

    Regression test for a real defect: Decree 43/2013 Article 1 lost its opening line
    ("When renewing Real Property Lease Contracts...") to the title field.
    """
    decree = next(d for d in parsed if d.law_id == "dubai-rent-decree")
    article = next(a for a in decree.articles if a.article_no == 1)
    assert article.title == "Percentages of Increase"
    assert article.text.startswith("When renewing Real Property Lease Contracts")


def test_definitions_table_reads_term_then_definition(parsed: list[ParsedDocument]):
    """Sub-pixel y differences must not invert a two-column definitions table."""
    labour = next(d for d in parsed if d.law_id == "uae-labour-law")
    article = next(a for a in labour.articles if a.article_no == 1 and "cabinet" not in a.part_id)
    assert "State: United Arab Emirates." in article.text
    assert "Ministry: Ministry of Human Resources" in article.text


def test_sections_carry_forward_within_a_part(parsed: list[ParsedDocument]):
    """Dubai Law 26/2007 prints chapter headings above the first article of each chapter."""
    tenancy = next(d for d in parsed if d.law_id == "dubai-tenancy-law")
    by_no = {a.article_no: a for a in tenancy.articles}
    assert by_no[5].title == "Term of Lease Contract"
    assert by_no[6].title == ""
    assert by_no[6].section == "Term of Lease Contract"


def test_pages_are_recorded_and_ordered(parsed: list[ParsedDocument]):
    for document in parsed:
        for article in document.articles:
            assert 1 <= article.page_start <= article.page_end <= document.page_count


class TestHeadingRegex:
    """The anchoring is what keeps in-body cross-references out of the heading set."""

    @pytest.mark.parametrize(
        "line", ["Article (5)", "Article 5", " Article (54) * ", "ARTICLE (12)", "Article (7):"]
    )
    def test_accepts_real_headings(self, line: str):
        assert ARTICLE_HEADING_RE.match(line)

    @pytest.mark.parametrize(
        "line",
        [
            "Article (9) of this Law.",
            "in accordance with Article (5) of the Decree-Law",
            "Articles (2), (3), (4) of the Original Law are superseded",
            "Article (58-64)",
        ],
    )
    def test_rejects_cross_references(self, line: str):
        assert ARTICLE_HEADING_RE.match(line) is None


class TestInstrumentTitle:
    @pytest.mark.parametrize(
        "line",
        [
            "Law No. (26) of 2007",
            "Federal Decree-Law No. (33) of 2021",
            "Cabinet Resolution No. (1) Of 2022",
            "Decree No. (43) of 2013",
        ],
    )
    def test_accepts_bare_titles(self, line: str):
        assert INSTRUMENT_TITLE_RE.match(line)

    @pytest.mark.parametrize(
        "line",
        [
            "Law No. (16) of 2007 Establishing the Real Estate Regulatory Agency,",
            "Federal Law No. (5) of 1985 Issuing the Civil Code of the United Arab "
            "Emirates and its",
            "Decree No. (2) of 1993 Establishing a Special Tribunal",
        ],
    )
    def test_rejects_recital_lines(self, line: str):
        """Every Dubai law lists other instruments in its recitals."""
        assert INSTRUMENT_TITLE_RE.match(line) is None

    def test_instrument_key_is_canonical(self):
        assert _instrument_key("Law No. (26) of 2007") == ("law", 26, 2007)
        assert _instrument_key("Federal Decree-Law No. 33 of 2021 Regarding X") == (
            "federaldecreelaw",
            33,
            2021,
        )
        assert _instrument_key("no instrument here") is None


class TestNormalise:
    def test_folds_compatibility_ligatures(self):
        assert _normalise("eﬃciency") == "efficiency"
        assert _normalise("deﬁne") == "define"

    def test_collapses_unicode_spaces_only(self):
        assert _normalise("a b\u200bc") == "a bc"

    def test_preserves_accented_letters(self):
        """Regression guard: a character-class RANGE here would blank Latin/Greek text."""
        assert _normalise("café Ünter Ωmega") == "café Ünter Ωmega"


def test_contents_page_numbers_never_reach_an_article_title(parsed: list[ParsedDocument]):
    """A contents row is ``Article (N) | Title | page``, and the page is not the title.

    ``PAGE_FURNITURE_RE`` dropped a bare page number but not a *range*, so the Labour
    Law's ``Article (58-64) | Penalties | 39-40`` row indexed seven articles under the
    title "Penalties 39-40". Nothing downstream would have rejected that: it is a
    plausible-looking string, and it rode into the chunk text and therefore into BM25.
    """
    titled = [
        (document.law_id, article.article_no, article.title)
        for document in parsed
        for article in document.articles
        if article.title
    ]
    assert titled, "no titles parsed at all"
    contaminated = [entry for entry in titled if any(ch.isdigit() for ch in entry[2])]
    assert contaminated == [], f"page numbers leaked into titles: {contaminated}"

    labour = next(d for d in parsed if d.law_id == "uae-labour-law")
    penalties = [a.title for a in labour.articles if 58 <= a.article_no <= 64]
    assert penalties == ["Penalties"] * len(penalties), penalties
