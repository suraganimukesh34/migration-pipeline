"""Tests for the skill generator.

Run from the migration-pipeline folder:  py -3 -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
sys.path.insert(0, str(ROOT / "extraction"))
sys.path.insert(0, str(ROOT / "skill_generation"))

import commit_pattern_extractor as cpe  # noqa: E402
import generator  # noqa: E402
import skill_template  # noqa: E402
from git_app_connector import GitCliSource, collect_pairs, find_migrations  # noqa: E402

FIXTURE = str(ROOT / "fixtures" / "migration-fixture-android")
NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def fixture_patterns():
    cli = GitCliSource(FIXTURE)
    pairs, _ = collect_pairs(find_migrations(cli), cli.read_file)
    return cpe.extract_patterns([p.to_dict() for p in pairs])


def make_pattern(pid="p", category="null_safety", rule="A rule.", confidence="medium", pairs=3):
    return {"id": pid, "category": category, "rule": rule, "confidence": confidence,
            "sources": {"commits": pairs, "pairs": pairs},
            "examples": [{"file": "A.java", "commit": "abc1234", "java": "j", "kotlin": "k"}]}


class GeneratorTests(unittest.TestCase):
    def generate(self, doc, version=1):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patterns = Path(tmp.name) / "patterns.json"
        patterns.write_text(json.dumps(doc), encoding="utf-8")
        skill_dir = generator.generate(str(patterns), str(Path(tmp.name) / "out"), version, NOW)
        return skill_dir, (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    def test_fixture_patterns_produce_a_valid_skill(self):
        skill_dir, text = self.generate(fixture_patterns())
        fields, body = generator.parse_frontmatter(text)
        self.assertEqual(skill_dir.name, skill_template.SKILL_NAME)
        self.assertEqual(fields["name"], skill_dir.name)
        self.assertLessEqual(len(fields["description"]), skill_template.MAX_DESCRIPTION)
        self.assertEqual(fields["metadata"]["version"], "1")
        self.assertEqual(fields["metadata"]["source-pairs"], "17")
        self.assertLess(len(body.splitlines()), generator.MAX_BODY_LINES)

    def test_every_pattern_rule_appears_in_the_skill(self):
        doc = fixture_patterns()
        _, text = self.generate(doc)
        for p in doc["patterns"]:
            self.assertIn(p["rule"], text)

    def test_examples_are_rendered_as_fenced_code_blocks(self):
        doc = {"generated_from": {"pairs": 3, "commits": 3}, "patterns": [
            make_pattern("p", rule="Rule.", pairs=3)]}
        doc["patterns"][0]["examples"] = [{"file": "A.java", "commit": "abc1234",
                                           "java": "if (x != null) {\n    go();\n}", "kotlin": "x?.go()"}]
        _, text = self.generate(doc)
        self.assertIn("  ```java\n  if (x != null) {\n      go();\n  }\n  ```", text)
        self.assertIn("  ```kotlin\n  x?.go()\n  ```", text)

    def test_code_containing_backticks_gets_a_longer_fence(self):
        doc = {"generated_from": {"pairs": 3, "commits": 3}, "patterns": [make_pattern("p", pairs=3)]}
        doc["patterns"][0]["examples"] = [{"file": "A.java", "commit": "abc1234",
                                           "java": "s = \"```\";", "kotlin": "val s = \"```\""}]
        _, text = self.generate(doc)
        self.assertIn("  ````java", text)

    def test_rules_are_grouped_under_category_headings(self):
        _, text = self.generate(fixture_patterns())
        for heading in ("### Null safety", "### Data classes", "### Lambdas and anonymous classes"):
            self.assertIn(heading, text)

    def test_stronger_patterns_come_first_within_a_category(self):
        doc = {"generated_from": {"pairs": 20, "commits": 20}, "patterns": [
            make_pattern("weak", rule="Weak rule.", pairs=1, confidence="low"),
            make_pattern("strong", rule="Strong rule.", pairs=12, confidence="high"),
        ]}
        _, text = self.generate(doc)
        self.assertLess(text.index("Strong rule."), text.index("Weak rule."))

    def test_needs_review_conflicts_are_not_presented_as_rules(self):
        doc = {"generated_from": {"pairs": 5, "commits": 5}, "patterns": [
            make_pattern("ok", rule="Safe rule."),
            make_pattern("conflict", category="conflict", confidence="needs_review",
                         rule="History uses RxJava, standards say Coroutines."),
        ]}
        _, text = self.generate(doc)
        rules_section, _, unresolved = text.partition("## Unresolved")
        self.assertIn("Safe rule.", rules_section)
        self.assertNotIn("RxJava", rules_section)
        self.assertIn("RxJava", unresolved)
        self.assertIn("Do not apply them", unresolved)

    def test_empty_pattern_set_still_yields_a_valid_skill(self):
        _, text = self.generate({"generated_from": {"pairs": 0, "commits": 0}, "patterns": []})
        self.assertIn("No patterns have been extracted yet.", text)

    def test_states_that_only_commit_history_was_used(self):
        _, text = self.generate(fixture_patterns())
        self.assertIn("generated from commit history only", text)

    def test_output_is_deterministic(self):
        doc = fixture_patterns()
        self.assertEqual(self.generate(doc)[1], self.generate(doc)[1])

    def test_description_is_capped(self):
        text = skill_template.build_description([f"cat_{i}" * 20 for i in range(100)])
        self.assertLessEqual(len(text), skill_template.MAX_DESCRIPTION)


class ValidationTests(unittest.TestCase):
    def write(self, tmp, folder, name, description="Does a thing.", body="# Body\n"):
        skill_dir = Path(tmp) / folder
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            f'---\nname: {name}\ndescription: {json.dumps(description)}\n---\n\n{body}', encoding="utf-8")
        return skill_dir

    def test_rejects_name_that_does_not_match_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(generator.SkillValidationError):
                generator.validate(self.write(tmp, "folder-a", "folder-b"))

    def test_rejects_invalid_name_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(generator.SkillValidationError):
                generator.validate(self.write(tmp, "Bad_Name", "Bad_Name"))

    def test_rejects_overlong_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(generator.SkillValidationError):
                generator.validate(self.write(tmp, "ok-name", "ok-name", description="x" * 1025))

    def test_rejects_overlong_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(generator.SkillValidationError):
                generator.validate(self.write(tmp, "ok-name", "ok-name", body="line\n" * 600))

    def test_accepts_a_valid_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            generator.validate(self.write(tmp, "ok-name", "ok-name"))


if __name__ == "__main__":
    unittest.main()
