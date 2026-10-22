# Java-to-Kotlin migration pipeline

An agent converts a Java module to Kotlin using rules learned from how your team did it before. People
review the result, and their decisions make the next set of rules better.

In short:

- Your app repo already holds examples of Java files turned into Kotlin. The pipeline reads that history
  and writes down the patterns people used. That written set of rules is the **skill** (a playbook).
- An **agent** reads the skill, converts a Java module you give it, and opens a draft pull request.
- People review the PR. Merging, closing, or commenting on it is feedback.
- The pipeline reads that feedback and proposes a new skill version. A person approves it before the
  agent uses it.

This follows the technical design document (TDD), with one source (the app repo's commit history) and
Claude standing in for the Copilot agent, so it can be tested outside the client VDI.

## The flow (TDD step in brackets)

**Automatic** = the pipeline or agent does it. **Person** = a human decides.

1. **Read the history (automatic, 1).** `pipeline.py run --mcp <owner> <repo>` reads the app repo's
   commits through GitHub's MCP server, read-only. Nothing is cloned, so nothing needs updating. It
   keeps only migration commits: a `.java` file deleted and a `.kt` file of the same name added, or a
   100% rename followed by a commit that edits the `.kt` file.
2. **Find the patterns (automatic, 3).** For each migration it compares the file before and after and
   detects 15 known patterns (getter to property, null ternary to elvis, anonymous class to lambda,
   static members to companion object, and so on). Each rule gets a confidence level and real examples.
3. **Write a skill version (automatic, 4).** The rules are rendered as a skill (`SKILL.md`) and saved as
   a numbered version with a change log against the previous one. It starts as **pending**. A version is
   written only when something that changes what the agent is taught changed: a rule added, removed or
   demoted, or its confidence changed. Review counts alone do not create a version.
4. **Review and adopt the skill (person).** Read the change log, then adopt the version (below).
   Every version is kept and `rollback` returns to the previous one. The agent only uses the adopted
   version.
5. **Convert a module (automatic, 5).** The agent (`claude_agent/`) is given a Java module and reads the
   adopted skill. It keeps behavior and public API the same, makes two commits per file (a pure rename,
   then the edit), pushes `migrate/<module>-to-kotlin` and opens a **draft PR**. The PR description
   lists the skill version used and the patterns that appeared in the conversion. That link is what lets
   reviews teach the skill.
6. **Review the PR (person, 6-8).** Review, test and promote it as usual. This is the correctness check;
   the pipeline does not compile or test the converted code.
   - **Merge**: accepted, for every pattern the PR lists.
   - **Close** without merging: rejected.
   - **Comment** to vote on one pattern: `skill-feedback: reject static_to_companion - utility classes
     should be objects`. Named votes count immediately and outrank the merge or close signal.
7. **Collect the feedback (automatic, 9).** `pipeline.py feedback collect --repo <owner>/<name>` reads
   the migration PRs and what happened to them. A rule rejected more than accepted moves to
   `needs_review`, and the agent stops applying it until a person rules on it.
8. **Rule on contested patterns (person).** Keep or drop the rule, with a reason. The record of why a
   rule changed is the point.
9. **Next version (automatic, 9).** `run` is due again when enough new migration commits land, on a
   schedule, or when new review decisions exist. A material change writes the next version, and the
   loop returns to step 4.

### Who decides what

| Question | Answer |
|---|---|
| Does every new skill version need review? | Only versions that change what the agent is taught. Evidence-only changes make no version. |
| Who approves a version? | A person. Nothing is adopted automatically. |
| Who checks the converted code? | The normal PR review and UAT. The pipeline does not. |
| I disagree with a rule | Comment on a PR, or run `feedback resolve`. |

## Setup

Clone this repo. The pipeline reads the app repo's history over GitHub MCP, so the app repo needs no
clone (the sample used here is `suraganimukesh34/migration-fixture-android`). The agent works in a
sandbox repo, which it needs cloned. It is a separate repo, so it is not stored inside this one:

```
git clone https://github.com/suraganimukesh34/acme-shop claude_agent/demo/orders-app
```

To use your own repos, point the commands at them instead. The `skills/` folder is generated output
(versions, collected review outcomes); it is created by the first `run`.

- Python 3 and the MCP package: `py -3 -m pip install mcp`.
- `GITHUB_TOKEN`: a read-only fine-grained token for the app repo (history).
- `GITHUB_PR_TOKEN`: a fine-grained token for the repo the agent works in, with Pull requests
  read/write and Contents read/write (open PRs, read review decisions, push skill proposals).
- `MIGRATION_PIPELINE`: the path of this folder, for the agent.
- Settings (trigger): `pipeline.config.json`.

Set tokens yourself as environment variables in your own terminal. Never paste a token into chat, a
ticket, or a file in a repo.

## Commands

Run from this folder.

| Task | Command |
|---|---|
| Learn from the history; writes a pending version if one is due (`--force` ignores the trigger) | `py -3 pipeline.py run --mcp <owner> <repo>` |
| Same, and if a version is created, also put it up as a draft PR from a local clone | `py -3 pipeline.py run --mcp <owner> <repo> --propose-repo-path <local clone> --propose-repo <owner>/<name>` |
| Show versions and the trigger baseline | `py -3 pipeline.py status` |
| Adopt a version and install it for the agent | `py -3 pipeline.py adopt <n> --install-to <repo>/.claude/skills` |
| Return to the previous version | `py -3 pipeline.py rollback --install-to <repo>/.claude/skills` |
| Put a version up for review as a draft PR on the repo (branch `skill/v<n>`, change log as description) | `py -3 pipeline.py propose <n> --repo-path <local clone> --repo <owner>/<name>` |
| Which skill patterns a finished conversion used (the agent runs this) | `py -3 pipeline.py patterns <module folder> --base <revision before conversion>` |
| Open a draft PR for a pushed branch (the agent runs this) | `py -3 pipeline.py pr --repo <owner>/<name> --title "..." --body-file PR_BODY.md` |
| Read what reviewers decided | `py -3 pipeline.py feedback collect --repo <owner>/<name>` |
| Show outcomes and votes per pattern | `py -3 pipeline.py feedback status` |
| Rule on a contested pattern | `py -3 pipeline.py feedback resolve <pattern_id> --keep\|--drop --reason "..."` |

`--keep` keeps the rule and counts only later outcomes. `--drop` removes it.

Reviewing a skill version as a PR: after `propose`, merging the PR accepts it; then run
`pipeline.py adopt <n>` so the pipeline records it. Closing the PR discards the proposal; the version
stays saved.

### A typical round

1. `run`, read the change log, adopt the version (or `propose` it and merge the PR).
2. Give the agent a Java module. It opens a draft PR.
3. Review it: merge, close, or add `skill-feedback:` comments.
4. `feedback collect`, then `run`. If a rule was contested, `feedback resolve`, then `run` again.
5. Review the next version and repeat.

## Folders

| Folder | What it is |
|---|---|
| `connectors/` | read-only GitHub MCP client; migration-commit filter; PR creation |
| `extraction/` | pattern extraction from before/after pairs; which patterns a conversion used |
| `skill_generation/` | renders the skill (Copilot/Claude skill format); versions, change log, trigger; proposing a version as a PR |
| `feedback_loop/` | reads what reviewers decided and applies it to the pattern set |
| `claude_agent/` | the migration agent, its command, and a demo clone of the sandbox repo |
| `skills/` | generated output: versions, the adopted skill, collected review outcomes |

## Not here yet

- The library repo and Confluence sources and the merge between sources (TDD steps 1b, 2, and the merge
  in 3): there is no access to them. Their place in the flow is the same as the commit history's.
- Checks on the converted code (compiling, API comparison): not part of the TDD and removed. Review and
  UAT are the correctness check.
- Running on GitHub Copilot inside the client VDI. This version uses Claude as the stand-in.
