"""Tests for the review feedback loop.

Run from the migration-pipeline folder:  py -3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "feedback_loop"))
sys.path.insert(0, str(ROOT / "skill_generation"))
sys.path.insert(0, str(ROOT / "connectors"))

import feedback_loop as fl  # noqa: E402
import mcp_client  # noqa: E402
import pipeline  # noqa: E402
import versioning  # noqa: E402

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def doc(patterns=(("getter_to_property", "medium", 8), ("data_class", "low", 2)), pairs=17, commits=17):
    return {"generated_from": {"pairs": pairs, "commits": commits}, "patterns": [
        {"id": pid, "category": "properties", "rule": f"Rule {pid}.", "confidence": conf,
         "sources": {"commits": n, "pairs": n},
         "examples": [{"file": "A.java", "commit": "abc1234", "java": "j", "kotlin": "k"}]}
        for pid, conf, n in patterns]}


def outcome(pr, state, patterns, votes=(), updated=None, head=None):
    return {"repo": "o/r", "pr": pr, "title": f"PR {pr}", "head": head or f"migrate/m{pr}-to-kotlin",
            "state": state, "patterns": list(patterns), "votes": list(votes),
            "updated_at": (updated or T0).isoformat(), "collected_at": T0.isoformat()}


def body(*patterns):
    line = ", ".join(f"{p} x1" for p in patterns)
    return ("## Java to Kotlin: m\n\nBuilt with the\njava-to-kotlin-android skill v2.\n\n"
            f"### Verification gate: PASS\nSkill patterns detected in the conversion: {line}\nVERDICT: PASS\n")


class ParsingTests(unittest.TestCase):
    def test_patterns_from_the_pr_body(self):
        self.assertEqual(fl.parse_patterns(body("getter_to_property", "data_class")),
                         ["getter_to_property", "data_class"])

    def test_no_patterns_line_gives_nothing(self):
        self.assertEqual(fl.parse_patterns("just text"), [])
        self.assertEqual(fl.parse_patterns(None), [])

    def test_skill_version(self):
        self.assertEqual(fl.parse_skill_version(body("a")), 2)
        self.assertIsNone(fl.parse_skill_version("no version here"))

    def test_skill_version_survives_the_pr_text_wrapping_the_sentence(self):
        # Seen on a real agent PR: "...with the java-to-kotlin-android" then a line break, then "skill v1."
        wrapped = "Converted 19 files, a rename commit and a conversion commit each, with the java-to-kotlin-android\nskill v1."
        self.assertEqual(fl.parse_skill_version(wrapped), 1)

    def test_votes_with_and_without_reasons(self):
        text = ("Looks fine overall.\nskill-feedback: reject getter_to_property - breaks Java callers\n"
                "Skill-Feedback: ACCEPT data_class\nskill-feedback: reject nullable_types: too aggressive")
        votes = fl.parse_votes(text, "alice")
        self.assertEqual([(v["verdict"], v["pattern"], v["reason"]) for v in votes], [
            ("reject", "getter_to_property", "breaks Java callers"),
            ("accept", "data_class", ""),
            ("reject", "nullable_types", "too aggressive")])
        self.assertTrue(all(v["by"] == "alice" for v in votes))

    def test_ordinary_text_is_not_a_vote(self):
        self.assertEqual(fl.parse_votes("I reject this approach. skill feedback is useful."), [])


class FakeGitHub:
    def __init__(self, prs, reviews=None, comments=None, review_comments=None, failing=()):
        self.prs, self.reviews = prs, reviews or {}
        self.comments, self.review_comments = comments or {}, review_comments or {}
        self.failing, self.calls = set(failing), []

    def __call__(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "list_pull_requests":
            start = (args["page"] - 1) * args["perPage"]
            return self.prs[start:start + args["perPage"]]
        method, number = args["method"], args["pullNumber"]
        if method in self.failing:
            raise RuntimeError("MCP tool failed: 500")
        return {"get_reviews": self.reviews, "get_comments": self.comments,
                "get_review_comments": self.review_comments}[method].get(number, [])


def pr(number, state="open", merged=False, head=None, text=None, draft=True):
    return {"number": number, "state": state, "merged": merged, "draft": draft,
            "head": {"ref": head or f"migrate/m{number}-to-kotlin"}, "title": f"PR {number}",
            "body": text if text is not None else body("getter_to_property"),
            "html_url": f"https://github.com/o/r/pull/{number}", "updated_at": "2026-10-13T12:00:00Z"}


class CollectTests(unittest.TestCase):
    def collect(self, fake, **kwargs):
        return fl.collect_outcomes(fake, "o", "r", now=T0, **kwargs)

    def test_state_mapping(self):
        out = self.collect(FakeGitHub([pr(1, "closed", merged=True), pr(2, "closed"), pr(3, "open")]))
        self.assertEqual({k: v["state"] for k, v in out.items()},
                         {"o/r#1": "merged", "o/r#2": "rejected", "o/r#3": "pending"})

    def test_a_merged_pr_is_recognised_from_the_shape_the_real_server_returns(self):
        # Seen on the real server: the list says merged=False for a merged PR; merged_at is set.
        merged_as_listed = {**pr(1, "closed"), "merged": False, "merged_at": "2026-10-13T10:43:31Z", "draft": False}
        closed_unmerged = {**pr(2, "closed"), "merged": False, "draft": False}      # no merged_at at all
        still_open = pr(3, "open")
        out = self.collect(FakeGitHub([merged_as_listed, closed_unmerged, still_open]))
        self.assertEqual({v["pr"]: v["state"] for v in out.values()}, {1: "merged", 2: "rejected", 3: "pending"})
        # ...and the plain merged=True form still works.
        self.assertEqual(self.collect(FakeGitHub([pr(4, "closed", merged=True)]))["o/r#4"]["state"], "merged")

    def test_only_the_agents_branches_are_read(self):
        out = self.collect(FakeGitHub([pr(1), pr(2, head="feature/other"), pr(3, head="migrate/x")]))
        self.assertEqual(sorted(o["pr"] for o in out.values()), [1, 3])
        self.assertEqual(sorted(o["pr"] for o in self.collect(
            FakeGitHub([pr(1), pr(2, head="feature/other")]), prefix="feature/").values()), [2])

    def test_patterns_and_skill_version_come_from_the_body(self):
        out = self.collect(FakeGitHub([pr(1, text=body("data_class", "nullable_types"))]))["o/r#1"]
        self.assertEqual(out["patterns"], ["data_class", "nullable_types"])
        self.assertEqual(out["skill_version"], 2)

    def test_review_states_and_votes_from_reviews_and_comments(self):
        fake = FakeGitHub(
            [pr(1, "closed")],
            reviews={1: [{"state": "CHANGES_REQUESTED", "body": "skill-feedback: reject getter_to_property - NPE risk",
                          "user": {"login": "bob"}}]},
            comments={1: [{"body": "skill-feedback: accept data_class", "user": {"login": "amy"}}]},
            review_comments={1: [{"body": "skill-feedback: reject nullable_types", "user": {"login": "bob"}}]})
        out = self.collect(fake)["o/r#1"]
        self.assertTrue(out["changes_requested"])
        self.assertFalse(out["approved"])
        self.assertEqual({(v["pattern"], v["verdict"], v["by"]) for v in out["votes"]},
                         {("getter_to_property", "reject", "bob"), ("data_class", "accept", "amy"),
                          ("nullable_types", "reject", "bob")})

    def test_a_failing_review_fetch_does_not_lose_the_outcome(self):
        out = self.collect(FakeGitHub([pr(1, "closed")], failing={"get_reviews", "get_comments", "get_review_comments"}))
        self.assertEqual(out["o/r#1"]["state"], "rejected")
        self.assertEqual(out["o/r#1"]["votes"], [])

    def test_many_pages_are_all_read(self):
        prs = [pr(n, "closed", merged=True) for n in range(1, 151)]
        self.assertEqual(len(self.collect(FakeGitHub(prs))), 150)

    def test_wrapped_list_shapes(self):
        class Wrapped(FakeGitHub):
            def __call__(self, name, args):
                result = super().__call__(name, args)
                return {"pull_requests": result} if name == "list_pull_requests" else {"reviews": result}
        self.assertEqual(len(self.collect(Wrapped([pr(1)]))), 1)

    def test_only_the_feedback_tools_are_used(self):
        fake = FakeGitHub([pr(1)])
        self.collect(fake)
        self.assertTrue({n for n, _ in fake.calls} <= {"list_pull_requests", "pull_request_read"})
        self.assertTrue({n for n, _ in fake.calls} <= mcp_client.PR.allowed_tools)


class ApplyFeedbackTests(unittest.TestCase):
    def apply(self, outcomes, decisions=None):
        return fl.apply_feedback(doc(), {o["pr"]: o for o in outcomes}, decisions or {})

    def pattern(self, result, pid):
        return next(p for p in result["patterns"] if p["id"] == pid)

    def test_a_merged_draft_is_an_acceptance_and_changes_no_confidence(self):
        result = self.apply([outcome(1, "merged", ["getter_to_property"])])
        p = self.pattern(result, "getter_to_property")
        self.assertEqual(p["feedback"], {"accepted": 1, "rejected": 0})
        self.assertEqual(p["confidence"], "medium")
        self.assertEqual(result["feedback"]["demoted"], [])

    def test_a_rejected_draft_sends_its_patterns_to_review(self):
        result = self.apply([outcome(1, "rejected", ["getter_to_property"])])
        p = self.pattern(result, "getter_to_property")
        self.assertEqual(p["confidence"], "needs_review")
        self.assertEqual(p["previous_confidence"], "medium")
        self.assertIn("rejected in 1 reviewed draft(s)", p["feedback_reason"])
        self.assertEqual(self.pattern(result, "data_class")["confidence"], "low")  # not in that draft

    def test_acceptances_outweigh_a_single_rejection(self):
        result = self.apply([outcome(1, "merged", ["getter_to_property"]), outcome(2, "merged", ["getter_to_property"]),
                             outcome(3, "rejected", ["getter_to_property"])])
        self.assertEqual(self.pattern(result, "getter_to_property")["confidence"], "medium")

    def test_a_tie_does_not_demote_a_pattern(self):
        # Demotion needs MORE rejections than acceptances: one of each leaves the rule in place.
        result = self.apply([outcome(1, "merged", ["getter_to_property"]), outcome(2, "rejected", ["getter_to_property"])])
        self.assertEqual(self.pattern(result, "getter_to_property")["confidence"], "medium")

    def test_more_rejections_than_acceptances_always_demotes(self):
        outcomes = [outcome(1, "rejected", ["data_class"]), outcome(2, "rejected", ["data_class"]),
                    outcome(3, "merged", ["data_class"])]
        self.assertEqual(self.pattern(self.apply(outcomes), "data_class")["confidence"], "needs_review")

    def test_pending_drafts_are_ignored(self):
        result = self.apply([outcome(1, "pending", ["getter_to_property"])])
        self.assertNotIn("feedback", self.pattern(result, "getter_to_property"))
        self.assertEqual(result["feedback"]["pending"], 1)

    def test_a_reviewers_named_rejection_outweighs_merges_that_merely_contained_the_rule(self):
        # The real case: a merged draft contained the rule; a reviewer then rejected it by name elsewhere.
        votes = [{"pattern": "getter_to_property", "verdict": "reject", "reason": "breaks isX()", "by": "bob"}]
        result = self.apply([outcome(1, "merged", ["getter_to_property"]), outcome(2, "merged", ["getter_to_property"]),
                             outcome(3, "pending", [], votes)])
        p = self.pattern(result, "getter_to_property")
        self.assertEqual(p["confidence"], "needs_review")
        self.assertEqual(p["feedback"], {"accepted": 2, "rejected": 1})      # the totals stay visible

    def test_a_reviewers_named_acceptance_outweighs_closed_drafts_that_merely_contained_the_rule(self):
        votes = [{"pattern": "data_class", "verdict": "accept", "reason": "", "by": "amy"}]
        result = self.apply([outcome(1, "rejected", ["data_class"]), outcome(2, "rejected", ["data_class"]),
                             outcome(3, "pending", [], votes)])
        self.assertEqual(self.pattern(result, "data_class")["confidence"], "low")

    def test_without_any_named_vote_the_implicit_signals_decide(self):
        result = self.apply([outcome(1, "rejected", ["data_class"]), outcome(2, "rejected", ["data_class"]),
                             outcome(3, "merged", ["data_class"])])
        self.assertEqual(self.pattern(result, "data_class")["confidence"], "needs_review")

    def test_an_explicit_vote_counts_at_once_even_on_a_pr_still_in_review(self):
        votes = [{"pattern": "getter_to_property", "verdict": "reject", "reason": "breaks isX()", "by": "bob"}]
        result = self.apply([outcome(1, "pending", ["getter_to_property", "data_class"], votes)])
        self.assertEqual(self.pattern(result, "getter_to_property")["confidence"], "needs_review")
        self.assertIn("breaks isX()", self.pattern(result, "getter_to_property")["feedback_reason"])
        # ...but what merging or closing would imply is not counted while the PR is open.
        self.assertNotIn("feedback", self.pattern(result, "data_class"))

    def test_an_accept_vote_on_an_open_pr_counts_as_evidence(self):
        votes = [{"pattern": "data_class", "verdict": "accept", "reason": "", "by": "amy"}]
        result = self.apply([outcome(1, "pending", ["data_class"], votes)])
        self.assertEqual(self.pattern(result, "data_class")["feedback"], {"accepted": 1, "rejected": 0})
        self.assertEqual(self.pattern(result, "data_class")["confidence"], "low")

    def test_a_reviewer_can_reject_one_rule_in_a_merged_draft(self):
        votes = [{"pattern": "getter_to_property", "verdict": "reject", "reason": "breaks isX()", "by": "bob"}]
        result = self.apply([outcome(1, "merged", ["getter_to_property", "data_class"], votes)])
        p = self.pattern(result, "getter_to_property")
        self.assertEqual(p["confidence"], "needs_review")
        self.assertIn("breaks isX()", p["feedback_reason"])
        self.assertEqual(self.pattern(result, "data_class")["feedback"], {"accepted": 1, "rejected": 0})

    def test_an_explicit_vote_replaces_the_implicit_one_for_that_pattern(self):
        votes = [{"pattern": "data_class", "verdict": "accept", "reason": "", "by": "bob"}]
        result = self.apply([outcome(1, "rejected", ["data_class", "getter_to_property"], votes)])
        self.assertEqual(self.pattern(result, "data_class")["feedback"], {"accepted": 1, "rejected": 0})
        self.assertEqual(self.pattern(result, "getter_to_property")["confidence"], "needs_review")

    def test_votes_for_unknown_patterns_are_ignored(self):
        votes = [{"pattern": "made_up", "verdict": "reject", "reason": "", "by": "bob"}]
        result = self.apply([outcome(1, "merged", ["data_class"], votes)])
        self.assertEqual([p["id"] for p in result["patterns"]], ["getter_to_property", "data_class"])

    def test_the_input_document_is_not_modified(self):
        original = doc()
        before = json.dumps(original, sort_keys=True)
        fl.apply_feedback(original, {1: outcome(1, "rejected", ["data_class"])}, {})
        self.assertEqual(json.dumps(original, sort_keys=True), before)


class DecisionTests(unittest.TestCase):
    def ruled(self, decision, *, outcome_time, decided_at=T0):
        return fl.apply_feedback(
            doc(), {1: outcome(1, "rejected", ["getter_to_property"], updated=outcome_time)},
            {"getter_to_property": {"decision": decision, "reason": "r", "at": decided_at.isoformat()}})

    def test_keep_overrides_a_rejection_that_came_before_it(self):
        result = self.ruled("keep", outcome_time=T0 - timedelta(days=1))
        p = next(p for p in result["patterns"] if p["id"] == "getter_to_property")
        self.assertEqual(p["confidence"], "medium")
        self.assertNotIn("feedback", p)

    def test_a_rejection_after_a_keep_counts_again(self):
        result = self.ruled("keep", outcome_time=T0 + timedelta(days=1))
        p = next(p for p in result["patterns"] if p["id"] == "getter_to_property")
        self.assertEqual(p["confidence"], "needs_review")

    def test_drop_removes_the_pattern_for_good(self):
        result = self.ruled("drop", outcome_time=T0 + timedelta(days=5))
        self.assertEqual([p["id"] for p in result["patterns"]], ["data_class"])
        self.assertEqual(result["feedback"]["dropped"], ["getter_to_property"])

    def test_a_decision_is_recorded_with_its_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = fl.record_decision(tmp, "data_class", "keep", "  fine in practice ", T0)
            self.assertEqual(record["reason"], "fine in practice")
            self.assertEqual(fl.load_decisions(tmp)["data_class"]["decision"], "keep")

    def test_a_decision_needs_a_valid_choice_and_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            for args in (("data_class", "maybe", "why"), ("data_class", "keep", ""), ("data_class", "drop", "   ")):
                with self.assertRaises(fl.FeedbackError, msg=args):
                    fl.record_decision(tmp, *args)


class StoreTests(unittest.TestCase):
    def test_outcomes_round_trip_and_merge_by_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(fl.load_outcomes(tmp), {})
            first = {"o/r#1": outcome(1, "pending", ["data_class"]), "o/r#2": outcome(2, "merged", [])}
            fl.save_outcomes(tmp, first)
            changed = fl.merge_outcomes(fl.load_outcomes(tmp), {"o/r#1": outcome(1, "rejected", ["data_class"])})
            self.assertEqual(changed["o/r#1"]["state"], "rejected")     # the new decision replaces the old record
            self.assertEqual(changed["o/r#2"]["state"], "merged")       # an unlisted PR keeps its last record

    def test_event_count_counts_decided_outcomes_explicit_votes_and_decisions(self):
        vote = {"pattern": "data_class", "verdict": "reject", "reason": "", "by": "bob"}
        outcomes = {1: outcome(1, "merged", []), 2: outcome(2, "pending", [], votes=[vote]),
                    3: outcome(3, "rejected", [])}
        # 2 decided PRs + 1 vote on the open one + 1 ruling by a person
        self.assertEqual(fl.event_count(outcomes, {"data_class": {}}), 4)
        self.assertEqual(fl.event_count({2: outcome(2, "pending", [])}, {}), 0)   # nothing decided, nothing said


class PipelineIntegrationTests(unittest.TestCase):
    """New review decisions reach the generated skill, and are an automatic reason to regenerate."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = versioning.SkillStore(self.root / "skills")
        self.config = versioning.load_config(None)

    def test_a_rejection_changes_the_next_skill_version_and_triggers_it(self):
        base = doc()
        v1, _ = self.store.create_version(base, T0, feedback_events=0)
        outcomes = {1: outcome(1, "rejected", ["getter_to_property"])}
        events = fl.event_count(outcomes, {})

        decision = versioning.evaluate_trigger(self.config, self.store.load_state(), 17, T0 + timedelta(hours=1), events)
        self.assertTrue(decision.should_run, decision.reason)
        self.assertIn("1 new review decision", decision.reason)

        new_doc = fl.apply_feedback(base, outcomes, {})
        v2, diff = self.store.create_version(new_doc, T0 + timedelta(hours=1), feedback_events=events)
        self.assertEqual(diff["confidence"], [("getter_to_property", "medium", "needs_review")])
        skill = (self.store.root / f"v{v2}" / "java-to-kotlin-android" / "SKILL.md").read_text(encoding="utf-8")
        rules, _, unresolved = skill.partition("## Unresolved")
        self.assertNotIn("Rule getter_to_property.", rules)             # no longer taught as a rule
        self.assertIn("Rule getter_to_property.", unresolved)
        self.assertIn("rejected in 1 reviewed draft(s)", unresolved)
        self.assertIn("Do not apply them", unresolved)
        changes = (self.store.root / f"v{v2}" / "CHANGES.md").read_text(encoding="utf-8")
        self.assertIn("`getter_to_property`: medium -> needs_review", changes)

        # Once that decision is in the baseline it is no longer "new".
        again = versioning.evaluate_trigger(self.config, self.store.load_state(), 17, T0 + timedelta(hours=2), events)
        self.assertFalse(again.should_run)

    def test_a_merge_that_only_adds_evidence_is_not_a_material_change(self):
        base = doc()
        self.store.create_version(base, T0)
        merged = fl.apply_feedback(base, {1: outcome(1, "merged", ["getter_to_property"])}, {})
        diff = versioning.diff_patterns(self.store.read_patterns(1), merged)
        self.assertEqual(diff["evidence"], [("getter_to_property", (0, 0), (1, 0))])   # still listed in a change log
        self.assertFalse(versioning.is_empty(diff))
        self.assertFalse(versioning.is_material(diff))                    # but it does not change what is taught

    def test_what_counts_as_material(self):
        base = doc()
        self.store.create_version(base, T0)
        old = self.store.read_patterns(1)
        demoted = fl.apply_feedback(base, {1: outcome(1, "rejected", ["getter_to_property"])}, {})
        self.assertTrue(versioning.is_material(versioning.diff_patterns(old, demoted)))        # a rule is demoted
        dropped = fl.apply_feedback(base, {}, {"data_class": {"decision": "drop", "reason": "r", "at": T0.isoformat()}})
        self.assertTrue(versioning.is_material(versioning.diff_patterns(old, dropped)))        # a rule is removed
        more = doc(pairs=20, commits=20)
        more["patterns"][0]["sources"] = {"commits": 9, "pairs": 9}
        self.assertTrue(versioning.is_material(versioning.diff_patterns(old, more)))           # new source data
        added = doc(patterns=(("getter_to_property", "medium", 8), ("data_class", "low", 2), ("equals_to_operator", "low", 1)))
        self.assertTrue(versioning.is_material(versioning.diff_patterns(old, added)))          # a rule is added
        self.assertFalse(versioning.is_material(versioning.diff_patterns(old, base)))          # nothing changed

    def test_a_forced_version_still_lists_evidence_in_its_change_log(self):
        base = doc()
        self.store.create_version(base, T0)
        merged = fl.apply_feedback(base, {1: outcome(1, "merged", ["getter_to_property"])}, {})
        v2, _ = self.store.create_version(merged, T0 + timedelta(hours=1), feedback_events=1)
        changes = (self.store.root / f"v{v2}" / "CHANGES.md").read_text(encoding="utf-8")
        self.assertIn("Review evidence changed (accepted/rejected) (1)", changes)
        self.assertIn("`getter_to_property`: 0/0 -> 1/0", changes)

    def test_accepted_drafts_show_up_as_evidence_in_the_skill(self):
        new_doc = fl.apply_feedback(doc(), {1: outcome(1, "merged", ["getter_to_property"])}, {})
        v, _ = self.store.create_version(new_doc, T0)
        skill = (self.store.root / f"v{v}" / "java-to-kotlin-android" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Accepted in 1 reviewed draft(s).", skill)



class PipelineCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.skills = Path(tmp.name) / "skills"

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["--skills-dir", str(self.skills), "feedback", *args])
        return code, out.getvalue(), err.getvalue()

    def test_collect_stores_outcomes_through_the_pr_profile(self):
        fake = FakeGitHub([pr(1, "closed"), pr(2, "closed", merged=True)])
        profiles = []

        def routed(name, args, profile=mcp_client.READ):
            profiles.append(profile)
            return fake(name, args)

        with mock.patch.object(mcp_client, "call_tool", routed), mock.patch.object(mcp_client, "close_session"):
            code, out, _ = self.run_cli("collect", "--repo", "o/r")
        self.assertEqual(code, 0, out)
        self.assertIn("Collected 2 migration PR(s) from o/r", out)
        self.assertTrue(profiles and all(p is mcp_client.PR for p in profiles))
        stored = json.loads((self.skills / "feedback" / "outcomes.json").read_text(encoding="utf-8"))
        self.assertEqual({v["state"] for v in stored.values()}, {"rejected", "merged"})

    def test_resolve_records_a_decision_and_rejects_unknown_patterns(self):
        code, out, _ = self.run_cli("resolve", "data_class", "--keep", "--reason", "fine in practice")
        self.assertEqual(code, 0)
        self.assertIn("data_class -> keep", out)
        self.assertEqual(fl.load_decisions(self.skills / "feedback")["data_class"]["decision"], "keep")
        code, _, err = self.run_cli("resolve", "no_such_pattern", "--drop", "--reason", "x")
        self.assertEqual(code, 1)
        self.assertIn("unknown pattern", err)

    def test_a_ruling_needs_a_choice_and_a_reason(self):
        for args in (("data_class", "--drop"), ("data_class", "--reason", "why")):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                self.run_cli("resolve", *args)
            self.assertEqual(ctx.exception.code, 2, args)          # the command line itself refuses it

    def test_status_with_nothing_recorded(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("no migration PRs recorded yet", out)


if __name__ == "__main__":
    unittest.main()
