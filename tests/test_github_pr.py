"""Tests for opening pull requests through GitHub's MCP server.

The MCP call is replaced by a fake, so these run offline.

Run from the migration-pipeline folder:  py -3 -m unittest discover -s tests
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connectors"))

import github_pr  # noqa: E402
import mcp_client  # noqa: E402
import pipeline  # noqa: E402

PR_URL = "https://github.com/octo/repo/pull/7"


class FakeGitHub:
    """Stands in for call_tool: records every call and answers from a script."""

    def __init__(self, open_prs=None, create_answer=None, create_error=None, list_error=None):
        self.open_prs = open_prs if open_prs is not None else []
        self.create_answer = create_answer if create_answer is not None else {"html_url": PR_URL, "number": 7}
        self.create_error, self.list_error = create_error, list_error
        self.calls = []

    def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "list_pull_requests":
            if self.list_error:
                raise RuntimeError(self.list_error)
            return self.open_prs
        if name == "create_pull_request":
            if self.create_error:
                raise RuntimeError(self.create_error)
            return self.create_answer
        raise AssertionError(f"unexpected tool {name}")

    def names(self):
        return [n for n, _ in self.calls]


def make(fake, head="migrate/billing-to-kotlin", base="main", title="Migrate billing to Kotlin", body="## PR\ntext"):
    return github_pr.create_draft_pull_request(fake, "octo", "repo", head, base, title, body)


class CreateTests(unittest.TestCase):
    def test_opens_a_draft_pr_with_the_right_arguments(self):
        fake = FakeGitHub()
        result = make(fake)
        self.assertEqual((result.url, result.number, result.created), (PR_URL, 7, True))
        sent = next(a for n, a in fake.calls if n == "create_pull_request")
        self.assertIs(sent["draft"], True)
        self.assertEqual((sent["head"], sent["base"], sent["title"], sent["body"]),
                         ("migrate/billing-to-kotlin", "main", "Migrate billing to Kotlin", "## PR\ntext"))
        self.assertEqual((sent["owner"], sent["repo"]), ("octo", "repo"))

    def test_an_already_open_pr_is_returned_and_nothing_is_created(self):
        fake = FakeGitHub(open_prs=[{"html_url": "https://github.com/octo/repo/pull/3", "number": 3}])
        result = make(fake)
        self.assertEqual((result.number, result.created), (3, False))
        self.assertNotIn("create_pull_request", fake.names())

    def test_the_duplicate_check_looks_at_this_owner_and_branch_only(self):
        fake = FakeGitHub()
        make(fake)
        asked = next(a for n, a in fake.calls if n == "list_pull_requests")
        self.assertEqual((asked["head"], asked["state"]), ("octo:migrate/billing-to-kotlin", "open"))

    def test_a_wrapped_list_answer_is_understood(self):
        self.assertFalse(make(FakeGitHub(open_prs={"pull_requests": [{"html_url": PR_URL, "number": 7}]})).created)

    def test_an_answer_without_a_link_is_an_error_not_a_silent_success(self):
        with self.assertRaises(github_pr.PullRequestError) as ctx:
            make(FakeGitHub(create_answer={"id": 5}))
        self.assertIn("without a PR link", str(ctx.exception))

    def test_a_plain_text_answer_with_a_link_is_understood(self):
        self.assertEqual(make(FakeGitHub(create_answer={"text": f"Created {PR_URL}."})).number, 7)


class ValidationTests(unittest.TestCase):
    def test_nothing_is_sent_for_bad_input(self):
        for kwargs in ({"head": "main"}, {"head": ""}, {"title": "  "}, {"body": "\n "}):
            fake = FakeGitHub()
            with self.assertRaises(github_pr.PullRequestError, msg=kwargs):
                make(fake, **kwargs)
            self.assertEqual(fake.calls, [], kwargs)


class ErrorTests(unittest.TestCase):
    def explained(self, **fake_kwargs):
        with self.assertRaises(github_pr.PullRequestError) as ctx:
            make(FakeGitHub(**fake_kwargs))
        return str(ctx.exception)

    def test_forbidden_names_the_permission_needed(self):
        self.assertIn("Pull requests: read and write", self.explained(create_error="failed: 403 Forbidden"))

    def test_bad_credentials_points_at_the_token(self):
        self.assertIn("GITHUB_PR_TOKEN", self.explained(list_error="failed: 401 Bad credentials"))

    def test_other_errors_keep_their_message(self):
        self.assertIn("rate limit", self.explained(create_error="failed: rate limit exceeded"))


class RepoParsingTests(unittest.TestCase):
    def test_remote_url_forms(self):
        for url, expected in {
            "https://github.com/octo/repo.git": ("octo", "repo"),
            "https://github.com/octo/repo": ("octo", "repo"),
            "git@github.com:octo/repo.git": ("octo", "repo"),
            "ssh://git@github.example.com/octo/repo.git": ("octo", "repo"),
        }.items():
            self.assertEqual(github_pr.parse_remote_url(url), expected, url)
        with self.assertRaises(github_pr.PullRequestError):
            github_pr.parse_remote_url("not a url")

    def test_split_repo(self):
        self.assertEqual(github_pr.split_repo("octo/repo"), ("octo", "repo"))
        for bad in ("octo", "octo/", "/repo", "a/b/c"):
            with self.assertRaises(github_pr.PullRequestError, msg=bad):
                github_pr.split_repo(bad)

    def test_repo_from_a_git_remote_and_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True)
            with self.assertRaises(github_pr.PullRequestError):
                github_pr.repo_from_git(tmp)
            subprocess.run(["git", "-C", tmp, "remote", "add", "origin", "https://github.com/octo/repo.git"],
                           check=True, capture_output=True)
            self.assertEqual(github_pr.repo_from_git(tmp), ("octo", "repo"))


class ProfileTests(unittest.TestCase):
    def test_the_pr_profile_can_open_and_read_pull_requests_never_merge_or_change_code(self):
        self.assertEqual(mcp_client.PR.allowed_tools,
                         frozenset({"create_pull_request", "list_pull_requests", "pull_request_read"}))
        for tool in ("merge_pull_request", "update_pull_request", "push_files", "create_or_update_file",
                     "delete_file", "create_branch", "issue_write"):
            with self.assertRaises(PermissionError, msg=tool):
                mcp_client.call_tool(tool, {}, mcp_client.PR)

    def test_the_read_profile_cannot_touch_pull_requests_at_all(self):
        for tool in ("create_pull_request", "merge_pull_request", "push_files", "list_pull_requests"):
            with self.assertRaises(PermissionError, msg=tool):
                mcp_client.call_tool(tool, {})            # the default profile is read
            with self.assertRaises(PermissionError, msg=tool):
                mcp_client.call_tool_text(tool, {})

    def test_each_profile_uses_its_own_token(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "read-token", "GITHUB_PR_TOKEN": "pr-token"}):
            self.assertEqual(mcp_client._settings(mcp_client.PR)[1]["Authorization"], "Bearer pr-token")
            self.assertEqual(mcp_client._settings(mcp_client.READ)[1]["Authorization"], "Bearer read-token")
        with mock.patch.dict(os.environ):
            os.environ.pop("GITHUB_PR_TOKEN", None)
            with self.assertRaises(SystemExit) as ctx:
                mcp_client._settings(mcp_client.PR)
        self.assertIn("GITHUB_PR_TOKEN", str(ctx.exception))


class PipelineCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.body = self.dir / "PR_BODY.md"
        self.body.write_text("## Java to Kotlin: billing\n\ntext\n", encoding="utf-8")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["--skills-dir", str(self.dir / "skills"), "pr", *args])
        return code, out.getvalue(), err.getvalue()

    def args(self, **overrides):
        values = {"--repo": "octo/repo", "--head": "migrate/billing-to-kotlin", "--title": "Migrate billing",
                  "--body-file": str(self.body)}
        values.update(overrides)
        return [part for pair in values.items() for part in pair]

    def test_the_pr_is_opened_through_the_pr_profile_and_run_twice_it_is_not_duplicated(self):
        profiles = []
        fake = FakeGitHub()

        def routed(name, args, profile=mcp_client.READ):
            profiles.append(profile)
            return fake(name, args)

        with mock.patch.object(mcp_client, "call_tool", routed), mock.patch.object(mcp_client, "close_session"):
            code, out, _ = self.run_cli(*self.args())
            self.assertEqual(code, 0, out)
            self.assertIn(f"Opened draft pull request: {PR_URL}", out)
            fake.open_prs = [{"html_url": PR_URL, "number": 7}]
            _, again, _ = self.run_cli(*self.args())
        self.assertIn("already open", again)
        self.assertEqual(fake.names().count("create_pull_request"), 1)
        self.assertTrue(all(p is mcp_client.PR for p in profiles))

    def test_a_missing_body_file_is_an_error_and_sends_nothing(self):
        with mock.patch.object(mcp_client, "call_tool", side_effect=AssertionError("must not call MCP")):
            code, _, err = self.run_cli(*self.args(**{"--body-file": str(self.dir / "missing.md")}))
        self.assertEqual(code, 1)
        self.assertIn("PR body file not found", err)


if __name__ == "__main__":
    unittest.main()
