"""Open a draft pull request through GitHub's MCP server (TDD step 5: the agent opens a PR).

After the agent pushes its branch with git, this opens the PR from the text the agent wrote.
It only opens a DRAFT, never merges, and is safe to run twice: if a PR is already open for the
branch it returns that one instead of creating another.

Usage:
  py -3 pipeline.py pr --head <branch> --title "..." --body-file <PR_BODY.md> [--repo owner/name] [--base main]
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_REMOTE_PATTERNS = (
    re.compile(r"^https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$"),
    re.compile(r"^git@[^:]+:([^/]+)/([^/]+?)(?:\.git)?$"),
    re.compile(r"^ssh://git@[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$"),
)


class PullRequestError(RuntimeError):
    """A problem the user can act on."""


@dataclass
class PullRequest:
    url: str
    number: "int | None"
    created: bool          # False when an existing open PR was returned instead


def parse_remote_url(url: str) -> "tuple[str, str]":
    """owner and repository name from an https, ssh or scp-style GitHub remote URL."""
    for pattern in _REMOTE_PATTERNS:
        match = pattern.match(url.strip())
        if match:
            return match.group(1), match.group(2)
    raise PullRequestError(f"cannot read an owner/repository from the remote URL {url!r}")


def repo_from_git(directory: "str | Path | None" = None, remote: str = "origin") -> "tuple[str, str]":
    done = subprocess.run(["git", "remote", "get-url", remote], cwd=directory, capture_output=True, text=True)
    if done.returncode != 0:
        raise PullRequestError(f"this repository has no '{remote}' remote; add one or pass --repo owner/name")
    return parse_remote_url(done.stdout)


def split_repo(value: str) -> "tuple[str, str]":
    if value.count("/") != 1 or not all(value.split("/")):
        raise PullRequestError(f"--repo must look like owner/name, got {value!r}")
    owner, name = value.split("/")
    return owner, name


def _pull_request_from(payload: Any) -> "PullRequest | None":
    """The link and number from a create_pull_request answer (or a list_pull_requests item)."""
    if not isinstance(payload, dict):
        return None
    url = payload.get("html_url") or payload.get("url")
    number = payload.get("number")
    if url and "/pull/" in str(url):
        return PullRequest(str(url), number if isinstance(number, int) else None, True)
    text = payload.get("text")
    if isinstance(text, str):
        found = re.search(r"https?://\S+/pull/(\d+)", text)
        if found:
            return PullRequest(found.group(0).rstrip(".,)"), int(found.group(1)), True)
    return None


def _explain(message: str, owner: str, repo: str) -> str:
    lowered = message.lower()
    if "403" in lowered or "forbidden" in lowered or "not accessible" in lowered:
        return (f"GitHub refused (403): GITHUB_PR_TOKEN needs 'Pull requests: read and write' "
                f"on {owner}/{repo}.")
    if "401" in lowered or "bad credentials" in lowered:
        return "GitHub rejected the token (401). Check GITHUB_PR_TOKEN."
    return f"GitHub could not create the pull request: {message[:300]}"


def create_draft_pull_request(call_tool: Callable[[str, dict], Any], owner: str, repo: str, head: str,
                              base: str, title: str, body: str) -> PullRequest:
    """Open a draft PR, or return the open one that already exists for this branch."""
    if not head or head == base:
        raise PullRequestError(f"the head branch must differ from the base branch '{base}'")
    if not title.strip() or not body.strip():
        raise PullRequestError("a title and a PR body are required")

    try:
        listed = call_tool("list_pull_requests", {
            "owner": owner, "repo": repo, "state": "open", "head": f"{owner}:{head}", "perPage": 5})
        open_prs = listed if isinstance(listed, list) else listed.get("pull_requests", [])
        for item in open_prs:
            found = _pull_request_from(item)
            if found:
                found.created = False
                return found
        answer = call_tool("create_pull_request", {
            "owner": owner, "repo": repo, "title": title, "head": head, "base": base,
            "body": body, "draft": True})
    except RuntimeError as exc:
        raise PullRequestError(_explain(str(exc), owner, repo)) from exc

    found = _pull_request_from(answer)
    if found is None:
        raise PullRequestError(f"GitHub answered without a PR link: {str(answer)[:200]}")
    return found
