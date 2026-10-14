"""Migration-commit filter for the app repo (TDD section 8.4).

A migration is one of:
  - direct: a single commit removes a .java file and adds a .kt file with the
    same path stem in the same directory (delete + add, or R<100 rename).
  - two-step: a pure rename commit (R100, .java -> .kt, no content change)
    followed by the next commit that touches that .kt file, which carries the
    actual conversion. Only that immediate next commit is linked; later edits
    are ignored, so routine work on a renamed file is not mistaken for a
    migration.

Migrations whose rename has no following commit are reported as unfinished.

Commits come from a CommitSource, so the filter logic is independent of where
history is read from:
  - GitCliSource:    local clone via the git CLI (testing, bulk validation).
  - McpCommitSource: read-only Git MCP connector (production, inside the VDI).

Usage: py -3 git_app_connector.py <repo-path>
"""

import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

# One name-status row as git prints it, e.g. ("R100", old, new), ("D", path).
ChangeRow = tuple[str, ...]


@dataclass
class CommitRecord:
    sha: str
    subject: str
    changes: list[ChangeRow]
    parent: str | None = None  # first parent; None for a root commit


@dataclass
class Migration:
    sha: str
    subject: str
    pairs: list = field(default_factory=list)  # (java_path, kt_path)
    kind: str = "direct"                       # "direct" or "two-step"
    conversion_sha: str = ""                   # for two-step: the commit with the conversion
    conversion_subject: str = ""
    parent_sha: str | None = None              # parent of `sha`: where the .java still exists


# Reads a file's text at a commit: read(path, ref) -> text, or None if it doesn't exist.
FileReader = Callable[[str, str], str | None]


@dataclass
class MigrationPair:
    """One before/after tuple, the raw material for pattern extraction (TDD 8.4)."""
    before_path: str
    after_path: str
    before_text: str
    after_text: str
    commit_sha: str            # the migration commit (the rename, for two-step)
    commit_subject: str
    kind: str
    conversion_sha: str = ""   # two-step only: the commit that carries the conversion
    conversion_subject: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class CommitSource(Protocol):
    def commits_oldest_first(self) -> Iterable[CommitRecord]: ...


class GitCliSource:
    """Reads history from a local clone with `git log --name-status -M`."""

    def __init__(self, repo: str):
        self.repo = repo

    def commits_oldest_first(self) -> list[CommitRecord]:
        out = subprocess.run(
            ["git", "-C", self.repo, "log", "--all", "-M", "--name-status",
             "--format=@@COMMIT %H %P@@%s"],
            check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout
        commits: list[CommitRecord] = []
        for line in out.splitlines():
            if line.startswith("@@COMMIT "):
                head, _, subject = line[len("@@COMMIT "):].partition("@@")
                ids = head.split()
                commits.append(CommitRecord(ids[0], subject, [], ids[1] if len(ids) > 1 else None))
            elif line.strip() and commits:
                commits[-1].changes.append(tuple(line.split("\t")))
        # git log is newest-first; reverse so renames are seen before their conversions.
        commits.reverse()
        return commits

    def read_file(self, path: str, ref: str) -> str | None:
        """File text at a commit via `git show ref:path`, or None if it doesn't exist there."""
        result = subprocess.run(
            ["git", "-C", self.repo, "show", f"{ref}:{path}"],
            capture_output=True, text=True, encoding="utf-8",
        )
        return result.stdout if result.returncode == 0 else None


class McpCommitSource:
    """Reads history through a read-only Git MCP connector.

    `call_tool(name, arguments) -> parsed JSON` is supplied by whatever MCP
    client the pipeline uses, so this class needs no MCP SDK itself. Tool names
    and argument names default to the GitHub MCP server's; override them for
    the connector the client actually exposes.

    Shapes observed from the GitHub MCP server (verified against a real repo):
      list tool -> [{"sha": ..., "commit": {"message": ...}}, ...], newest first
      get tool  -> {"sha": ..., "commit": {"message": ...},
                    "files": [{"filename", "status", "changes"?}]}
    Zero-valued counts are omitted, so a missing "changes" means 0 (a pure rename).
    The server does NOT return "previous_filename" for renames. For a renamed .kt
    file the old path is inferred as the same path with a .java extension, then
    confirmed with `file_exists(path, parent_sha)`. Without `file_exists` the
    inference is trusted, which can over-count .kt -> .kt renames.

    Parent commits are taken as the previous commit in the list, which assumes
    linear history (true for the fixture; merge commits need a parents field).

    Cost: one list call per page, one get call per commit, plus one existence
    check per renamed .kt file.
    """

    def __init__(self, call_tool: Callable[[str, dict], Any], owner: str, repo: str,
                 list_tool: str = "list_commits", get_tool: str = "get_commit",
                 page_size: int = 100,
                 file_exists: Callable[[str, str], bool] | None = None):
        self.call_tool = call_tool
        self.owner = owner
        self.repo = repo
        self.list_tool = list_tool
        self.get_tool = get_tool
        self.page_size = page_size
        self.file_exists = file_exists

    def _list_all_shas(self) -> list[str]:
        shas: list[str] = []
        page = 1
        while True:
            batch = self.call_tool(self.list_tool, {
                "owner": self.owner, "repo": self.repo,
                "page": page, "perPage": self.page_size,
            })
            if not batch:
                return shas
            shas.extend(c["sha"] for c in batch)
            page += 1

    def _to_change_rows(self, files: list[dict], parent_sha: str | None) -> list[ChangeRow]:
        rows: list[ChangeRow] = []
        for f in files:
            status = f.get("status")
            name = f["filename"]
            if status == "renamed":
                old = f.get("previous_filename")
                if old is None and name.endswith(".kt"):
                    candidate = name[: -len(".kt")] + ".java"
                    if self.file_exists is None or (
                            parent_sha is not None and self.file_exists(candidate, parent_sha)):
                        old = candidate
                if old is None:
                    rows.append(("M", name))  # rename from an unknown or non-.java path
                    continue
                # No similarity score is available; zero changed lines means a pure rename.
                code = "R100" if f.get("changes", 0) == 0 else "R050"
                rows.append((code, old, name))
            elif status == "added":
                rows.append(("A", name))
            elif status == "removed":
                rows.append(("D", name))
            else:
                rows.append(("M", name))
        return rows

    def commits_oldest_first(self) -> Iterable[CommitRecord]:
        shas = list(reversed(self._list_all_shas()))
        for i, sha in enumerate(shas):
            detail, files = self._get_commit_with_all_files(sha)
            subject = detail["commit"]["message"].splitlines()[0]
            parent = shas[i - 1] if i > 0 else None
            yield CommitRecord(sha, subject, self._to_change_rows(files, parent), parent)

    def _get_commit_with_all_files(self, sha: str) -> tuple[dict, list[dict]]:
        """get_commit pages its file list; keep reading until a short page so no file is lost."""
        files: list[dict] = []
        page = 1
        while True:
            detail = self.call_tool(self.get_tool, {
                "owner": self.owner, "repo": self.repo, "sha": sha,
                "page": page, "perPage": self.page_size,
            })
            batch = detail.get("files") or []
            files.extend(batch)
            if len(batch) < self.page_size:
                return detail, files
            page += 1


def _stem(path: str, ext: str) -> str:
    return path[: -len(ext)]


def find_migrations(source: CommitSource) -> list[Migration]:
    migrations: list[Migration] = []
    pending: dict[str, Migration] = {}  # .kt path -> pure-rename migration awaiting its conversion

    for commit in source.commits_oldest_first():
        sha, subject = commit.sha, commit.subject
        removed_java = {}  # stem -> java path
        added_kt = {}      # stem -> kt path
        touched_kt = {}    # kt path -> status, for every .kt path this commit changes
        pure_rename = set()

        for fields in commit.changes:
            status = fields[0]
            if status.startswith("R") and len(fields) == 3:
                old, new = fields[1], fields[2]
                if old.endswith(".java") and new.endswith(".kt"):
                    removed_java[_stem(old, ".java")] = old
                    added_kt[_stem(new, ".kt")] = new
                    if status == "R100":
                        pure_rename.add(new)
                    touched_kt[new] = status
            elif status == "D" and len(fields) == 2 and fields[1].endswith(".java"):
                removed_java[_stem(fields[1], ".java")] = fields[1]
            elif status == "A" and len(fields) == 2 and fields[1].endswith(".kt"):
                added_kt[_stem(fields[1], ".kt")] = fields[1]
                touched_kt[fields[1]] = status
            elif len(fields) == 2 and fields[1].endswith(".kt"):
                touched_kt[fields[1]] = status

        # A later commit that touches a pure-renamed .kt file is its conversion step.
        for path in touched_kt:
            if path in pending and path not in pure_rename:
                mig = pending.pop(path)
                mig.conversion_sha = sha
                mig.conversion_subject = subject

        pairs = [
            (java, added_kt[stem])
            for stem, java in removed_java.items()
            if stem in added_kt
        ]
        if not pairs:
            continue

        is_pure = any(kt in pure_rename for _, kt in pairs)
        if is_pure:
            mig = Migration(sha, subject, pairs, kind="two-step", parent_sha=commit.parent)
            for _, kt in pairs:
                if kt in pure_rename:
                    pending[kt] = mig
            migrations.append(mig)
        else:
            migrations.append(
                Migration(sha, subject, pairs, kind="direct", parent_sha=commit.parent))

    return migrations


def collect_pairs(migrations: list[Migration], read_file: FileReader) -> tuple[list[MigrationPair], list[str]]:
    """Fetch before/after contents for every migrated file.

    before = the .java at the migration commit's parent (it still exists there).
    after  = the .kt at the conversion commit for two-step migrations (the rename
             commit alone has no real Kotlin yet), or at the migration commit for direct ones.

    Returns (pairs, skipped). A migration is skipped, with a reason, when it is an
    unfinished two-step, has no parent, or either file can't be read.
    """
    pairs: list[MigrationPair] = []
    skipped: list[str] = []
    for m in migrations:
        label = f"{m.sha[:7]} {m.subject}"
        if m.kind == "two-step" and not m.conversion_sha:
            skipped.append(f"{label}: rename has no conversion commit yet")
            continue
        if m.parent_sha is None:
            skipped.append(f"{label}: no parent commit to read the .java from")
            continue
        after_ref = m.conversion_sha if m.kind == "two-step" else m.sha
        for java_path, kt_path in m.pairs:
            before = read_file(java_path, m.parent_sha)
            after = read_file(kt_path, after_ref)
            if before is None or after is None:
                missing = java_path if before is None else kt_path
                skipped.append(f"{label}: could not read {missing}")
                continue
            pairs.append(MigrationPair(
                java_path, kt_path, before, after, m.sha, m.subject, m.kind,
                m.conversion_sha, m.conversion_subject))
    return pairs, skipped


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    migrations = find_migrations(GitCliSource(sys.argv[1]))

    direct = [m for m in migrations if m.kind == "direct"]
    two_step_done = [m for m in migrations if m.kind == "two-step" and m.conversion_sha]
    two_step_open = [m for m in migrations if m.kind == "two-step" and not m.conversion_sha]

    for m in migrations:
        print(f"MIGRATION [{m.kind}] {m.sha[:7]} {m.subject}")
        for java, kt in m.pairs:
            print(f"    {java} -> {kt}")
        if m.conversion_sha:
            print(f"    conversion: {m.conversion_sha[:7]} {m.conversion_subject}")

    print(f"\nDirect migrations:          {len(direct)}")
    print(f"Two-step, conversion found: {len(two_step_done)}")
    print(f"Two-step, no conversion:    {len(two_step_open)}")
    print(f"Total rename/migration events: {len(migrations)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
