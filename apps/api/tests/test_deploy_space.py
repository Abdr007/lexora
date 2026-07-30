"""The Hugging Face Spaces frontmatter contract.

The Hub validates this block in a pre-receive hook, so a violation rejects the push only
after the whole repository has been uploaded, and the error names the offending field
without naming the file it lives in. These tests move that failure to `make check`.

`scripts/deploy_space.py` sits at the repository root, outside the `apps/api` entry that
pytest is configured with, so it is loaded by path rather than imported as a package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_deploy_space() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "deploy_space", REPO_ROOT / "scripts" / "deploy_space.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


deploy_space = _load_deploy_space()


class TestShortDescription:
    def test_committed_readme_is_deployable(self):
        """The regression guard. This is the file that actually gets pushed."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        assert readme.startswith("---"), "README lost its Spaces frontmatter"
        assert deploy_space.frontmatter_problems(readme.split("---")[1]) == []

    def test_over_long_description_is_rejected(self):
        too_long = "x" * (deploy_space.SHORT_DESCRIPTION_MAX + 1)
        problems = deploy_space.frontmatter_problems(
            f"sdk: docker\nshort_description: {too_long}\n"
        )
        assert len(problems) == 1
        # The count has to be in the message; "too long" without a number means opening
        # the file and counting by hand.
        assert str(deploy_space.SHORT_DESCRIPTION_MAX + 1) in problems[0]

    def test_exactly_at_the_limit_is_accepted(self):
        at_limit = "x" * deploy_space.SHORT_DESCRIPTION_MAX
        assert (
            deploy_space.frontmatter_problems(f"sdk: docker\nshort_description: {at_limit}\n") == []
        )

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_quotes_are_stripped_before_measuring(self, quote: str):
        """The live description is quoted, because it contains a colon."""
        described = "UAE labour & Dubai tenancy law: cited answers, or refusal"
        assert len(described) <= deploy_space.SHORT_DESCRIPTION_MAX
        problems = deploy_space.frontmatter_problems(
            f"sdk: docker\nshort_description: {quote}{described}{quote}\n"
        )
        assert problems == []

    def test_colon_inside_a_description_is_measured_whole(self):
        """Splitting on every colon would measure only the text before it.

        Constructed so the head is comfortably short while the whole description is over
        the limit: a parser that truncated at the inner colon would report no problem
        here, and the Hub would reject the push anyway. That is the precise failure this
        check exists to prevent, so the case has to be able to fail.
        """
        head = "UAE labour law"
        described = f"{head}: {'y' * (deploy_space.SHORT_DESCRIPTION_MAX - len(head))}"
        assert len(head) < deploy_space.SHORT_DESCRIPTION_MAX < len(described)

        problems = deploy_space.frontmatter_problems(
            f'sdk: docker\nshort_description: "{described}"\n'
        )
        assert len(problems) == 1
        assert str(len(described)) in problems[0]


class TestSdkDeclaration:
    def test_missing_docker_sdk_is_rejected(self):
        problems = deploy_space.frontmatter_problems("title: Lexora\napp_port: 7860\n")
        assert any("sdk: docker" in problem for problem in problems)


class TestPushFailureDiagnosis:
    """Every branch here was written after being sent to the wrong page by the old one."""

    def test_metadata_rejection_points_at_the_readme(self):
        stderr = (
            "remote: Sorry, your push was rejected during YAML metadata verification:\n"
            'remote: - Error: "short_description" length must be less than or equal to 60'
        )
        assert "README.md" in deploy_space.explain_push_failure(stderr)

    def test_binary_rejection_does_not_blame_the_readme(self):
        """The real regression: this used to be reported as a frontmatter problem."""
        stderr = (
            "remote: Your push was rejected because it contains binary files.\n"
            "remote: Please use https://huggingface.co/docs/hub/xet to store binary files.\n"
            "remote: Offending files:\n"
            "remote:   - var/index/vectors.npy (ref: refs/heads/main)\n"
            "! [remote rejected] main -> main (pre-receive hook declined)"
        )
        message = deploy_space.explain_push_failure(stderr)
        assert "binary" in message
        assert "README" not in message
        assert "token" not in message

    def test_auth_rejection_points_at_the_token(self):
        stderr = "remote: Authentication failed\nfatal: could not read Password"
        assert "token" in deploy_space.explain_push_failure(stderr)

    def test_unrecognised_rejection_invents_nothing(self):
        """Silence about the cause beats a confident wrong cause."""
        message = deploy_space.explain_push_failure("remote: something entirely new")
        assert "reason is above" in message
        assert "README" not in message
        assert "token" not in message
