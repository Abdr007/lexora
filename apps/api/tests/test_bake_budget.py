"""The CI gate that would have caught the build-time OOM.

Parsing is done against the real log text, mangled encoding included: the Hugging Face
build log renders the em-dash in `scripts/bake.py`'s output as a stray byte, so a pattern
anchored on that character would match locally and fail in the only place it matters.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


budget = _load("check_bake_budget")

# Copied from an actual Hugging Face build log, including the mangled separator.
REAL_LOG = """\
--> RUN python scripts/bake.py embedding
baked [embedding] embedding model cached \xe2 peak RSS 290 MB
DONE 2.6s
--> RUN python scripts/bake.py reranker
baked [reranker] cross-encoder cached \xe2 peak RSS 254 MB
DONE 11.2s
--> RUN python scripts/bake.py vectorstore
baked [vectorstore] 181 vector points \xe2 peak RSS 157 MB
DONE 1.4s
"""


class TestParse:
    def test_reads_every_stage_from_a_real_log(self):
        assert budget.parse(REAL_LOG) == {
            "embedding": 290.0,
            "reranker": 254.0,
            "vectorstore": 157.0,
        }

    def test_local_em_dash_also_parses(self):
        line = "baked [embedding] embedding model cached — peak RSS 12 MB"
        assert budget.parse(line) == {"embedding": 12.0}

    def test_empty_log_yields_nothing(self):
        assert budget.parse("") == {}


class TestProblems:
    def test_real_log_passes(self):
        assert budget.problems(budget.parse(REAL_LOG)) == []

    def test_over_budget_stage_is_reported(self):
        peaks = {"embedding": 900.0, "reranker": 254.0, "vectorstore": 157.0}
        found = budget.problems(peaks)
        assert len(found) == 1
        assert "900" in found[0]

    def test_missing_stage_fails_rather_than_passing_vacuously(self):
        """The check itself must not be silently satisfiable.

        A budget test over an empty set passes. If a stage stopped printing — renamed,
        removed, or crashed before its line — the gate would go green while the thing it
        guards went unmeasured.
        """
        found = budget.problems({"embedding": 290.0})
        assert len(found) == 2
        assert any("reranker" in problem for problem in found)
        assert any("vectorstore" in problem for problem in found)

    def test_empty_log_is_a_failure_not_a_pass(self):
        assert len(budget.problems({})) == len(budget.EXPECTED_STAGES)
