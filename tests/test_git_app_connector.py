"""Tests for the app-repo migration filter, run against the synthetic fixture.

Run from the migration-pipeline folder:  py -3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))

from git_app_connector import (  # noqa: E402
    GitCliSource, McpCommitSource, Migration, collect_pairs, find_migrations,
)

FIXTURE = str(ROOT / "fixtures" / "migration-fixture-android")


def summarize(migrations):
    return [(m.sha, m.kind, m.pairs, m.conversion_sha) for m in migrations]


def make_fake_mcp(source: GitCliSource, calls: list):
    """A fake MCP call_tool that serves the fixture's history in the GitHub commit shape."""
    commits = list(source.commits_oldest_first())
    newest_first = list(reversed(commits))
    by_sha = {c.sha: c for c in commits}

    def to_file(row):
        # Mirrors the real GitHub MCP server: no previous_filename, zero counts omitted.
        code = row[0]
        if code.startswith("R"):
            entry = {"filename": row[2], "status": "renamed"}
            if code != "R100":
                entry["changes"] = 5
            return entry
        status = {"A": "added", "D": "removed", "M": "modified"}[code]
        return {"filename": row[1], "status": status, "changes": 3}

    def call_tool(name, args):
        calls.append((name, dict(args)))
        if name == "list_commits":
            start = (args["page"] - 1) * args["perPage"]
            page = newest_first[start:start + args["perPage"]]
            return [{"sha": c.sha, "commit": {"message": c.subject}} for c in page]
        if name == "get_commit":
            c = by_sha[args["sha"]]
            start = (args["page"] - 1) * args["perPage"]
            window = c.changes[start:start + args["perPage"]]  # file list is paged, like the real server
            return {"sha": c.sha, "commit": {"message": c.subject + "\n\nbody"},
                    "files": [to_file(r) for r in window]}
        raise AssertionError(f"unexpected tool {name}")

    return call_tool


def make_file_exists(source: GitCliSource):
    """file_exists(path, sha): does `path` exist right after commit `sha`? Replays the history."""
    snapshots = {}
    paths = set()
    for c in source.commits_oldest_first():
        for row in c.changes:
            code = row[0]
            if code.startswith("R"):
                paths.discard(row[1])
                paths.add(row[2])
            elif code == "D":
                paths.discard(row[1])
            else:
                paths.add(row[1])
        snapshots[c.sha] = set(paths)
    return lambda path, sha: path in snapshots[sha]


class FixtureTests(unittest.TestCase):
    def test_cli_source_finds_expected_migrations(self):
        migrations = find_migrations(GitCliSource(FIXTURE))
        kinds = sorted(m.kind for m in migrations)
        self.assertEqual(kinds, ["direct"] * 13 + ["two-step"] * 4)
        two_step = {m.pairs[0][0].rsplit("/", 1)[-1]: m for m in migrations if m.kind == "two-step"}
        self.assertEqual(set(two_step), {
            "UserViewModel.java", "OrderViewModel.java", "ProductViewModel.java", "CartService.java"})
        for name, m in two_step.items():
            expected = name[: -len(".java")]
            self.assertEqual(m.conversion_subject, f"refactor: Migrate {expected} to Kotlin")

    def test_noise_commits_are_not_migrations(self):
        subjects = " | ".join(m.subject for m in find_migrations(GitCliSource(FIXTURE)))
        # A new Kotlin file, a KDoc edit after a migration, a rewrite under a new name,
        # and a Java file deleted with no Kotlin counterpart.
        for noise in ("Greeter", "KDoc", "ReportBuilder", "LegacyHelper"):
            self.assertNotIn(noise, subjects)

    def test_conversion_is_linked_past_an_unrelated_commit(self):
        # "feat: add Greeter" sits between the ProductViewModel rename and its conversion.
        migrations = find_migrations(GitCliSource(FIXTURE))
        pvm = next(m for m in migrations if m.pairs[0][0].endswith("ProductViewModel.java"))
        self.assertEqual(pvm.conversion_subject, "refactor: Migrate ProductViewModel to Kotlin")

    def test_later_edit_to_a_migrated_file_is_not_the_conversion(self):
        migrations = find_migrations(GitCliSource(FIXTURE))
        cart = next(m for m in migrations if m.pairs[0][0].endswith("CartService.java"))
        self.assertEqual(cart.conversion_subject, "refactor: Migrate CartService to Kotlin")

    def test_mcp_source_matches_cli_source(self):
        cli = GitCliSource(FIXTURE)
        calls = []
        mcp = McpCommitSource(make_fake_mcp(cli, calls), "owner", "repo", page_size=2,
                              file_exists=make_file_exists(cli))
        self.assertEqual(summarize(find_migrations(mcp)), summarize(find_migrations(cli)))

    def test_mcp_rename_not_from_java_is_ignored(self):
        # A renamed .kt whose .java counterpart does not exist at the parent is not a migration.
        files = [{"filename": "app/Foo.kt", "status": "renamed"}]
        source = McpCommitSource(lambda *_: None, "o", "r", file_exists=lambda path, sha: False)
        self.assertEqual(source._to_change_rows(files, "parentsha"), [("M", "app/Foo.kt")])

    def test_mcp_rename_without_parent_is_ignored_when_verifying(self):
        files = [{"filename": "app/Foo.kt", "status": "renamed"}]
        source = McpCommitSource(lambda *_: None, "o", "r", file_exists=lambda path, sha: True)
        self.assertEqual(source._to_change_rows(files, None), [("M", "app/Foo.kt")])

    def test_pairs_have_before_and_after_contents(self):
        cli = GitCliSource(FIXTURE)
        pairs, skipped = collect_pairs(find_migrations(cli), cli.read_file)
        self.assertEqual(skipped, [])
        self.assertEqual(len(pairs), 17)
        by_name = {p.before_path.rsplit("/", 1)[-1]: p for p in pairs}
        user = by_name["User.java"]
        self.assertIn("public class User", user.before_text)
        self.assertIn("data class User(", user.after_text)
        # Two-step: "after" must come from the conversion commit, not the pure rename.
        vm = by_name["UserViewModel.java"]
        self.assertEqual(vm.kind, "two-step")
        self.assertIn("new UserRepository.Callback()", vm.before_text)
        self.assertIn("{ user ->", vm.after_text)
        self.assertTrue(vm.conversion_subject.startswith("refactor: Migrate UserViewModel"))

    def test_pairs_identical_across_sources(self):
        cli = GitCliSource(FIXTURE)
        mcp = McpCommitSource(make_fake_mcp(cli, []), "owner", "repo", page_size=2,
                              file_exists=make_file_exists(cli))
        # Same file reader on both sides, so any difference comes from parent/commit plumbing.
        from_cli, _ = collect_pairs(find_migrations(cli), cli.read_file)
        from_mcp, _ = collect_pairs(find_migrations(mcp), cli.read_file)
        self.assertEqual([p.to_dict() for p in from_cli], [p.to_dict() for p in from_mcp])

    def test_pairs_skip_unfinished_two_step_and_missing_files(self):
        unfinished = Migration("a" * 40, "rename only", [("A.java", "A.kt")],
                               kind="two-step", parent_sha="p" * 40)
        no_parent = Migration("b" * 40, "root", [("B.java", "B.kt")], parent_sha=None)
        unreadable = Migration("c" * 40, "gone", [("C.java", "C.kt")], parent_sha="p" * 40)
        pairs, skipped = collect_pairs([unfinished, no_parent, unreadable], lambda path, ref: None)
        self.assertEqual(pairs, [])
        self.assertEqual(len(skipped), 3)
        self.assertIn("no conversion commit", skipped[0])
        self.assertIn("no parent", skipped[1])
        self.assertIn("could not read", skipped[2])

    def test_client_allowlist_blocks_write_tools(self):
        import mcp_client
        for tool in ("push_files", "merge_pull_request", "create_or_update_file", "delete_file"):
            with self.assertRaises(PermissionError):
                mcp_client.call_tool(tool, {})
            with self.assertRaises(PermissionError):
                mcp_client.call_tool_text(tool, {})

    def test_mcp_source_paginates_and_fetches_each_commit(self):
        cli = GitCliSource(FIXTURE)
        calls = []
        mcp = McpCommitSource(make_fake_mcp(cli, calls), "owner", "repo", page_size=2,
                              file_exists=make_file_exists(cli))
        find_migrations(mcp)
        total = len(list(cli.commits_oldest_first()))
        list_pages = [a["page"] for n, a in calls if n == "list_commits"]
        # Pages with data, plus one empty page that ends the listing.
        self.assertEqual(list_pages, list(range(1, (total + 1) // 2 + 2)))
        # Every commit is fetched at least once; commits with many files need extra file pages.
        fetched = {a["sha"] for n, a in calls if n == "get_commit"}
        self.assertEqual(len(fetched), total)

    def test_mcp_reads_every_file_page_of_a_large_commit(self):
        cli = GitCliSource(FIXTURE)
        calls = []
        mcp = McpCommitSource(make_fake_mcp(cli, calls), "owner", "repo", page_size=2)
        biggest = max(cli.commits_oldest_first(), key=lambda c: len(c.changes))
        self.assertGreater(len(biggest.changes), 2)  # the fixture must have a commit that spans pages
        got = next(r for r in mcp.commits_oldest_first() if r.sha == biggest.sha)
        self.assertEqual(len(got.changes), len(biggest.changes))
        pages = [a["page"] for n, a in calls if n == "get_commit" and a["sha"] == biggest.sha]
        self.assertGreater(len(pages), 1)


if __name__ == "__main__":
    unittest.main()
