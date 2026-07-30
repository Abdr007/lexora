#!/usr/bin/env python3
"""Create the Hugging Face Space and push this repository to it.

Idempotent: run it again to redeploy. It creates the Space if it is missing, commits
anything outstanding, wires the git remote, and pushes.

    python scripts/deploy_space.py --space lexora --public

Authentication comes from `~/.cache/huggingface/token`, written by `hf auth login`
(note: `huggingface-cli` is deprecated in huggingface_hub 1.x and no longer runs).
No token is passed on this command line, so it cannot reach shell history.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[1]

# Files that must be inside the image. If any is missing the Space builds and then
# reports `degraded`, which is a slow way to discover a missing artefact.
REQUIRED: Final = (
    Path("Dockerfile"),
    Path("README.md"),
    Path("var/index/chunks.jsonl"),
    Path("var/index/bm25.json"),
    Path("corpus/manifest.json"),
)

# The Hub enforces this in a pre-receive hook, so an over-long description rejects the
# push only after the entire repository has been uploaded, and the message names the
# field without naming the file it lives in. Checking it here costs nothing.
SHORT_DESCRIPTION_MAX: Final = 60


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, cwd=REPO_ROOT, capture_output=True, text=True, check=check
    )


def frontmatter_problems(frontmatter: str) -> list[str]:
    """Check the Spaces configuration block. Pure, so it is testable without a repo."""
    problems: list[str] = []
    if "sdk: docker" not in frontmatter:
        problems.append("README.md frontmatter does not declare `sdk: docker`")

    for line in frontmatter.splitlines():
        if not line.startswith("short_description:"):
            continue
        # Split once only: quoting is optional in YAML, and a description containing a
        # colon needs the quotes — splitting on every colon would truncate at the first.
        described = line.split(":", 1)[1].strip().strip("\"'")
        if len(described) > SHORT_DESCRIPTION_MAX:
            problems.append(
                f"short_description is {len(described)} characters; the Hub rejects "
                f"anything over {SHORT_DESCRIPTION_MAX}. Shorten it in README.md."
            )
    return problems


def explain_push_failure(stderr: str) -> str:
    """Turn the remote's rejection into the one instruction that acts on it.

    This used to blame a read-only token whatever the remote actually said. The Hub
    rejects a push for several unrelated reasons and always names the one that applies;
    guessing sends you to the tokens page while the real fault sits in README.md. When
    nothing matches, say so rather than inventing a cause.
    """
    if "YAML metadata" in stderr:
        return (
            "push failed: the Hub rejected the repository metadata. Correct the field "
            "named above in README.md's frontmatter and re-run."
        )
    if "binary files" in stderr:
        return (
            "push failed: the Hub refuses binary files outside Xet/LFS. Store the "
            "offending file above in a text format, or track it with Xet."
        )
    if any(marker in stderr for marker in ("Authentication", "401", "403")):
        return (
            "push failed: authentication. Create a WRITE token at "
            "https://huggingface.co/settings/tokens and log in again with "
            "`hf auth login --token hf_... --add-to-git-credential`."
        )
    return "push failed. The remote's reason is above."


def preflight() -> list[str]:
    problems: list[str] = []
    for relative in REQUIRED:
        if not (REPO_ROOT / relative).exists():
            problems.append(f"missing {relative} — run `make index`")

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    if not readme.startswith("---"):
        problems.append("README.md has no Spaces YAML frontmatter (sdk: docker)")
        return problems

    problems.extend(frontmatter_problems(readme.split("---")[1]))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--space", default="lexora", help="Space name (not the full id)")
    parser.add_argument("--public", action="store_true", help="make the Space public")
    parser.add_argument("--message", default="Deploy Lexora", help="commit message")
    args = parser.parse_args(argv)

    problems = preflight()
    if problems:
        print("preflight failed:")
        for problem in problems:
            print(f"  {problem}")
        return 1

    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("huggingface_hub is not installed. Run `make setup-api`.")
        return 1

    api = HfApi()
    try:
        user = api.whoami()["name"]
    except Exception:
        print(
            "Not logged in to Hugging Face.\n"
            "  1. Create a WRITE token at https://huggingface.co/settings/tokens\n"
            "  2. apps/api/.venv/bin/hf auth login --token hf_... --add-to-git-credential\n"
        )
        return 1

    repo_id = f"{user}/{args.space}"
    print(f"  user   {user}")
    print(f"  space  {repo_id}  ({'public' if args.public else 'private'})")

    url = api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        private=not args.public,
        exist_ok=True,
    )
    print(f"  ready  {url}")

    # Commit whatever is outstanding. A Space is deployed by pushing a git history, so
    # there is nothing to push until the work is committed.
    if run("git", "status", "--porcelain", check=False).stdout.strip():
        run("git", "add", "-A")
        run("git", "commit", "-m", args.message, check=False)
        print("  committed the working tree")

    remote_url = f"https://huggingface.co/spaces/{repo_id}"
    existing = run("git", "remote", check=False).stdout.split()
    if "space" in existing:
        run("git", "remote", "set-url", "space", remote_url)
    else:
        run("git", "remote", "add", "space", remote_url)

    branch = run("git", "branch", "--show-current").stdout.strip() or "master"
    print(f"  pushing {branch} -> space/main …")
    pushed = run("git", "push", "--force", "space", f"{branch}:main", check=False)
    if pushed.returncode != 0:
        stderr = pushed.stderr.strip()
        print(stderr[-1500:])
        print(f"\n{explain_push_failure(stderr)}")
        return 1

    print(f"\n  deployed  {remote_url}")
    print(f"  API       https://{user.replace('_', '-')}-{args.space}.hf.space/api/health")
    print("\n  The first build takes 8-12 minutes (it downloads the ONNX models).")
    print("  Watch it under the Logs tab, then add LEXORA_ANTHROPIC_API_KEY under")
    print("  Settings -> Variables and secrets when your key arrives.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
