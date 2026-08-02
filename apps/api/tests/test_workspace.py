"""Bring-your-own-document: extraction, isolation, and the citation contract.

Exercised against the real government PDFs in `corpus/pdf`, not a synthesised fixture. A
parser tested on a PDF this project generated proves only that it can read its own
output; the corpus files are the kind of typesetting it actually has to survive, and they
are already pinned by SHA-256.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.models import CITATION_RE
from app.core.settings import Settings
from app.workspace.chunker import chunk_document, coverage, split_sections
from app.workspace.extract import (
    ExtractionError,
    _assert_public_url,
    extract_bytes,
    html_to_text,
    normalise,
)
from app.workspace.store import SessionStore, WorkspaceFullError

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_PDF = REPO_ROOT / "corpus" / "pdf" / "dubai-rent-increase-decree-43-2013.pdf"


@pytest.fixture
def settings() -> Settings:
    return Settings()


class TestExtraction:
    def test_reads_a_real_government_pdf(self, settings: Settings):
        if not REAL_PDF.exists():
            pytest.skip("corpus PDFs not present; run `make corpus`")
        document = extract_bytes(REAL_PDF.read_bytes(), REAL_PDF.name, settings=settings)
        assert document.kind == "pdf"
        assert document.pages
        assert "rent" in document.text.lower()
        # A text-layer PDF must not silently go through OCR: that would be slow, and on a
        # keyless deploy it would fail outright.
        assert document.ocr_engine is None
        assert document.diagnostics["text_layer"] is True

    def test_magic_bytes_beat_a_lying_extension(self, settings: Settings):
        """An extension is a claim by whoever uploaded the file."""
        if not REAL_PDF.exists():
            pytest.skip("corpus PDFs not present; run `make corpus`")
        document = extract_bytes(REAL_PDF.read_bytes(), "actually-a-pdf.txt", settings=settings)
        assert document.kind == "pdf"

    def test_plain_text_and_markdown(self, settings: Settings):
        document = extract_bytes(
            b"# Terms\n\nThe notice period is 30 days.", "t.md", settings=settings
        )
        assert document.kind == "text"
        assert "notice period" in document.text

    def test_empty_file_is_refused(self, settings: Settings):
        with pytest.raises(ExtractionError, match="empty"):
            extract_bytes(b"", "empty.txt", settings=settings)

    def test_oversized_file_is_refused(self, settings: Settings):
        payload = b"x" * (settings.workspace_max_bytes + 1)
        with pytest.raises(ExtractionError, match="larger than"):
            extract_bytes(payload, "big.txt", settings=settings)

    def test_unsupported_type_names_what_is_accepted(self, settings: Settings):
        with pytest.raises(ExtractionError, match="not supported"):
            extract_bytes(b"\x00\x01binary", "thing.exe", settings=settings)

    def test_html_drops_script_and_keeps_text(self):
        title, text = html_to_text(
            "<html><head><title>Lease</title><script>alert(1)</script></head>"
            "<body><p>Rent is due monthly.</p></body></html>"
        )
        assert title == "Lease"
        assert "Rent is due monthly." in text
        assert "alert" not in text

    def test_normalise_removes_invisible_characters(self):
        """Zero-width characters survive into chunks and break exact-term matching."""
        assert normalise("noti\u200bce") == "notice"


class TestUrlDefences:
    """The server fetches a URL a stranger supplied. That is an SSRF primitive."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://localhost:8080/",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://[::1]/",
        ],
    )
    def test_private_and_metadata_addresses_are_refused(self, url: str):
        with pytest.raises(ExtractionError):
            _assert_public_url(url)

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://host/f"])
    def test_non_http_schemes_are_refused(self, url: str):
        with pytest.raises(ExtractionError, match="http"):
            _assert_public_url(url)


class TestChunking:
    def test_headings_become_sections(self, settings: Settings):
        document = extract_bytes(
            b"# Termination\n\nEither party may end this agreement with 30 days notice.\n\n"
            b"# Payment\n\nRent is payable monthly in advance without deduction.\n",
            "lease.md",
            settings=settings,
        )
        sections = split_sections(document)
        assert [s.title for s in sections] == ["Termination", "Payment"]

    def test_chunks_carry_the_right_unit_and_cite_correctly(self, settings: Settings):
        document = extract_bytes(
            b"# Termination\n\nEither party may end this agreement with 30 days written notice "
            b"delivered to the address stated in the schedule.\n",
            "lease.md",
            settings=settings,
        )
        chunks = chunk_document(document, "doc-test", settings)
        assert chunks
        assert chunks[0].unit_label == "Section"
        # The citation the model is asked to emit must be one the verifier can parse.
        assert CITATION_RE.search(chunks[0].display_citation) is not None

    def test_structureless_text_falls_back_to_pages(self, settings: Settings):
        document = extract_bytes(
            b"the quick brown fox jumps over the lazy dog again and again and again",
            "n.txt",
            settings=settings,
        )
        chunks = chunk_document(document, "doc-flat", settings)
        assert chunks
        assert chunks[0].unit_label == "Page"


class TestCitationPattern:
    """This regression dropped every citation on every uploaded document, silently."""

    @pytest.mark.parametrize(
        ("text", "unit", "number"),
        [
            ("[Labour Law, Article 51]", "Article", 51),
            ("[Labour Law, Art. 51]", "Art.", 51),
            ("[Labour Law, Article (30)]", "Article", 30),
            ("[lease.pdf, Section 4]", "Section", 4),
            ("[contract_2024.pdf, Page 2]", "Page", 2),
            ("[my-file.docx, Clause 12]", "Clause", 12),
        ],
    )
    def test_units_and_filenames_are_recognised(self, text: str, unit: str, number: int):
        match = CITATION_RE.search(text)
        assert match is not None, f"{text} did not parse — its citation would be dropped"
        assert match.group("unit").lower() == unit.lower()
        assert int(match.group("article")) == number

    def test_prose_is_not_mistaken_for_a_citation(self):
        assert CITATION_RE.search("[see the schedule attached]") is None


class TestSessionIsolation:
    """A session id is the only thing separating one person's contract from another's."""

    def _doc(self, settings: Settings, body: bytes, name: str):
        document = extract_bytes(body, name, settings=settings)
        return document, chunk_document(document, f"doc-{name}", settings)

    def test_documents_do_not_leak_between_sessions(self, settings: Settings):
        store = SessionStore(settings)
        document, chunks = self._doc(settings, b"# A\n\n" + b"secret clause text " * 10, "a.md")

        first = store.ensure(None)
        store.add(first.session_id, document, chunks, "doc-a")
        second = store.ensure(None)

        assert first.session_id != second.session_id
        loaded_first = store.get(first.session_id)
        loaded_second = store.get(second.session_id)
        assert loaded_first is not None
        assert loaded_second is not None
        assert len(loaded_first.documents) == 1
        assert len(loaded_second.documents) == 0

    def test_unknown_session_id_creates_an_empty_one(self, settings: Settings):
        store = SessionStore(settings)
        session = store.ensure("not-a-real-session-id")
        assert session.documents == {}

    def test_document_cap_is_enforced(self, settings: Settings):
        capped = settings.model_copy(update={"workspace_max_docs": 2})
        store = SessionStore(capped)
        session = store.ensure(None)
        for index in range(2):
            document, chunks = self._doc(
                capped, b"# H\n\n" + b"clause body text " * 10, f"{index}.md"
            )
            store.add(session.session_id, document, chunks, f"doc-{index}")
        document, chunks = self._doc(capped, b"# H\n\n" + b"clause body text " * 10, "x.md")
        with pytest.raises(WorkspaceFullError, match="already holds"):
            store.add(session.session_id, document, chunks, "doc-x")

    def test_idle_sessions_are_purged(self, settings: Settings):
        expiring = settings.model_copy(update={"workspace_session_ttl_s": 0.05})
        store = SessionStore(expiring)
        session = store.ensure(None)
        time.sleep(0.1)
        assert store.get(session.session_id) is None

    def test_clear_actually_forgets(self, settings: Settings):
        store = SessionStore(settings)
        document, chunks = self._doc(settings, b"# A\n\n" + b"clause body text " * 10, "a.md")
        session = store.ensure(None)
        store.add(session.session_id, document, chunks, "doc-a")
        assert store.clear(session.session_id) is True
        assert store.get(session.session_id) is None


class TestCoverage:
    """Every line of an uploaded document must reach the index.

    Chunking is the one stage that can lose text without failing. A dropped paragraph does
    not raise — it simply never becomes searchable, and the only symptom is a question that
    should have been answerable coming back refused. Nothing downstream can detect it,
    because nothing downstream ever saw the missing words.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "dubai-rent-increase-decree-43-2013.pdf",
            "dubai-tenancy-amendment-33-2008.pdf",
            "dubai-tenancy-law-26-2007.pdf",
            "uae-labour-law-33-2021.pdf",
        ],
    )
    def test_real_pdfs_are_fully_covered(self, name: str, settings: Settings):
        pdf = REPO_ROOT / "corpus" / "pdf" / name
        if not pdf.exists():
            pytest.skip("corpus PDFs not present; run `make corpus`")
        document = extract_bytes(pdf.read_bytes(), name, settings=settings)
        chunks = chunk_document(document, "doc-cov", settings)
        kept = coverage(document, chunks)
        assert kept == 1.0, f"{name} lost {(1 - kept):.2%} of its words during chunking"

    def test_a_short_opening_paragraph_survives(self, settings: Settings):
        """The regression: a first section under the merge threshold was dropped outright.

        It could not merge backwards — there was nothing before it — and the branch that
        would have kept it required a previous section to exist.
        """
        document = extract_bytes(
            b"Short intro.\n\n# Terms\n\n" + b"the clause body continues " * 20,
            "s.md",
            settings=settings,
        )
        chunks = chunk_document(document, "doc-short", settings)
        assert coverage(document, chunks) == 1.0
        assert "Short intro." in chunks[0].text

    def test_heading_words_reach_the_index(self, settings: Settings):
        """`_match_heading` returns a cleaned label; the raw line has to survive too."""
        document = extract_bytes(
            b"## Article (5) Definitions and Interpretation\n\n"
            + b"the defined terms are set out below and apply throughout " * 6,
            "h.md",
            settings=settings,
        )
        chunks = chunk_document(document, "doc-head", settings)
        assert coverage(document, chunks) == 1.0
        joined = " ".join(chunk.text for chunk in chunks)
        assert "Interpretation" in joined


class TestCitableNumbering:
    """A citation must point at something the reader can find in their own document.

    `article_no` used to be the reading-order index, so the UDHR -- 30 printed articles,
    36+ detected headings -- put the text of Article 24 under number 35. The model read
    "Article 24" from the passage body and cited it correctly; no chunk carried 24, so
    the verifier marked a correct citation unsupported and the UI flagged it red.
    """

    def _doc(self, text: str):
        from app.workspace.extract import ExtractedDocument, ExtractedPage

        return ExtractedDocument(
            title="sample.pdf",
            source="sample.pdf",
            kind="pdf",
            pages=(ExtractedPage(page_no=1, text=text),),
        )

    def test_printed_number_wins_over_reading_order(self, settings: Settings):
        text = (
            "PREAMBLE\nWhereas recognition of the inherent dignity of all members.\n\n"
            "WHEREAS DISREGARD\nBarbarous acts have outraged the conscience of mankind.\n\n"
            "Article 24\nEveryone has the right to rest and leisure, including reasonable "
            "limitation of working hours and periodic holidays with pay.\n"
        )
        chunks = chunk_document(self._doc(text), "doc-x", settings)
        rest = [c for c in chunks if "rest and leisure" in c.text.lower()]
        assert rest, "the article text was lost"
        assert rest[0].article_no == 24, (
            f"cited as {rest[0].article_no}; the document prints 'Article 24'"
        )

    def test_repeated_numbers_do_not_collide(self, settings: Settings):
        """Two 'Article 1's must not resolve to the same citation."""
        text = (
            "Article 1\nThe first agreement covers the initial term of the lease.\n\n"
            "Article 2\nThe second clause covers renewal and its associated notice.\n\n"
            "Article 1\nAppendix restatement of the first article for reference only.\n"
        )
        chunks = chunk_document(self._doc(text), "doc-y", settings)
        numbers = [c.article_no for c in chunks]
        assert len(numbers) == len(set(numbers)), f"colliding article numbers: {numbers}"
        # 2 is unique and printed, so it must survive as 2.
        assert 2 in numbers

    def test_unnumbered_sections_still_get_a_number(self, settings: Settings):
        text = (
            "INTRODUCTION\nThis policy sets out how the company handles annual leave.\n\n"
            "Article 7\nEmployees accrue leave monthly across the whole calendar year.\n"
        )
        chunks = chunk_document(self._doc(text), "doc-z", settings)
        numbers = [c.article_no for c in chunks]
        assert all(n > 0 for n in numbers)
        assert len(numbers) == len(set(numbers))
        assert 7 in numbers, "a printed number was lost"
