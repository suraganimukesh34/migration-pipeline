"""Tests for the adopt --install-to option and the patterns command.

Run from the migration-pipeline folder:  py -3 -m unittest discover -s tests
"""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skill_generation"))

import pipeline  # noqa: E402
import versioning  # noqa: E402

SKILL = "java-to-kotlin-android"
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def doc(*ids):
    return {"generated_from": {"pairs": 4, "commits": 4}, "patterns": [
        {"id": pid, "category": "properties", "rule": f"Rule {pid}.", "confidence": "medium",
         "sources": {"commits": 4, "pairs": 4},
         "examples": [{"file": "A.java", "commit": "abc1234", "java": "j", "kotlin": "k"}]} for pid in ids]}


class Cli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(["--skills-dir", str(self.dir / "skills"), *args])
        return code, out.getvalue(), err.getvalue()


class AdoptInstallTests(Cli):
    def setUp(self):
        super().setUp()
        store = versioning.SkillStore(self.dir / "skills")
        store.create_version(doc("a"), NOW)
        store.create_version(doc("a", "b"), NOW)
        self.target = self.dir / "agent" / ".claude" / "skills"

    def installed(self):
        return (self.target / SKILL / "SKILL.md").read_text(encoding="utf-8")

    def version_text(self, n):
        return (self.dir / "skills" / f"v{n}" / SKILL / "SKILL.md").read_text(encoding="utf-8")

    def test_adopt_installs_the_skill_where_the_agent_reads_it(self):
        code, out, _ = self.run_cli("adopt", "1", "--install-to", str(self.target))
        self.assertEqual(code, 0)
        self.assertIn("Installed into", out)
        self.assertEqual(self.installed(), self.version_text(1))

    def test_adopting_another_version_replaces_the_installed_one(self):
        self.run_cli("adopt", "1", "--install-to", str(self.target))
        self.run_cli("adopt", "2", "--install-to", str(self.target))
        self.assertEqual(self.installed(), self.version_text(2))

    def test_rollback_reinstalls_the_earlier_version(self):
        self.run_cli("adopt", "1", "--install-to", str(self.target))
        self.run_cli("adopt", "2", "--install-to", str(self.target))
        code, _, _ = self.run_cli("rollback", "--install-to", str(self.target))
        self.assertEqual(code, 0)
        self.assertEqual(self.installed(), self.version_text(1))

    def test_more_than_one_folder_can_be_installed_at_once(self):
        other = self.dir / "other" / "skills"
        self.run_cli("adopt", "1", "--install-to", str(self.target), "--install-to", str(other))
        self.assertTrue((other / SKILL / "SKILL.md").exists())
        self.assertTrue((self.target / SKILL / "SKILL.md").exists())

    def test_without_the_option_nothing_is_installed(self):
        self.run_cli("adopt", "1")
        self.assertFalse(self.target.exists())


class PatternsCommandTests(Cli):
    JAVA = ("package p;\npublic class Person {\n    private final String name;\n"
            "    public Person(String name) {\n        this.name = name;\n    }\n"
            "    public String getName() { return name; }\n}\n")

    def make_repo(self):
        repo = self.dir / "repo"
        module = repo / "src" / "p"
        module.mkdir(parents=True)
        (module / "Person.java").write_text(self.JAVA, encoding="utf-8")
        for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                    ["config", "core.autocrlf", "false"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
        return repo, module

    def test_it_lists_the_patterns_a_conversion_used(self):
        repo, module = self.make_repo()
        (module / "Person.java").unlink()
        (module / "Person.kt").write_text("package p\n\nclass Person(val name: String)\n", encoding="utf-8")
        code, out, _ = self.run_cli("patterns", str(module), "--base", "HEAD")
        self.assertEqual(code, 0)
        self.assertIn("Converted files: 1", out)
        line = next(l for l in out.splitlines() if l.startswith("Skill patterns detected in the conversion:"))
        self.assertIn("constructor_to_primary_val x1", line)

    def test_a_new_kotlin_file_is_not_a_conversion(self):
        repo, module = self.make_repo()
        (module / "Brand.kt").write_text("package p\n\nclass Brand\n", encoding="utf-8")
        _, out, _ = self.run_cli("patterns", str(module), "--base", "HEAD")
        self.assertIn("Converted files: 0", out)
        self.assertIn("Skill patterns detected in the conversion: none", out)

    def test_the_line_is_the_one_the_feedback_loop_reads_back(self):
        sys.path.insert(0, str(ROOT / "feedback_loop"))
        import feedback_loop
        repo, module = self.make_repo()
        (module / "Person.java").unlink()
        (module / "Person.kt").write_text("package p\n\nclass Person(val name: String)\n", encoding="utf-8")
        _, out, _ = self.run_cli("patterns", str(module), "--base", "HEAD")
        self.assertIn("constructor_to_primary_val", feedback_loop.parse_patterns(out))


if __name__ == "__main__":
    unittest.main()
