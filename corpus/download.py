#!/usr/bin/env python3
"""Fetch the Lexora corpus: official English-language UAE labour and Dubai tenancy law.

Every document listed in ``corpus/sources.json`` is an official publication of a UAE
federal ministry or the Government of Dubai. Nothing here is generated, paraphrased or
synthesised — the retrieval quality claims Lexora makes are only meaningful against the
real statutes.

Usage
-----
    python corpus/download.py            # fetch + verify against pinned digests
    python corpus/download.py --pin      # fetch, then rewrite sources.json with the
                                         # digests actually observed (first-run bootstrap)
    python corpus/download.py --verify   # verify what is already on disk, fetch nothing

Fallback note
-------------
``www.mohre.gov.ae`` is not reachable from every network (it geo-restricts and rate-limits
aggressively), and ``uaelegislation.gov.ae`` sits behind a bot challenge that rejects
non-browser clients. For those documents ``sources.json`` carries a ``mirror_url``
pointing at a Wayback Machine snapshot **of that exact government file**. The mirror is
only ever tried after the official URL fails, the SHA-256 pin is enforced either way, and
``corpus/manifest.json`` records which URL actually served each byte range so provenance
is never ambiguous.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

CORPUS_DIR: Final = Path(__file__).resolve().parent
SOURCES_PATH: Final = CORPUS_DIR / "sources.json"
MANIFEST_PATH: Final = CORPUS_DIR / "manifest.json"
PDF_DIR: Final = CORPUS_DIR / "pdf"

# Government CDNs reject the default urllib agent outright. This is the plain
# identification string of a normal desktop browser; no attempt is made to evade a
# challenge, and a hard failure is reported rather than worked around.
USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_S: Final = 120
PDF_MAGIC: Final = b"%PDF-"
UNPINNED_DIGEST: Final = "0" * 64
MIN_PLAUSIBLE_PDF_BYTES: Final = 8 * 1024
# Width at which a progress line truncates a URL for the terminal.
URL_PREVIEW_CHARS: Final = 96


class CorpusError(RuntimeError):
    """A corpus document could not be fetched or failed verification."""


@dataclass(frozen=True, slots=True)
class Document:
    """One official legal document declared in ``sources.json``."""

    law_id: str
    label: str
    title: str
    jurisdiction: str
    publisher: str
    language: str
    filename: str
    official_url: str
    mirror_url: str | None
    sha256: str
    article_pattern: str
    notes: str

    @property
    def path(self) -> Path:
        return PDF_DIR / self.filename

    @property
    def is_pinned(self) -> bool:
        return self.sha256 != UNPINNED_DIGEST


def load_documents() -> list[Document]:
    """Parse ``sources.json`` into typed records."""
    raw: dict[str, Any] = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    docs: list[Document] = []
    for entry in raw["documents"]:
        docs.append(
            Document(
                law_id=entry["law_id"],
                label=entry["label"],
                title=entry["title"],
                jurisdiction=entry["jurisdiction"],
                publisher=entry["publisher"],
                language=entry["language"],
                filename=entry["filename"],
                official_url=entry["official_url"],
                mirror_url=entry.get("mirror_url"),
                sha256=entry["sha256"],
                article_pattern=entry["article_pattern"],
                notes=entry.get("notes", ""),
            )
        )
    if not docs:
        raise CorpusError("sources.json declares no documents")
    return docs


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(url: str) -> bytes:
    """GET ``url`` and return the body, or raise ``CorpusError``.

    Only https is accepted: the corpus is the trust anchor for every answer Lexora
    gives, so it is never fetched over a channel an attacker could rewrite.
    """
    if not url.lower().startswith("https://"):
        raise CorpusError(f"refusing non-https corpus source: {url}")
    request = urllib.request.Request(  # noqa: S310 - scheme is validated above
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:  # noqa: S310
            body: bytes = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CorpusError(f"{type(exc).__name__}: {exc}") from exc

    if not body.startswith(PDF_MAGIC):
        preview = body[:120].decode("utf-8", errors="replace").replace("\n", " ")
        raise CorpusError(f"response is not a PDF (got {len(body)} bytes: {preview!r})")
    if len(body) < MIN_PLAUSIBLE_PDF_BYTES:
        raise CorpusError(f"PDF is implausibly small ({len(body)} bytes) — likely an error page")
    return body


def fetch_document(doc: Document) -> tuple[bytes, str]:
    """Return ``(pdf_bytes, source_url_used)``, trying the official URL first."""
    attempts: list[tuple[str, str]] = [("official", doc.official_url)]
    if doc.mirror_url:
        attempts.append(("wayback-mirror", doc.mirror_url))

    failures: list[str] = []
    for kind, url in attempts:
        print(
            f"    → {kind}: {url[:URL_PREVIEW_CHARS]}{'…' if len(url) > URL_PREVIEW_CHARS else ''}"
        )
        try:
            body = _fetch(url)
        except CorpusError as exc:
            print(f"      ✗ {exc}")
            failures.append(f"{kind}: {exc}")
            continue
        print(f"      ✓ {len(body):,} bytes")
        return body, url

    raise CorpusError(
        f"could not fetch {doc.law_id} from any source.\n      "
        + "\n      ".join(failures)
        + "\n      See the 'Fallback note' in corpus/download.py — if both the official "
        "host and the archive are unreachable from this network, download the file "
        f"manually from {doc.official_url} and place it at {doc.path}."
    )


def verify_on_disk(doc: Document) -> str:
    """Return the digest of the on-disk copy, raising if absent or mismatched."""
    if not doc.path.exists():
        raise CorpusError(f"missing: {doc.path}")
    digest = sha256_of(doc.path.read_bytes())
    if doc.is_pinned and digest != doc.sha256:
        raise CorpusError(
            f"digest mismatch for {doc.filename}\n"
            f"        expected {doc.sha256}\n"
            f"        actual   {digest}\n"
            "        The published document changed, or the file was tampered with. "
            "Re-run with --pin after reviewing the diff."
        )
    return digest


def write_manifest(records: list[dict[str, Any]]) -> None:
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "document_count": len(records),
        "documents": records,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def pin_digests(observed: dict[str, str]) -> None:
    """Rewrite ``sources.json`` in place with the digests actually observed."""
    raw: dict[str, Any] = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    for entry in raw["documents"]:
        if entry["law_id"] in observed:
            entry["sha256"] = observed[entry["law_id"]]
    SOURCES_PATH.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    print(f"\n  pinned {len(observed)} digest(s) into {SOURCES_PATH.name}")


def _reject_digest_mismatch(doc: Document, digest: str, source_used: str, *, pinning: bool) -> None:
    """Raise when a fetched document does not match its pinned digest."""
    if pinning or not doc.is_pinned or digest == doc.sha256:
        return
    raise CorpusError(
        f"digest mismatch from {source_used}\n"
        f"        expected {doc.sha256}\n        actual   {digest}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--pin", action="store_true", help="rewrite sources.json with observed digests"
    )
    parser.add_argument(
        "--verify", action="store_true", help="verify local files only; fetch nothing"
    )
    parser.add_argument(
        "--force", action="store_true", help="re-download even if the file is present"
    )
    args = parser.parse_args(argv)

    documents = load_documents()
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Lexora corpus — {len(documents)} official document(s)\n")
    records: list[dict[str, Any]] = []
    observed: dict[str, str] = {}
    failed: list[str] = []

    for doc in documents:
        print(f"  [{doc.law_id}] {doc.title[:78]}")
        source_used = "local-cache"
        try:
            if args.verify or (doc.path.exists() and not args.force):
                digest = verify_on_disk(doc)
                print(f"      ✓ on disk, sha256 {digest[:16]}…")
            else:
                body, source_used = fetch_document(doc)
                digest = sha256_of(body)
                _reject_digest_mismatch(doc, digest, source_used, pinning=args.pin)
                doc.path.write_bytes(body)
                print(
                    f"      ✓ saved {doc.path.relative_to(CORPUS_DIR.parent)} sha256 {digest[:16]}…"
                )
        except CorpusError as exc:
            print(f"      ✗ FAILED: {exc}\n")
            failed.append(doc.law_id)
            continue

        observed[doc.law_id] = digest
        records.append(
            {
                "law_id": doc.law_id,
                "label": doc.label,
                "title": doc.title,
                "jurisdiction": doc.jurisdiction,
                "publisher": doc.publisher,
                "language": doc.language,
                "filename": doc.filename,
                "bytes": doc.path.stat().st_size,
                "sha256": digest,
                "official_url": doc.official_url,
                "retrieved_from": source_used,
                "article_pattern": doc.article_pattern,
                "notes": doc.notes,
            }
        )
        print()

    if args.pin and observed:
        pin_digests(observed)

    if records:
        write_manifest(records)
        print(
            f"  wrote {MANIFEST_PATH.relative_to(CORPUS_DIR.parent)} ({len(records)} document(s))"
        )

    if failed:
        print(f"\n  {len(failed)} document(s) FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1

    total = sum(r["bytes"] for r in records)
    print(f"\n  corpus ready — {len(records)} documents, {total / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
