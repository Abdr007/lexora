#!/usr/bin/env python3
"""Build ``eval/questions.jsonl`` and verify every label against the real corpus.

The question set is hand-written, not model-generated. A labelled set produced by the
same family of model that is later judged against it measures agreement, not correctness,
and the whole point of this harness is to have ground truth that is independent of the
system under test.

What this script does add is *verification*: every ``(law_id, article_no)`` label is
checked to exist in the parsed corpus, and every answerable question's expected keywords
are checked to actually occur in the cited article. A typo'd label would otherwise show
up as a retrieval failure and quietly understate the system.

Run with ``python eval/build_questions.py`` (also runs as part of ``make eval``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final, NamedTuple

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
# Both the repo root and the API package must be importable: these scripts import
# `eval.*` (sibling modules) and `app.*` (the service). Doing it here rather than
# relying on PYTHONPATH means the script runs correctly from any working directory
# and under any runner — a missing PYTHONPATH previously broke the CI eval step only.
for _path in (REPO_ROOT, REPO_ROOT / "apps" / "api"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.rag.parse import ParseDiagnostics, parse_corpus  # noqa: E402

OUTPUT: Final = REPO_ROOT / "eval" / "questions.jsonl"

LABOUR: Final = "uae-labour-law"
TENANCY: Final = "dubai-tenancy-law"
AMEND: Final = "dubai-tenancy-amendment"
RENT: Final = "dubai-rent-decree"

DECREE: Final = "decree-law"
CABINET: Final = "cabinet-resolution"


class Q(NamedTuple):
    """One labelled question.

    ``must_contain`` are terms that appear in the cited article; they are used only to
    validate the label at build time, never to score the system.
    """

    question: str
    law_id: str
    article_no: int
    part: str | None
    answer: str
    must_contain: tuple[str, ...]
    tags: tuple[str, ...]


ANSWERABLE: Final[tuple[Q, ...]] = (
    # ── UAE Labour Law, Federal Decree-Law 33/2021 ─────────────────────────
    Q(
        "How much end-of-service gratuity is a full-time foreign worker owed after six years?",
        LABOUR,
        51,
        DECREE,
        "21 days' basic wage for each of the first five years of service and 30 days' basic "
        "wage for each additional year.",
        ("twenty-one days", "thirty day"),
        ("gratuity", "paraphrase"),
    ),
    Q(
        "What is the longest probationary period an employer may impose?",
        LABOUR,
        9,
        DECREE,
        "Six months.",
        ("probationary", "six"),
        ("probation",),
    ),
    Q(
        "How many normal working hours may a worker be required to work per day?",
        LABOUR,
        17,
        DECREE,
        "Eight hours per day, or 48 hours per week.",
        ("eight hours", "forty-eight"),
        ("hours",),
    ),
    Q(
        "How is overtime pay calculated?",
        LABOUR,
        19,
        DECREE,
        "Basic wage for the overtime hours plus at least 25%, rising to 50% when the "
        "overtime falls between 10pm and 4am.",
        ("overtime", "twenty five percent"),
        ("overtime", "pay"),
    ),
    Q(
        "How many days of maternity leave is a female worker entitled to?",
        LABOUR,
        30,
        DECREE,
        "Sixty days: 45 on full wage and 15 on half wage.",
        ("sixty days", "forty-five"),
        ("leave", "maternity"),
    ),
    Q(
        "How much annual leave does a worker accrue?",
        LABOUR,
        29,
        DECREE,
        "Thirty days per year once a year of service is completed.",
        ("annual leave",),
        ("leave", "annual"),
    ),
    Q(
        "What sick leave is a worker entitled to after probation?",
        LABOUR,
        31,
        DECREE,
        "Up to 90 days per year: the first 15 on full wage, the next 30 on half wage and "
        "the remainder unpaid.",
        ("sick leave", "ninety"),
        ("leave", "sick"),
    ),
    Q(
        "What notice period applies when either party terminates an employment contract?",
        LABOUR,
        43,
        DECREE,
        "Between 30 and 90 days, as agreed in the contract.",
        ("notice",),
        ("termination", "notice"),
    ),
    Q(
        "When may an employer dismiss a worker without notice?",
        LABOUR,
        44,
        DECREE,
        "After a written investigation, in defined cases such as assuming a false identity, "
        "a serious error causing loss, or disclosing work secrets.",
        ("without notice",),
        ("termination", "dismissal"),
    ),
    Q(
        "When can a worker resign without giving notice and keep their entitlements?",
        LABOUR,
        45,
        DECREE,
        "Where the employer breaches its obligations, or the worker faces grave danger, "
        "among the listed cases.",
        ("without notice",),
        ("termination", "resignation"),
    ),
    Q(
        "What counts as unlawful termination of a worker's service?",
        LABOUR,
        47,
        DECREE,
        "Termination because the worker filed a serious complaint or brought a claim that "
        "proved valid.",
        ("unlawful",),
        ("termination",),
    ),
    Q(
        "What is the minimum age for employment?",
        LABOUR,
        5,
        DECREE,
        "Fifteen years.",
        ("fifteen years",),
        ("juveniles",),
    ),
    Q(
        "Can an employer enforce a non-competition clause after employment ends?",
        LABOUR,
        10,
        DECREE,
        "Yes, where the work gave access to clients or secrets, limited in time, place and "
        "type of work, and not exceeding two years.",
        ("non-competition", "two years"),
        ("non-compete",),
    ),
    Q(
        "Is discrimination between workers prohibited?",
        LABOUR,
        4,
        DECREE,
        "Yes — discrimination on race, colour, sex, religion, national or social origin or "
        "disability is prohibited, and equal pay applies for the same work.",
        ("discrimination",),
        ("equality",),
    ),
    Q(
        "Can an employer deduct money from a worker's wage?",
        LABOUR,
        25,
        DECREE,
        "Only in the listed cases, and deductions to recover a loan or advance may not "
        "exceed 20% of the wage.",
        ("deduct",),
        ("wages", "deductions"),
    ),
    Q(
        "What disciplinary sanctions may an employer impose?",
        LABOUR,
        39,
        DECREE,
        "A written warning, a fine, suspension without pay, denial of promotion, or "
        "dismissal, in the order set out in the article.",
        ("warning",),
        ("discipline",),
    ),
    Q(
        "For how long may an employer suspend a worker pending an investigation?",
        LABOUR,
        40,
        DECREE,
        "No more than 30 days.",
        ("suspend",),
        ("discipline",),
    ),
    Q(
        "Do workers have to pay court fees to bring a labour claim?",
        LABOUR,
        55,
        DECREE,
        "Labour claims are exempt from judicial fees at all stages where the claim does not "
        "exceed 100,000 dirhams.",
        ("judicial fees",),
        ("disputes",),
    ),
    Q(
        "Does an employer need a permit before hiring someone in the UAE?",
        LABOUR,
        6,
        DECREE,
        "Yes — a work permit from the Ministry is required before recruiting or employing a "
        "worker.",
        ("work permit",),
        ("permits",),
    ),
    Q(
        "Can an employer make a worker hand over their passport?",
        LABOUR,
        14,
        DECREE,
        "No. The employer may not retain the worker's official documents or compel work "
        "against the worker's will.",
        ("forced labour",),
        ("prohibitions",),
    ),
    Q(
        "What paid leave is available for a death in the family?",
        LABOUR,
        32,
        DECREE,
        "Bereavement leave of five days for a spouse and three days for a parent, child, "
        "sibling, grandchild or grandparent.",
        ("bereavement",),
        ("leave",),
    ),
    Q(
        "Can a worker take unpaid leave?",
        LABOUR,
        33,
        DECREE,
        "Yes, with the employer's approval.",
        ("unpaid leave",),
        ("leave",),
    ),
    Q(
        "What happens if a worker abandons their job without a valid reason?",
        LABOUR,
        50,
        DECREE,
        "The employer reports the absence and the worker may be barred from a new work "
        "permit for a period, subject to the Implementing Regulation.",
        ("leaves work",),
        ("absence",),
    ),
    Q(
        "What weekly rest is a worker entitled to?",
        LABOUR,
        21,
        DECREE,
        "A paid weekend of not less than one day.",
        ("weekend",),
        ("hours", "rest"),
    ),
    Q(
        "How long may a worker work continuously before a break?",
        LABOUR,
        18,
        DECREE,
        "No more than five consecutive hours.",
        ("five consecutive hours",),
        ("hours", "rest"),
    ),
    Q(
        "What must an employment contract contain and in how many copies?",
        LABOUR,
        8,
        DECREE,
        "It is made in two copies, one for each party, in the form set by the Implementing "
        "Regulation.",
        ("two copies",),
        ("contract",),
    ),
    Q(
        "What are the recognised patterns of work?",
        LABOUR,
        7,
        DECREE,
        "Full time, part time, temporary work, flexible work, and any further pattern set "
        "by the Implementing Regulation.",
        ("full time", "part time"),
        ("contract", "patterns"),
    ),
    Q(
        "Does the labour law apply to domestic workers?",
        LABOUR,
        3,
        DECREE,
        "No — domestic workers, government employees and the armed forces and police are "
        "outside its scope.",
        ("domestic workers",),
        ("scope",),
    ),
    Q(
        "When must an employer pay a worker's final entitlements after the contract ends?",
        LABOUR,
        53,
        DECREE,
        "Within 14 days of the end of the contract.",
        ("entitlements",),
        ("termination",),
    ),
    Q(
        "Is there a statutory minimum wage in the UAE private sector?",
        LABOUR,
        27,
        DECREE,
        "The Cabinet may set a minimum wage on the Minister's proposal; the Decree-Law "
        "itself does not fix an amount.",
        ("minimum wage",),
        ("wages",),
    ),
    # ── Cabinet Resolution No. 1 of 2022 ───────────────────────────────────
    Q(
        "How is freelance work defined and does it need a permit?",
        LABOUR,
        8,
        CABINET,
        "Freelance is an independent, flexible arrangement without a fixed employer, and "
        "requires a freelance work permit from the Ministry.",
        ("freelance",),
        ("freelance", "cabinet"),
    ),
    Q(
        "How much annual leave does a part-time worker get?",
        LABOUR,
        18,
        CABINET,
        "Pro rata to actual working hours, calculated under the Implementing Regulation.",
        ("part-time",),
        ("leave", "cabinet"),
    ),
    Q(
        "How is end-of-service pay worked out for part-time employees?",
        LABOUR,
        30,
        CABINET,
        "Annual working hours in the contract divided by full-time annual hours, times 100, "
        "applied to the full-time gratuity.",
        ("part-time", "job-sharing"),
        ("gratuity", "cabinet"),
    ),
    Q(
        "What conditions make a non-competition clause enforceable?",
        LABOUR,
        12,
        CABINET,
        "It must define place, term and type of work, last no more than two years, and the "
        "employer must bring a claim within the periods set out.",
        ("non-competition",),
        ("non-compete", "cabinet"),
    ),
    Q(
        "What happens to workers if the employer becomes bankrupt?",
        LABOUR,
        25,
        CABINET,
        "The contract terminates and workers' entitlements are treated under the bankruptcy "
        "rules in the Resolution.",
        ("bankruptcy",),
        ("termination", "cabinet"),
    ),
    Q(
        "What circumstances count as a grave danger allowing a worker to leave?",
        LABOUR,
        26,
        CABINET,
        "Circumstances that threaten the worker's safety or health where the employer knew "
        "and did not act.",
        ("grave danger",),
        ("safety", "cabinet"),
    ),
    # ── Dubai Law No. 26 of 2007 ───────────────────────────────────────────
    Q(
        "Does a Dubai tenancy contract have to be registered?",
        TENANCY,
        4,
        DECREE,
        "Yes — lease contracts must be in writing and registered with RERA.",
        ("RERA", "registered"),
        ("tenancy", "registration"),
    ),
    Q(
        "On what grounds can a landlord evict a tenant before the lease expires?",
        TENANCY,
        25,
        DECREE,
        "Non-payment within 30 days of notice, unauthorised subletting, illegal use, and "
        "the other listed cases.",
        ("eviction",),
        ("tenancy", "eviction"),
    ),
    Q(
        "Can a landlord cut off the water or electricity to force a tenant out?",
        TENANCY,
        34,
        DECREE,
        "No — the landlord may not disconnect services or disturb the tenant's use of the "
        "property.",
        ("disconnect",),
        ("tenancy",),
    ),
    Q(
        "What happens if a tenant stays on after the lease term ends?",
        TENANCY,
        6,
        DECREE,
        "The contract renews for the same term or one year, whichever is shorter, on the "
        "same conditions.",
        ("renewed",),
        ("tenancy", "renewal"),
    ),
    Q(
        "Who pays the fees and taxes on a leased property in Dubai?",
        TENANCY,
        22,
        DECREE,
        "The tenant, unless the lease contract says otherwise.",
        ("fees and taxes",),
        ("tenancy",),
    ),
    Q(
        "Does selling a rented property end the tenant's lease?",
        TENANCY,
        28,
        DECREE,
        "No — transferring ownership does not affect the tenant's right to occupy under a "
        "valid lease.",
        ("ownership",),
        ("tenancy",),
    ),
    # ── Dubai Law No. 33 of 2008 (amendment) ───────────────────────────────
    Q(
        "How much notice is needed to change the terms of a Dubai lease on renewal?",
        AMEND,
        14,
        DECREE,
        "Ninety days before the lease expires, unless the parties agree otherwise.",
        ("ninety",),
        ("tenancy", "renewal"),
    ),
    Q(
        "How much notice must a landlord give to evict a tenant for personal use?",
        AMEND,
        25,
        DECREE,
        "Twelve months' notice through a notary public or registered mail.",
        ("twelve",),
        ("tenancy", "eviction"),
    ),
    # ── Dubai Decree No. 43 of 2013 ────────────────────────────────────────
    Q(
        "By how much can my landlord raise the rent when I renew?",
        RENT,
        1,
        DECREE,
        "Nothing if the rent is within 10% of market, then 5%, 10%, 15% and a maximum of "
        "20% as the gap widens.",
        ("ten percent", "twenty percent"),
        ("tenancy", "rent"),
    ),
    Q(
        "How is the average market rent determined for a rent increase?",
        RENT,
        3,
        DECREE,
        "By the RERA rental index for the Emirate of Dubai.",
        ("average rental value",),
        ("tenancy", "rent"),
    ),
)

# Plausible questions the corpus genuinely does not answer. The set deliberately mixes
# far-out-of-domain (Singapore tax) with near-misses inside UAE law but outside these
# four instruments (DIFC, golden visa, corporate tax) — a refusal gate that only rejects
# the obvious ones is not worth measuring.
TRAPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("What is the capital gains tax rate in Singapore?", ("far",)),
    ("What are the visa requirements for tourists visiting Japan?", ("far",)),
    ("How do I open a corporate bank account in the UAE?", ("near", "uae")),
    ("How do I apply for a UAE golden visa?", ("near", "uae")),
    ("What is the UAE corporate tax rate for free zone companies?", ("near", "uae")),
    ("What does DIFC Employment Law say about end-of-service gratuity?", ("near", "difc")),
    ("How do I register a trademark in Dubai?", ("near", "dubai")),
    ("What are the rules for short-term holiday home rentals in Dubai?", ("near", "dubai")),
    ("What is the penalty for speeding on Sheikh Zayed Road?", ("near", "dubai")),
    ("How do I obtain a liquor licence in Dubai?", ("near", "dubai")),
    ("What health insurance must an employer provide in Abu Dhabi?", ("near", "uae")),
    ("What is the notice period under Saudi Arabian labour law?", ("near", "gcc")),
    ("How are owners association service charges regulated in Dubai?", ("near", "dubai")),
    ("What is the process for company liquidation in the DIFC?", ("near", "difc")),
    ("Which bank offers the best mortgage rate in Dubai?", ("far", "commercial")),
)


def build() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, q in enumerate(ANSWERABLE, start=1):
        rows.append(
            {
                "id": f"a{index:03d}",
                "question": q.question,
                "answerable": True,
                "answer": q.answer,
                "law_id": q.law_id,
                "article_no": q.article_no,
                "part": q.part,
                "tags": list(q.tags),
            }
        )
    for index, (question, tags) in enumerate(TRAPS, start=1):
        rows.append(
            {
                "id": f"t{index:03d}",
                "question": question,
                "answerable": False,
                "answer": "Not covered by the indexed corpus.",
                "law_id": None,
                "article_no": None,
                "part": None,
                "tags": list(tags),
            }
        )
    return rows


def verify(rows: list[dict[str, Any]]) -> list[str]:
    """Check every label against the parsed corpus. Returns a list of problems."""
    documents = parse_corpus(diagnostics=ParseDiagnostics())
    index: dict[tuple[str, int, str], str] = {}
    for document in documents:
        for article in document.articles:
            part = CABINET if "cabinet" in article.part_id else DECREE
            index[(document.law_id, article.article_no, part)] = article.text.lower()

    problems: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row["question"].strip().lower() in seen:
            problems.append(f"{row['id']}: duplicate question")
        seen.add(row["question"].strip().lower())
        if not row["answerable"]:
            continue
        key = (row["law_id"], row["article_no"], row["part"])
        text = index.get(key)
        if text is None:
            problems.append(f"{row['id']}: no such article {key}")
    return problems


def verify_keywords() -> list[str]:
    """Check that each answerable question's expected terms occur in the cited article."""
    documents = parse_corpus(diagnostics=ParseDiagnostics())
    index: dict[tuple[str, int, str], str] = {}
    for document in documents:
        for article in document.articles:
            part = CABINET if "cabinet" in article.part_id else DECREE
            index[(document.law_id, article.article_no, part)] = article.text.lower()

    problems: list[str] = []
    for position, q in enumerate(ANSWERABLE, start=1):
        text = index.get((q.law_id, q.article_no, q.part or DECREE))
        if text is None:
            continue
        missing = [term for term in q.must_contain if term.lower() not in text]
        if missing:
            problems.append(
                f"a{position:03d}: {q.law_id} art {q.article_no} lacks {missing} "
                f"— the label may point at the wrong article"
            )
    return problems


def main() -> int:
    rows = build()
    problems = verify(rows) + verify_keywords()
    if problems:
        print("label verification FAILED:")
        for problem in problems:
            print(f"  {problem}")
        return 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    answerable = sum(1 for row in rows if row["answerable"])
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    print(f"  {len(rows)} questions: {answerable} answerable, {len(rows) - answerable} traps")
    print("  every label verified against the parsed corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
