# Corpus

Four official English-language publications of UAE federal and Dubai government bodies.

| File | Instrument | Publisher |
|---|---|---|
| `uae-labour-law-33-2021.pdf` | Federal Decree-Law 33/2021 (as amended) + Cabinet Resolution 1/2022 | MOHRE |
| `dubai-tenancy-law-26-2007.pdf` | Law 26/2007 — Landlords and Tenants | Government of Dubai |
| `dubai-tenancy-amendment-33-2008.pdf` | Law 33/2008 — amending Law 26/2007 | Government of Dubai |
| `dubai-rent-increase-decree-43-2013.pdf` | Decree 43/2013 — Rent Increase | Government of Dubai |

```bash
python corpus/download.py            # fetch and verify against the pinned digests
python corpus/download.py --verify   # verify what is on disk, fetch nothing
python corpus/download.py --pin      # re-pin after reviewing a published change
```

The PDFs are **gitignored**. Only `sources.json` (URLs + SHA-256) and `manifest.json`
(what was actually fetched, and from where) are committed — so the repository does not
redistribute the documents, and cannot silently absorb a changed one.

## Fallback note

`www.mohre.gov.ae` is unreachable from some networks — it geo-restricts and rate-limits —
and `uaelegislation.gov.ae` sits behind a bot challenge that rejects non-browser clients.
For the affected document, `sources.json` carries a `mirror_url` pointing at a Wayback
Machine snapshot **of that exact government file**.

The mirror is only ever tried after the official URL fails. The SHA-256 pin is enforced
either way, and `manifest.json` records which URL served the bytes, so provenance is
never ambiguous. If both are unreachable, the downloader says so and tells you where to
place the file manually rather than proceeding with a partial corpus.

## Swapping the corpus

This is the enterprise pitch, and it is a real path rather than a slogan: add entries to
`sources.json`, run `make corpus && make index`, and the same engine serves your
documents. What is corpus-specific and would need attention:

- `app/rag/parse.py` — the heading and instrument-title regexes assume the
  "Article (N)" convention of UAE/Dubai legislative drafting.
- `app/rag/scope.py` — the jurisdiction list is specific to this corpus's boundaries.
- `eval/questions.jsonl` — labels are worthless against a different corpus. Rebuild them;
  `eval/build_questions.py` verifies every label against the parsed text.
- `LEXORA_REFUSAL_SCORE_FLOOR` — recalibrate with `make calibrate`.

`scripts/glyph_audit.py` re-runs the bimodal glyph-width analysis that found the broken
ligature in the Labour Law, so a new corpus is checked for the same class of defect
before it is trusted.
