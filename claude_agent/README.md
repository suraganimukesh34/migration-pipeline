# Claude migration agent (stands in for the Copilot agent of TDD step 5)

Given a Java module, the agent loads the current skill, converts the module to Kotlin, and opens a
draft pull request. People review it; the agent never merges.

| Path | Purpose |
|---|---|
| `.claude/agents/java-to-kotlin-migrator.md` | The agent: how to convert, the steps, and the PR template |
| `.claude/commands/migrate-module.md` | The `/migrate-module <path> [--push]` command |
| `.claude/skills/java-to-kotlin-android/SKILL.md` | The skill. `pipeline.py adopt N --install-to <this .claude/skills>` keeps it current |
| `demo/billing-app/` | A local clone of the sandbox repo, used to try the agent |

## Use it on a repo

1. Copy this folder's `.claude` into the target repository and commit it (the agent needs a clean tree).
2. Set `MIGRATION_PIPELINE` to the pipeline folder (the one with `pipeline.py`).
3. Set `GITHUB_PR_TOKEN` to a fine-grained token for that repository with *Pull requests: read and write*
   and *Contents: read*. Create it yourself and keep it out of chat. It is separate from the read-only
   `GITHUB_TOKEN` used to read history.
4. In Claude Code, in that repository: `/migrate-module <module path> --push`.

## What it does

branch, then for each file a rename commit and a conversion commit, then the PR text, then
`git push` and `pipeline.py pr`, which opens a **draft** PR through GitHub's MCP server.
Without `--push` it stops and asks before publishing.

## What reviewers can do

| Reviewer action | Effect on the playbook |
|---|---|
| Merge the PR | an acceptance vote for each pattern the PR lists |
| Close it without merging | a rejection vote for each of them |
| Comment `skill-feedback: reject <pattern_id> - reason` (or `accept`) | a vote on that one rule |

`pipeline.py feedback collect` reads these decisions and the next `pipeline.py run` writes a new skill
version. A rule with more rejections than acceptances becomes "needs review" and stops being taught
until a person rules on it (`pipeline.py feedback resolve <pattern_id> --keep|--drop --reason "..."`).

## Limits

- Nothing checks the converted code for you: review and UAT are the correctness check, as in the TDD.
  Compile the result before merging.
- Build setup (Kotlin plugin, view binding) is reported in the PR, not changed.
- The skill comes from synthetic history, so its rules are untested on a real codebase.
