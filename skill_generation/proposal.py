"""Propose a skill version as a pull request on the repository the agent reads its skill from.

A new version that changes what the agent is taught should be reviewed like code. This puts it on a
branch (skill/v<N>) of the target repository, with the change log as the description of a draft PR.
Merging the PR adopts the version for that repository; closing it discards the proposal, and the
version stays saved in the pipeline either way.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from skill_template import SKILL_NAME

SKILLS_FOLDER = Path(".claude") / "skills"
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # fail with a message; never wait for a password


class ProposalError(RuntimeError):
    """A problem the user can act on."""


def _git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          env=_GIT_ENV)
    if check and done.returncode != 0:
        raise ProposalError(f"git {' '.join(args)} failed: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout.strip()


def branch_name(version: int) -> str:
    return f"skill/v{version}"


def update_base(repo: Path, base: str = "main") -> None:
    """Bring the clone's base branch up to date, so the proposal is made against what is on GitHub."""
    if _git(repo, "branch", "--show-current") == base and not _git(repo, "status", "--porcelain"):
        _git(repo, "pull", "--ff-only", "origin", base)


def prepare_branch(store_root: Path, version: int, repo: Path, base: str = "main") -> str:
    """Create skill/v<N> from the base branch with the version's skill installed and committed."""
    source = Path(store_root) / f"v{version}" / SKILL_NAME
    if not (source / "SKILL.md").exists():
        raise ProposalError(f"v{version} has no {SKILL_NAME}/SKILL.md to propose")
    if _git(repo, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise ProposalError(f"{repo} is not a git repository")
    if _git(repo, "status", "--porcelain"):
        raise ProposalError(f"{repo} has uncommitted changes; commit or stash them first")
    current = _git(repo, "branch", "--show-current")
    if current != base:
        raise ProposalError(f"switch to '{base}' first (the repository is on '{current}')")

    branch = branch_name(version)
    exists_here = bool(_git(repo, "branch", "--list", branch))
    exists_there = subprocess.run(["git", "-C", str(repo), "ls-remote", "--exit-code", "--heads", "origin", branch],
                                  capture_output=True, env=_GIT_ENV).returncode == 0
    if exists_here or exists_there:
        raise ProposalError(f"{branch} already exists: v{version} was already proposed")

    _git(repo, "switch", "-c", branch)
    destination = Path(repo) / SKILLS_FOLDER / SKILL_NAME
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    _git(repo, "add", "-A")
    if not _git(repo, "status", "--porcelain"):
        _git(repo, "switch", base)
        _git(repo, "branch", "-D", branch)
        raise ProposalError(f"{repo} already has v{version}'s skill installed; there is nothing to propose")
    _git(repo, "commit", "-q", "-m", f"Update the {SKILL_NAME} skill to v{version}")
    return branch


def push(repo: Path, branch: str) -> None:
    _git(repo, "push", "-u", "origin", branch)


def back_to(repo: Path, base: str) -> None:
    _git(repo, "switch", base)


def body(store_root: Path, version: int) -> str:
    """The PR description: what changes, what the agent will no longer apply, and how to finish."""
    root = Path(store_root) / f"v{version}"
    changes = (root / "CHANGES.md").read_text(encoding="utf-8").rstrip()
    patterns = json.loads((root / "patterns.json").read_text(encoding="utf-8")).get("patterns", [])
    held_back = [p for p in patterns if p.get("confidence") == "needs_review"]
    lines = [
        f"## Proposed skill update: v{version}", "",
        "This changes the skill the migration agent reads in this repository.",
        "**Merging adopts it. Closing discards it** (the version stays saved in the pipeline).", "",
        changes, "",
    ]
    if held_back:
        lines += ["### Rules the agent will not apply under this version",
                  "These are waiting for a person's ruling. The agent uses its own judgment for them.", ""]
        for pattern in held_back:
            reason = pattern.get("feedback_reason") or "needs review"
            lines.append(f"- `{pattern['id']}`: {pattern['rule']} ({reason})")
        lines.append("")
    lines += [
        "### To finish",
        f"- Accept: merge this PR, then run `py -3 pipeline.py adopt {version}` so the pipeline records it.",
        "- Disagree with a held-back rule: close this PR, run "
        "`py -3 pipeline.py feedback resolve <pattern_id> --keep --reason \"...\"`, then run the pipeline again.",
    ]
    return "\n".join(lines) + "\n"
