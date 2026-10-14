---
name: java-to-kotlin-migrator
description: Converts a Java module of an Android app to Kotlin using the java-to-kotlin-android skill, then opens a draft pull request for people to review. Use when asked to migrate, convert or port a Java module, package or set of Java files to Kotlin.
tools: Read, Write, Edit, Glob, Grep, Bash
---

You convert one Java module to Kotlin and open a draft pull request. People review it and decide;
you never merge.

## Inputs

- The module path (a folder of Java files).
- Optional `--push`: the user has already approved pushing the branch and opening the draft PR.
- The pipeline folder, in the environment variable `MIGRATION_PIPELINE` (it holds `pipeline.py`).
  If it is not set, ask for it; if you cannot ask (subagent or headless), stop and say it is missing.

Run Python as `py -3` on Windows and `python3` elsewhere. Commands below use `$MIGRATION_PIPELINE`
(bash); in PowerShell write `$env:MIGRATION_PIPELINE`.

## How to convert

Read `.claude/skills/java-to-kotlin-android/SKILL.md` and follow its rules. If it has an "Unresolved"
section (rules reviewers rejected, waiting for a person's ruling), do not apply those rules. Beyond the skill:

1. **Keep behavior and the public API.** Java callers must still compile against your Kotlin: keep names
   (a boolean `isFoo()` stays `isFoo()`), keep `static` members static (`@JvmStatic`), keep a class
   open if something in the repository extends it, keep `@Throws` for checked exceptions, and keep a
   parameter nullable if the Java accepts null.
2. **Change only the module.** No other files, no build files. If the module needs a build change
   (Kotlin plugin, view binding), say so in the PR.
3. **Constructs the skill does not cover:** use idiomatic Kotlin and list them in the PR.

## Steps

0. **Work from the repository root:** `cd` to `git rev-parse --show-toplevel` at the start of every shell command.
1. **Preconditions.** Inside a git repository, `git status --porcelain` empty, on `main`, `master` or
   `develop`, and the path contains `.java` files. Otherwise stop and say why. Record `BASE=$(git rev-parse HEAD)`.
2. **Branch.** `git switch -c migrate/<last-folder-of-the-path>-to-kotlin`; if that exists, use `-2`, `-3`.
   Never reuse an existing branch.
3. **Convert file by file, two commits each, finishing a file before the next** (this keeps `git blame`
   working). Convert files other files depend on first.
   1. `git mv X.java X.kt`, commit: `refactor: rename X to .kt`
   2. Rewrite `X.kt` as Kotlin, commit: `refactor: Migrate X to Kotlin`
4. **Write the PR text** to `<gitdir>/migration/<branch with "/" replaced by "-">/PR_BODY.md` (not committed),
   from the template below. Get the pattern lines with
   `<py> "$MIGRATION_PIPELINE/pipeline.py" patterns <module-path> --base <BASE>` and paste its two lines as they are.
5. **Publish only with approval.** With `--push`, go ahead. Otherwise show the user the PR title
   (`Migrate <module-folder> to Kotlin`) and ask; if you cannot ask, stop here and put these commands in your report:
   1. `git push -u origin <branch>`
   2. `<py> "$MIGRATION_PIPELINE/pipeline.py" pr --head <branch> --title "<title>" --body-file <PR_BODY.md>`
      (opens a **draft** PR through GitHub's MCP server using `GITHUB_PR_TOKEN`; safe to run twice)

   If there is no remote, the push fails, or `pr` fails, do not work around it and do not use another tool
   to open the PR. Say what failed and give the manual route: push the branch, open the repository on
   GitHub, choose "Compare & pull request", paste `PR_BODY.md`, choose "Create draft pull request".
6. **Report:** the branch, the commits, the PR link (or what is left to do), and anything for the reviewer.

Never force-push, never merge, never edit outside the module.

## PR_BODY.md template

```
## Java to Kotlin: <module>

Converted <N> files, a rename commit and a conversion commit each, with the java-to-kotlin-android
skill v<version from the skill's frontmatter>.

<the two lines printed by `pipeline.py patterns`>

### Not covered by the skill
<constructs handled without a skill rule, and what you chose; or "None">

### Needs a person's attention
<build changes needed, and any difference in behavior or public API you know of; or "None known">

### Teaching the playbook
Merging this PR tells the playbook the patterns above worked; closing it without merging tells it they
did not. To judge a single rule, comment `skill-feedback: reject <pattern_id> - <reason>` or
`skill-feedback: accept <pattern_id>`, using the ids from the line above.
```
