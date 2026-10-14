"""Tests for skill versioning, the regeneration trigger and the pipeline CLI.

Run from the migration-pipeline folder:  py -3 -m unittest discover -s tests
"""

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skill_generation"))

import pipeline  # noqa: E402
import versioning  # noqa: E402
from skill_template import SKILL_NAME  # noqa: E402

FIXTURE = str(ROOT / "fixtures" / "migration-fixture-android")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def make_doc(patterns=(("a", "low", 1),), pairs=5, commits=5):
    return {"generated_from": {"pairs": pairs, "commits": commits}, "patterns": [
        {"id": pid, "category": "null_safety", "rule": f"Rule {pid}.", "confidence": conf,
         "sources": {"commits": n, "pairs": n},
         "examples": [{"file": "A.java", "commit": "abc1234", "java": "j", "kotlin": "k"}]}
        for pid, conf, n in patterns]}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "skills"
        self.store = versioning.SkillStore(self.root)

    def skill_file(self, version):
        return self.root / f"v{version}" / SKILL_NAME / "SKILL.md"


class CreateVersionTests(StoreTestCase):
    def test_first_version_is_complete_and_pending(self):
        version, _ = self.store.create_version(make_doc(), NOW)
        self.assertEqual(version, 1)
        for name in (f"{SKILL_NAME}/SKILL.md", "patterns.json", "CHANGES.md"):
            self.assertTrue((self.root / "v1" / name).exists(), name)
        state = self.store.load_state()
        self.assertEqual(state["versions"]["1"]["status"], "pending")
        self.assertIsNone(state["current"])
        self.assertEqual(state["baseline"]["commits"], 5)

    def test_skill_records_its_version(self):
        self.store.create_version(make_doc(), NOW)
        self.store.create_version(make_doc(), NOW)
        self.assertIn('version: "2"', self.skill_file(2).read_text(encoding="utf-8"))

    def test_versions_are_never_overwritten(self):
        self.store.create_version(make_doc((("a", "low", 1),)), NOW)
        before = {p.name: sha(p) for p in (self.root / "v1").rglob("*") if p.is_file()}
        version, _ = self.store.create_version(make_doc((("a", "low", 1), ("b", "low", 1))), NOW)
        after = {p.name: sha(p) for p in (self.root / "v1").rglob("*") if p.is_file()}
        self.assertEqual(version, 2)
        self.assertEqual(before, after)

    def test_lost_state_file_cannot_cause_an_overwrite(self):
        self.store.create_version(make_doc(), NOW)
        original = sha(self.skill_file(1))
        self.store.state_path.unlink()
        version, _ = self.store.create_version(make_doc((("z", "high", 12),)), NOW)
        self.assertEqual(version, 2)
        self.assertEqual(sha(self.skill_file(1)), original)

    def test_failed_render_leaves_no_partial_version(self):
        def boom(*args, **kwargs):
            raise RuntimeError("render failed")

        with self.assertRaises(RuntimeError):
            self.store.create_version(make_doc(), NOW, render=boom)
        self.assertEqual(list(self.root.glob("v*")), [])
        self.assertEqual(list(self.root.glob(".staging-*")), [])
        self.assertEqual(self.store.load_state()["versions"], {})

    def test_invalid_skill_is_rejected_and_not_kept(self):
        def bad_render(doc, out_dir, version, now):
            skill = Path(out_dir) / SKILL_NAME
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
            versioning.generator.validate(skill)

        with self.assertRaises(versioning.generator.SkillValidationError):
            self.store.create_version(make_doc(), NOW, render=bad_render)
        self.assertEqual(list(self.root.glob("v*")), [])


class ChangeLogTests(StoreTestCase):
    def test_diff_finds_added_removed_confidence_and_count_changes(self):
        old = make_doc((("keep", "low", 2), ("gone", "low", 1), ("grows", "medium", 3), ("up", "low", 2)))
        new = make_doc((("keep", "low", 2), ("new", "low", 1), ("grows", "medium", 6), ("up", "medium", 3)))
        diff = versioning.diff_patterns(old, new)
        self.assertEqual(diff["added"], ["new"])
        self.assertEqual(diff["removed"], ["gone"])
        self.assertEqual(diff["confidence"], [("up", "low", "medium")])
        self.assertEqual(diff["counts"], [("grows", 3, 6), ("up", 2, 3)])

    def test_identical_pattern_sets_have_an_empty_diff(self):
        self.assertTrue(versioning.is_empty(versioning.diff_patterns(make_doc(), make_doc())))

    def test_changes_file_describes_the_differences(self):
        self.store.create_version(make_doc((("a", "low", 2), ("b", "low", 1)), pairs=3, commits=3), NOW)
        self.store.create_version(make_doc((("a", "medium", 4), ("c", "low", 1)), pairs=5, commits=5), NOW)
        text = (self.root / "v2" / "CHANGES.md").read_text(encoding="utf-8")
        self.assertIn("changes since v1", text)
        self.assertIn("3 -> 5 converted files", text)
        self.assertIn("`c`", text)                      # added
        self.assertIn("`b`", text)                      # removed
        self.assertIn("`a`: low -> medium", text)       # confidence
        self.assertIn("`a`: 2 -> 4 files", text)        # count

    def test_first_version_changes_say_initial(self):
        self.store.create_version(make_doc(), NOW)
        self.assertIn("initial version", (self.root / "v1" / "CHANGES.md").read_text(encoding="utf-8"))

    def test_unchanged_patterns_are_reported_as_such(self):
        self.store.create_version(make_doc(), NOW)
        self.store.create_version(make_doc(), NOW)
        self.assertIn("No pattern changes.", (self.root / "v2" / "CHANGES.md").read_text(encoding="utf-8"))


class AdoptRollbackTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.create_version(make_doc((("a", "low", 1),)), NOW)
        self.store.create_version(make_doc((("a", "low", 1), ("b", "low", 1))), NOW)

    def current_text(self):
        return (self.root / "current" / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")

    def test_adopt_copies_the_version_and_updates_state(self):
        self.store.adopt(1)
        self.assertEqual(self.current_text(), self.skill_file(1).read_text(encoding="utf-8"))
        state = self.store.load_state()
        self.assertEqual(state["current"], 1)
        self.assertEqual(state["versions"]["1"]["status"], "current")
        self.assertEqual(state["versions"]["2"]["status"], "pending")

    def test_adopting_another_version_supersedes_the_first(self):
        self.store.adopt(1)
        self.store.adopt(2)
        state = self.store.load_state()
        self.assertEqual(self.current_text(), self.skill_file(2).read_text(encoding="utf-8"))
        self.assertEqual(state["versions"]["1"]["status"], "superseded")
        self.assertEqual(state["current"], 2)

    def test_adopting_never_changes_the_version_folders(self):
        before = (sha(self.skill_file(1)), sha(self.skill_file(2)))
        self.store.adopt(2)
        self.store.adopt(1)
        self.assertEqual(before, (sha(self.skill_file(1)), sha(self.skill_file(2))))

    def test_rollback_returns_to_the_previously_adopted_version(self):
        self.store.adopt(1)
        self.store.adopt(2)
        self.assertEqual(self.store.rollback(), 1)
        self.assertEqual(self.store.load_state()["current"], 1)
        self.assertEqual(self.current_text(), self.skill_file(1).read_text(encoding="utf-8"))

    def test_rollback_with_no_earlier_version_fails_clearly(self):
        with self.assertRaises(versioning.VersioningError):
            self.store.rollback()
        self.store.adopt(1)
        with self.assertRaises(versioning.VersioningError):
            self.store.rollback()

    def test_adopting_a_missing_version_fails(self):
        with self.assertRaises(versioning.VersioningError):
            self.store.adopt(9)

    def test_record_check_resets_the_baseline_without_a_new_version(self):
        self.store.record_check(40, NOW)
        state = self.store.load_state()
        self.assertEqual(state["baseline"]["commits"], 40)
        self.assertEqual(sorted(state["versions"]), ["1", "2"])


class TriggerTests(unittest.TestCase):
    def config(self, mode="either", new_commits=10, schedule_days=7):
        return {"trigger": {"mode": mode, "new_commits": new_commits, "schedule_days": schedule_days}}

    def state(self, commits=20, days_ago=1):
        return {"baseline": {"commits": commits, "at": (NOW - timedelta(days=days_ago)).isoformat()}}

    def due(self, config, state, commits_now):
        return versioning.evaluate_trigger(config, state, commits_now, NOW).should_run

    def test_first_run_is_always_due(self):
        decision = versioning.evaluate_trigger(self.config(), {"baseline": None}, 0, NOW)
        self.assertTrue(decision.should_run)
        self.assertIn("no previous generation", decision.reason)

    def test_new_commits_threshold(self):
        cfg = self.config("new_commits", new_commits=10)
        self.assertFalse(self.due(cfg, self.state(20), 29))
        self.assertTrue(self.due(cfg, self.state(20), 30))

    def test_new_commits_mode_ignores_the_schedule(self):
        self.assertFalse(self.due(self.config("new_commits"), self.state(20, days_ago=30), 20))

    def test_schedule_threshold(self):
        cfg = self.config("schedule", schedule_days=7)
        self.assertFalse(self.due(cfg, self.state(days_ago=6), 100))
        self.assertTrue(self.due(cfg, self.state(days_ago=7), 20))

    def test_either_fires_on_whichever_comes_first(self):
        cfg = self.config("either", new_commits=10, schedule_days=7)
        self.assertTrue(self.due(cfg, self.state(20, days_ago=1), 35))
        self.assertTrue(self.due(cfg, self.state(20, days_ago=9), 21))
        self.assertFalse(self.due(cfg, self.state(20, days_ago=1), 21))

    def test_fewer_commits_than_baseline_is_not_due(self):
        self.assertFalse(self.due(self.config("new_commits"), self.state(20), 15))

    def test_reason_states_the_facts(self):
        reason = versioning.evaluate_trigger(self.config(), self.state(20), 23, NOW).reason
        self.assertIn("3 new migration commit(s)", reason)
        self.assertIn("threshold 10", reason)


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(text, encoding="utf-8")
            return versioning.load_config(path)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(versioning.load_config("no-such-file.json"), versioning.DEFAULT_CONFIG)

    def test_file_overrides_only_what_it_sets(self):
        config = self.load('{"trigger": {"new_commits": 3}}')
        self.assertEqual(config["trigger"], {"mode": "either", "new_commits": 3, "schedule_days": 7})

    def test_rejects_unknown_mode(self):
        with self.assertRaises(versioning.VersioningError):
            self.load('{"trigger": {"mode": "hourly"}}')

    def test_rejects_non_positive_or_non_integer_values(self):
        for bad in ('{"trigger": {"new_commits": 0}}', '{"trigger": {"schedule_days": "7"}}',
                    '{"trigger": {"new_commits": true}}', '{"trigger": {"schedule_days": -1}}'):
            with self.assertRaises(versioning.VersioningError, msg=bad):
                self.load(bad)

    def test_shipped_config_is_valid(self):
        versioning.load_config(ROOT / "pipeline.config.json")


class PipelineCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.skills = Path(tmp.name) / "skills"
        self.config = Path(tmp.name) / "config.json"
        self.config.write_text(json.dumps(
            {"trigger": {"mode": "either", "new_commits": 10, "schedule_days": 7}}), encoding="utf-8")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["--config", str(self.config), "--skills-dir", str(self.skills), *args])
        return code, out.getvalue(), err.getvalue()

    def state(self):
        return json.loads((self.skills / "state.json").read_text(encoding="utf-8"))

    def test_first_run_creates_a_pending_version(self):
        code, out, _ = self.run_cli("run", "--local", FIXTURE)
        self.assertEqual(code, 0)
        self.assertIn("Created v1 (pending)", out)
        self.assertIn("initial version", out)
        self.assertEqual(self.state()["versions"]["1"]["status"], "pending")
        self.assertTrue((self.skills / "v1" / SKILL_NAME / "SKILL.md").exists())

    def test_second_run_is_not_due_and_makes_no_version(self):
        self.run_cli("run", "--local", FIXTURE)
        code, out, _ = self.run_cli("run", "--local", FIXTURE)
        self.assertEqual(code, 0)
        self.assertIn("Not due", out)
        self.assertEqual(list(self.state()["versions"]), ["1"])

    def test_force_creates_a_new_version_even_with_no_changes(self):
        self.run_cli("run", "--local", FIXTURE)
        _, out, _ = self.run_cli("run", "--local", FIXTURE, "--force")
        self.assertIn("Created v2", out)
        self.assertIn("No pattern changes.", out)

    def test_due_but_unchanged_patterns_skip_the_version_and_reset_the_baseline(self):
        self.run_cli("run", "--local", FIXTURE)
        state = self.state()
        state["baseline"]["at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        (self.skills / "state.json").write_text(json.dumps(state), encoding="utf-8")

        _, out, _ = self.run_cli("run", "--local", FIXTURE)
        self.assertIn("no pattern changes since v1", out)
        self.assertEqual(list(self.state()["versions"]), ["1"])
        age = datetime.now(timezone.utc) - datetime.fromisoformat(self.state()["baseline"]["at"])
        self.assertLess(age, timedelta(minutes=5))

    def test_adopt_status_and_rollback_through_the_cli(self):
        self.run_cli("run", "--local", FIXTURE)
        self.run_cli("run", "--local", FIXTURE, "--force")
        self.assertEqual(self.run_cli("adopt", "1")[0], 0)
        self.run_cli("adopt", "2")
        _, out, _ = self.run_cli("status")
        self.assertIn("Current (adopted): v2", out)
        self.assertIn("v1: superseded", out)
        _, out, _ = self.run_cli("rollback")
        self.assertIn("v1 is now current", out)

    def test_errors_exit_nonzero_with_a_message(self):
        code, _, err = self.run_cli("adopt", "5")
        self.assertEqual(code, 1)
        self.assertIn("v5 does not exist", err)
        code, _, err = self.run_cli("rollback")
        self.assertEqual(code, 1)
        self.assertIn("nothing to roll back", err)

    def test_status_before_any_run(self):
        _, out, _ = self.run_cli("status")
        self.assertIn("No versions yet", out)


if __name__ == "__main__":
    unittest.main()
