"""Tests for proposing a skill version as a PR, and for not making versions out of evidence alone.

Run from the migration-pipeline folder:  py -3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skill_generation"))
sys.path.insert(0, str(ROOT / "connectors"))
sys.path.insert(0, str(ROOT / "feedback_loop"))

import feedback_loop as fl  # noqa: E402
import mcp_client  # noqa: E402
import pipeline  # noqa: E402
import proposal  # noqa: E402
import versioning  # noqa: E402

SKILL = "java-to-kotlin-android"
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
FIXTURE = str(ROOT / "fixtures" / "migration-fixture-android")
PR_URL = "https://github.com/octo/repo/pull/5"


def doc(*ids, demoted=()):
    patterns = []
    for pid in ids:
        status = "needs_review" if pid in demoted else "medium"
        entry = {"id": pid, "category": "statics", "rule": f"Rule {pid}.", "confidence": status,
                 "sources": {"commits": 4, "pairs": 4},
                 "examples": [{"file": "A.java", "commit": "abc1234", "java": "j", "kotlin": "k"}]}
        if pid in demoted:
            entry["feedback_reason"] = "rejected in 1 reviewed draft(s), accepted in 0: because"
        patterns.append(entry)
    return {"generated_from": {"pairs": 4, "commits": 4}, "patterns": patterns}


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Workspace(unittest.TestCase):
    """A skills store with two versions, a target repo with a local bare 'origin', and a fake MCP."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = versioning.SkillStore(self.dir / "skills")
        self.store.create_version(doc("a", "b"), NOW)
        self.store.create_version(doc("a", "b", demoted=("b",)), NOW)

        self.origin = self.dir / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True, capture_output=True)
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                    ["config", "core.autocrlf", "false"]):
            git(self.repo, *cmd)
        skill = self.repo / ".claude" / "skills" / SKILL
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("the skill, version 1\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "-u", "origin", "main")

    def version_text(self, n):
        return (self.store.root / f"v{n}" / SKILL / "SKILL.md").read_text(encoding="utf-8")

    def installed_text(self):
        return (self.repo / ".claude" / "skills" / SKILL / "SKILL.md").read_text(encoding="utf-8")


class PrepareBranchTests(Workspace):
    def test_the_branch_carries_the_versions_skill_and_nothing_else(self):
        branch = proposal.prepare_branch(self.store.root, 2, self.repo)
        self.assertEqual(branch, "skill/v2")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "skill/v2")
        self.assertEqual(self.installed_text(), self.version_text(2))
        self.assertEqual(git(self.repo, "diff", "--name-only", "main..skill/v2"),
                         f".claude/skills/{SKILL}/SKILL.md")
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), f"Update the {SKILL} skill to v2")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_the_base_branch_is_left_untouched(self):
        before = git(self.repo, "rev-parse", "main")
        proposal.prepare_branch(self.store.root, 2, self.repo)
        proposal.back_to(self.repo, "main")
        self.assertEqual(git(self.repo, "rev-parse", "main"), before)
        self.assertEqual(self.installed_text(), "the skill, version 1\n")

    def test_it_refuses_a_dirty_repo_the_wrong_branch_and_a_second_proposal(self):
        (self.repo / "scratch.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(proposal.ProposalError) as ctx:
            proposal.prepare_branch(self.store.root, 2, self.repo)
        self.assertIn("uncommitted changes", str(ctx.exception))
        (self.repo / "scratch.txt").unlink()

        git(self.repo, "switch", "-q", "-c", "other")
        with self.assertRaises(proposal.ProposalError) as ctx:
            proposal.prepare_branch(self.store.root, 2, self.repo)
        self.assertIn("switch to 'main' first", str(ctx.exception))
        git(self.repo, "switch", "-q", "main")

        proposal.prepare_branch(self.store.root, 2, self.repo)
        proposal.back_to(self.repo, "main")
        with self.assertRaises(proposal.ProposalError) as ctx:
            proposal.prepare_branch(self.store.root, 2, self.repo)
        self.assertIn("already proposed", str(ctx.exception))

    def test_a_version_the_repo_already_has_is_not_proposed_again(self):
        skill = self.repo / ".claude" / "skills" / SKILL / "SKILL.md"
        skill.write_text(self.version_text(2), encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "already installed")
        with self.assertRaises(proposal.ProposalError) as ctx:
            proposal.prepare_branch(self.store.root, 2, self.repo)
        self.assertIn("nothing to propose", str(ctx.exception))
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")      # cleaned up after itself
        self.assertEqual(git(self.repo, "branch", "--list", "skill/v2"), "")

    def test_an_unknown_version_is_refused(self):
        with self.assertRaises(proposal.ProposalError):
            proposal.prepare_branch(self.store.root, 9, self.repo)


class BodyTests(Workspace):
    def test_the_body_has_the_change_log_the_held_back_rules_and_how_to_finish(self):
        text = proposal.body(self.store.root, 2)
        self.assertIn("Proposed skill update: v2", text)
        self.assertIn("Merging adopts it. Closing discards it", text)
        self.assertIn("changes since v1", text)                                   # the change log
        self.assertIn("`b`: Rule b. (rejected in 1 reviewed draft(s)", text)      # held back, with the reason
        self.assertIn("pipeline.py adopt 2", text)
        self.assertIn("feedback resolve <pattern_id> --keep", text)

    def test_a_version_with_nothing_held_back_has_no_such_section(self):
        self.assertNotIn("will not apply", proposal.body(self.store.root, 1))


class ProposeCommandTests(Workspace):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["--skills-dir", str(self.store.root), *args])
        return code, out.getvalue(), err.getvalue()

    def fake_mcp(self, calls):
        def call(name, args, profile=mcp_client.READ):
            calls.append((name, args, profile))
            return [] if name == "list_pull_requests" else {"html_url": PR_URL, "number": 5}
        return call

    def test_the_whole_proposal_pushes_a_branch_and_opens_a_draft_pr(self):
        calls = []
        with mock.patch.object(mcp_client, "call_tool", self.fake_mcp(calls)), \
                mock.patch.object(mcp_client, "close_session"):
            code, out, err = self.run_cli("propose", "2", "--repo-path", str(self.repo), "--repo", "octo/repo")
        self.assertEqual(code, 0, err)
        self.assertIn(f"Proposed v2 as {PR_URL}", out)
        created = next(a for n, a, _ in calls if n == "create_pull_request")
        self.assertEqual((created["head"], created["base"], created["draft"]), ("skill/v2", "main", True))
        self.assertEqual(created["title"], f"Update the {SKILL} skill to v2")
        self.assertIn("Proposed skill update: v2", created["body"])
        self.assertTrue(all(p is mcp_client.PR for _, _, p in calls))
        # the branch really reached the remote, and the clone is back on main, clean
        self.assertIn("skill/v2", git(self.origin, "branch", "--list"))
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        # the pipeline remembers it, and status says so
        state = self.store.load_state()
        self.assertEqual(state["versions"]["2"]["status"], "proposed")
        self.assertEqual(state["versions"]["2"]["proposal"], PR_URL)
        self.assertIsNone(state["current"])                                       # merging is the adoption, not proposing
        _, status, _ = self.run_cli("status")
        self.assertIn(f"proposed in {PR_URL}", status)

    def test_proposing_the_same_version_twice_is_refused_with_a_clear_message(self):
        with mock.patch.object(mcp_client, "call_tool", self.fake_mcp([])), mock.patch.object(mcp_client, "close_session"):
            self.run_cli("propose", "2", "--repo-path", str(self.repo), "--repo", "octo/repo")
            code, _, err = self.run_cli("propose", "2", "--repo-path", str(self.repo), "--repo", "octo/repo")
        self.assertEqual(code, 1)
        self.assertIn("already proposed", err)

    def test_a_dirty_repo_stops_before_anything_is_pushed(self):
        (self.repo / "scratch.txt").write_text("x", encoding="utf-8")
        with mock.patch.object(mcp_client, "call_tool", side_effect=AssertionError("must not call MCP")):
            code, _, err = self.run_cli("propose", "2", "--repo-path", str(self.repo), "--repo", "octo/repo")
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", err)
        self.assertNotIn("skill/v2", git(self.origin, "branch", "--list"))


class EvidenceOnlyRunTests(unittest.TestCase):
    """`run` makes a version for changes that alter the skill, not for evidence alone."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.skills = Path(tmp.name) / "skills"
        self.config = Path(tmp.name) / "config.json"
        self.config.write_text(json.dumps({"trigger": {"mode": "either", "new_commits": 10, "schedule_days": 7}}),
                               encoding="utf-8")
        self.run_cli("run", "--local", FIXTURE)          # v1

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = pipeline.main(["--config", str(self.config), "--skills-dir", str(self.skills), *args])
        return code, out.getvalue()

    def write_outcomes(self, *outcomes):
        fl.save_outcomes(self.skills / "feedback", {f"o/r#{o['pr']}": o for o in outcomes})

    def pr(self, number, state, patterns, votes=()):
        return {"repo": "o/r", "pr": number, "title": "t", "head": f"migrate/m{number}", "state": state,
                "patterns": patterns, "votes": list(votes), "updated_at": "2026-10-13T12:00:00+00:00",
                "collected_at": "2026-10-13T12:00:00+00:00"}

    def versions(self):
        return sorted(p.name for p in self.skills.glob("v[0-9]*"))

    def test_a_merge_alone_creates_no_version_but_resets_the_trigger(self):
        self.write_outcomes(self.pr(1, "merged", ["getter_to_property", "data_class"]))
        code, out = self.run_cli("run", "--local", FIXTURE)
        self.assertEqual(code, 0)
        self.assertIn("only review evidence changed (2 pattern(s))", out)
        self.assertEqual(self.versions(), ["v1"])
        # the evidence is not lost: it is recomputed from the outcomes and will be in the next material version
        _, again = self.run_cli("run", "--local", FIXTURE)
        self.assertIn("Not due", again)                      # the decision was recorded, so it is not "new" any more

    def test_a_named_rejection_does_create_a_version(self):
        vote = {"pattern": "getter_to_property", "verdict": "reject", "reason": "breaks isX()", "by": "bob"}
        self.write_outcomes(self.pr(1, "merged", ["getter_to_property", "data_class"], [vote]))
        _, out = self.run_cli("run", "--local", FIXTURE)
        self.assertIn("Created v2 (pending)", out)
        self.assertEqual(self.versions(), ["v1", "v2"])

    def test_the_evidence_from_an_earlier_merge_appears_in_the_next_material_version(self):
        self.write_outcomes(self.pr(1, "merged", ["data_class"]))
        self.run_cli("run", "--local", FIXTURE)                                        # evidence only: no version
        vote = {"pattern": "getter_to_property", "verdict": "reject", "reason": "x", "by": "bob"}
        self.write_outcomes(self.pr(1, "merged", ["data_class"]), self.pr(2, "pending", [], [vote]))
        self.run_cli("run", "--local", FIXTURE)                                        # material: version
        skill = (self.skills / "v2" / SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Accepted in 1 reviewed draft(s).", skill)                       # carried forward
        self.assertIn("needs review: rejected in 1", skill)

    def test_force_still_creates_a_version_for_evidence_alone(self):
        self.write_outcomes(self.pr(1, "merged", ["data_class"]))
        _, out = self.run_cli("run", "--local", FIXTURE, "--force")
        self.assertIn("Created v2", out)


class RunThenProposeTests(Workspace):
    """`run --propose-repo-path` puts a newly created version up for review; no version, no PR."""

    def setUp(self):
        super().setUp()
        self.skills = self.store.root.parent / "run-skills"
        self.config = self.skills.parent / "config.json"
        self.config.write_text(json.dumps({"trigger": {"mode": "either", "new_commits": 10, "schedule_days": 7}}),
                               encoding="utf-8")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        calls = []

        def call(name, arguments, profile=mcp_client.READ):
            calls.append(name)
            return [] if name == "list_pull_requests" else {"html_url": PR_URL, "number": 5}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(mcp_client, "call_tool", call), mock.patch.object(mcp_client, "close_session"):
            code = pipeline.main(["--config", str(self.config), "--skills-dir", str(self.skills), *args])
        return code, out.getvalue(), err.getvalue(), calls

    def test_a_new_version_is_proposed_and_a_run_with_nothing_new_is_not(self):
        propose = ("--propose-repo-path", str(self.repo), "--propose-repo", "octo/repo")
        code, out, err, calls = self.run_cli("run", "--local", FIXTURE, *propose)
        self.assertEqual(code, 0, err)
        self.assertIn("Created v1 (pending)", out)
        self.assertIn(f"Proposed v1 as {PR_URL}", out)
        self.assertIn("skill/v1", git(self.origin, "branch", "--list"))
        code, out, _, calls = self.run_cli("run", "--local", FIXTURE, *propose)
        self.assertIn("Not due", out)
        self.assertEqual(calls, [])                                          # nothing new, so no PR traffic at all

    def test_the_clone_is_brought_up_to_date_before_proposing(self):
        other = self.origin.parent / "other"
        git(self.origin.parent, "clone", "-q", "-b", "main", str(self.origin), str(other))
        (other / "later.txt").write_text("x", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "later")
        git(other, "push", "-q", "origin", "main")
        code, out, err, _ = self.run_cli("run", "--local", FIXTURE, "--propose-repo-path", str(self.repo),
                                         "--propose-repo", "octo/repo")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.repo / "later.txt").exists())


if __name__ == "__main__":
    unittest.main()
