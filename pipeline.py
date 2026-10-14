"""Java-to-Kotlin migration pipeline, following the technical design.

  Build the playbook   run       read the app repo's migration history, extract patterns, write a skill version
                       status    show versions and which one is adopted
                       adopt     make a version the current one (and install it where the agent reads it)
                       rollback  go back to the previously adopted version
  Convert              patterns  which skill patterns a finished conversion used (goes in the PR)
                       pr        open a draft pull request for the agent's pushed branch
  Learn from review    feedback  collect what reviewers decided about the drafts

Typical flow:
  py -3 pipeline.py run --mcp <owner> <repo>              step 1-4: new skill version (pending), with a change log
  py -3 pipeline.py adopt 1 --install-to <folder>         a person reads the change log, then adopts it
  py -3 pipeline.py propose 2 --repo-path <clone>         or: put a version up for review as a PR (merge = adopt)
  ... the agent converts a module and opens a PR (pipeline.py patterns, pipeline.py pr) ...
  ... people review it: merge, close, or comment `skill-feedback: reject <pattern> - why` ...
  py -3 pipeline.py feedback collect --repo <owner/name>  step 9: read the decisions
  py -3 pipeline.py run --mcp <owner> <repo>              the next skill version learns from them

`run --local <repo>` reads a local clone instead of GitHub (for testing without a network).
The regeneration trigger is in pipeline.config.json: new migration commits, a schedule, or a review decision.
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for sub in ("connectors", "extraction", "skill_generation", "feedback_loop"):
    sys.path.insert(0, str(ROOT / sub))

import applied_patterns  # noqa: E402
import feedback_loop  # noqa: E402
import github_pr  # noqa: E402
from commit_pattern_extractor import PATTERNS, extract_patterns  # noqa: E402
from git_app_connector import GitCliSource, McpCommitSource, collect_pairs, find_migrations  # noqa: E402
from skill_template import SKILL_NAME  # noqa: E402
import proposal  # noqa: E402
from versioning import (  # noqa: E402
    SkillStore, VersioningError, diff_patterns, evaluate_trigger, is_empty, is_material, load_config, utc_now,
)


def read_history(args) -> "tuple[dict, list[str]]":
    """Steps 1 and 3: read the app repo's migration history and extract patterns from it."""
    if args.local:
        source = GitCliSource(args.local)
        migrations = find_migrations(source)
        pairs, skipped = collect_pairs(migrations, source.read_file)
    else:
        import mcp_client  # imported here so --local works without the mcp package or a token

        owner, repo = args.mcp

        def file_exists(path: str, ref: str) -> bool:
            is_error, _ = mcp_client.call_tool_text(
                "get_file_contents", {"owner": owner, "repo": repo, "path": path, "ref": ref})
            return not is_error

        try:
            source = McpCommitSource(mcp_client.call_tool, owner, repo, file_exists=file_exists)
            migrations = find_migrations(source)
            pairs, skipped = collect_pairs(
                migrations, lambda path, ref: mcp_client.read_file(owner, repo, path, ref))
        finally:
            mcp_client.close_session()
    return extract_patterns([p.to_dict() for p in pairs]), skipped


def _feedback_dir(store: SkillStore) -> Path:
    return store.root / "feedback"


def cmd_run(args, config: dict, store: SkillStore) -> int:
    patterns_doc, skipped = read_history(args)
    for reason in skipped:
        print(f"SKIPPED {reason}")
    commits_now = patterns_doc["generated_from"]["commits"]
    print(f"Read {patterns_doc['generated_from']['pairs']} converted files across {commits_now} "
          f"migration commits; {len(patterns_doc['patterns'])} pattern(s).")

    # Step 9: what reviewers decided about earlier drafts flows back into the pattern set.
    outcomes = feedback_loop.load_outcomes(_feedback_dir(store))
    decisions = feedback_loop.load_decisions(_feedback_dir(store))
    patterns_doc = feedback_loop.apply_feedback(patterns_doc, outcomes, decisions)
    events = feedback_loop.event_count(outcomes, decisions)
    summary = patterns_doc["feedback"]
    if summary["decided"] or decisions:
        print(f"Review feedback: {summary['decided']} decided draft(s), {summary['pending']} pending; "
              f"needs review: {', '.join(summary['demoted']) or 'none'}; "
              f"dropped: {', '.join(summary['dropped']) or 'none'}.")

    now = utc_now()
    decision = evaluate_trigger(config, store.load_state(), commits_now, now, feedback_events=events)
    if not args.force and not decision.should_run:
        print(f"Not due: {decision.reason}. No new version created.")
        return 0

    previous = store.latest_version()
    if previous is not None and not args.force:
        diff = diff_patterns(store.read_patterns(previous), patterns_doc)
        if not is_material(diff):
            store.record_check(commits_now, now, feedback_events=events)
            if is_empty(diff):
                print(f"Due, but no pattern changes since v{previous}. No new version created.")
            else:
                print(f"Due, but only review evidence changed ({len(diff['evidence'])} pattern(s)). That does not "
                      f"change what the agent is taught, so no new version was created; the evidence will be "
                      f"included in the next version that does.")
            return 0

    version, _ = store.create_version(patterns_doc, now, feedback_events=events)
    print(f"Created v{version} (pending) at {store.root / f'v{version}'}")
    print()
    print((store.root / f"v{version}" / "CHANGES.md").read_text(encoding="utf-8"))
    print(f"Review the changes, then adopt it:  py -3 pipeline.py adopt {version}")
    return 0


def cmd_status(args, config: dict, store: SkillStore) -> int:
    state = store.load_state()
    print(f"Skills folder: {store.root}")
    print(f"Current (adopted): {'v' + str(state['current']) if state['current'] else 'none'}")
    if state["baseline"]:
        print(f"Trigger baseline: {state['baseline']['commits']} migration commits at {state['baseline']['at'][:19]}")
    trigger = config["trigger"]
    print(f"Trigger: {trigger['mode']} (new_commits={trigger['new_commits']}, schedule_days={trigger['schedule_days']})")
    versions = store.history()
    if not versions:
        print("No versions yet. Run: py -3 pipeline.py run --mcp <owner> <repo>")
    for v in versions:
        print(f"  v{v['version']}: {v['status']:10} {v['patterns']} pattern(s), {v['pairs']} file(s), "
              f"{v['commits']} commit(s), created {v['created'][:19]}"
              + (f" | proposed in {v['proposal']}" if v.get("proposal") else ""))
    return 0


def _install(store: SkillStore, targets: "list[str] | None") -> None:
    """Copy the adopted skill into each folder the agent reads skills from."""
    for target in targets or []:
        destination = Path(target) / SKILL_NAME
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(store.root / "current" / SKILL_NAME, destination)
        print(f"Installed into {destination}")


def cmd_adopt(args, config: dict, store: SkillStore) -> int:
    store.adopt(args.version)
    print(f"v{args.version} is now current (copied to {store.root / 'current'}).")
    _install(store, args.install_to)
    return 0


def cmd_rollback(args, config: dict, store: SkillStore) -> int:
    print(f"Rolled back: v{store.rollback()} is now current.")
    _install(store, args.install_to)
    return 0


def cmd_propose(args, config: dict, store: SkillStore) -> int:
    import mcp_client

    repo = Path(args.repo_path)
    owner, name = github_pr.split_repo(args.repo) if args.repo else github_pr.repo_from_git(repo)
    branch = proposal.prepare_branch(store.root, args.version, repo, args.base)
    try:
        proposal.push(repo, branch)
        result = github_pr.create_draft_pull_request(
            lambda n, a: mcp_client.call_tool(n, a, mcp_client.PR), owner, name, branch, args.base,
            f"Update the {SKILL_NAME} skill to v{args.version}", proposal.body(store.root, args.version))
    finally:
        mcp_client.close_session()
        proposal.back_to(repo, args.base)
    store.mark_proposed(args.version, result.url)
    print(("Proposed " if result.created else "Already proposed: ") + f"v{args.version} as {result.url}")
    print(f"Merging that PR adopts it. Afterwards run: py -3 pipeline.py adopt {args.version}")
    return 0


def cmd_patterns(args, config: dict, store: SkillStore) -> int:
    print(applied_patterns.describe(applied_patterns.conversion_pairs(args.module, args.base)))
    return 0


def _current_branch() -> str:
    import subprocess

    done = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    if done.returncode != 0:
        raise github_pr.PullRequestError("not inside a git repository; run this from the repo or pass --head")
    return done.stdout.strip()


def cmd_pr(args, config: dict, store: SkillStore) -> int:
    import mcp_client

    owner, repo = github_pr.split_repo(args.repo) if args.repo else github_pr.repo_from_git()
    head = args.head or _current_branch()
    body_path = Path(args.body_file)
    if not body_path.is_file():
        raise github_pr.PullRequestError(f"PR body file not found: {body_path}")
    try:
        result = github_pr.create_draft_pull_request(
            lambda n, a: mcp_client.call_tool(n, a, mcp_client.PR), owner, repo, head, args.base, args.title,
            body_path.read_text(encoding="utf-8"))
    finally:
        mcp_client.close_session()
    print(("Opened draft pull request: " if result.created else "A pull request is already open for this branch: ")
          + result.url)
    return 0


def _latest_patterns(store: SkillStore) -> "dict | None":
    latest = store.latest_version()
    return store.read_patterns(latest) if latest is not None else None


def cmd_feedback(args, config: dict, store: SkillStore) -> int:
    directory = _feedback_dir(store)

    if args.feedback_command == "collect":
        import mcp_client

        owner, repo = github_pr.split_repo(args.repo) if args.repo else github_pr.repo_from_git()
        try:
            collected = feedback_loop.collect_outcomes(
                lambda n, a: mcp_client.call_tool(n, a, mcp_client.PR), owner, repo)
        finally:
            mcp_client.close_session()
        outcomes = feedback_loop.merge_outcomes(feedback_loop.load_outcomes(directory), collected)
        feedback_loop.save_outcomes(directory, outcomes)
        print(f"Collected {len(collected)} migration PR(s) from {owner}/{repo}.")
        print(feedback_loop.describe(outcomes, feedback_loop.load_decisions(directory), _latest_patterns(store)))
        return 0

    if args.feedback_command == "status":
        print(feedback_loop.describe(feedback_loop.load_outcomes(directory), feedback_loop.load_decisions(directory),
                                     _latest_patterns(store)))
        return 0

    known = {spec.id for spec in PATTERNS}
    if args.pattern not in known:
        raise feedback_loop.FeedbackError(f"unknown pattern {args.pattern!r}. Known patterns: {', '.join(sorted(known))}")
    decision = feedback_loop.record_decision(
        directory, args.pattern, "keep" if args.keep else "drop", args.reason or "")
    print(f"Recorded: {args.pattern} -> {decision['decision']} ({decision['reason']}). "
          "It takes effect the next time the pipeline runs.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(ROOT / "pipeline.config.json"))
    parser.add_argument("--skills-dir", help="override the skills folder from the config")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="extract patterns and write a new skill version if due")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--mcp", nargs=2, metavar=("OWNER", "REPO"), help="read the repo through GitHub's MCP server")
    source.add_argument("--local", metavar="REPO", help="read a local clone with the git CLI")
    run.add_argument("--force", action="store_true", help="ignore the trigger and the no-change check")
    run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="show versions and the trigger baseline").set_defaults(func=cmd_status)

    adopt = sub.add_parser("adopt", help="make a version current")
    adopt.add_argument("version", type=int)
    adopt.add_argument("--install-to", action="append", metavar="FOLDER",
                       help="also copy it into this skills folder (repeatable), for example <repo>/.claude/skills")
    adopt.set_defaults(func=cmd_adopt)

    rollback = sub.add_parser("rollback", help="return to the previously adopted version")
    rollback.add_argument("--install-to", action="append", metavar="FOLDER", help="as for adopt")
    rollback.set_defaults(func=cmd_rollback)

    propose = sub.add_parser("propose", help="put a skill version up for review as a PR on the target repo")
    propose.add_argument("version", type=int)
    propose.add_argument("--repo-path", required=True, help="a local clone of the repo the agent reads its skill from")
    propose.add_argument("--repo", metavar="OWNER/NAME", help="default: read from the clone's 'origin' remote")
    propose.add_argument("--base", default="main", help="the branch the skill is merged into (default: main)")
    propose.set_defaults(func=cmd_propose)

    patterns = sub.add_parser("patterns", help="which skill patterns a finished conversion used")
    patterns.add_argument("module", help="the folder of converted files")
    patterns.add_argument("--base", required=True, help="the git revision from before the conversion")
    patterns.set_defaults(func=cmd_patterns)

    pr = sub.add_parser("pr", help="open a draft pull request for a pushed branch (GitHub MCP)")
    pr.add_argument("--repo", metavar="OWNER/NAME", help="default: read from the 'origin' remote")
    pr.add_argument("--head", help="the branch with the changes (default: the current branch)")
    pr.add_argument("--base", default="main", help="the branch to merge into (default: main)")
    pr.add_argument("--title", required=True)
    pr.add_argument("--body-file", required=True, help="the PR description (Markdown)")
    pr.set_defaults(func=cmd_pr)

    feedback = sub.add_parser("feedback", help="learn from what reviewers decided about drafts")
    fsub = feedback.add_subparsers(dest="feedback_command", required=True)
    collect = fsub.add_parser("collect", help="read the migration PRs and what happened to them")
    collect.add_argument("--repo", metavar="OWNER/NAME", help="default: read from the 'origin' remote")
    fsub.add_parser("status", help="show outcomes, votes per pattern, and what would change")
    resolve = fsub.add_parser("resolve", help="a person's ruling on a pattern that needs review")
    resolve.add_argument("pattern", help="the pattern id, for example static_to_companion")
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--keep", action="store_true", help="the rule stays; only later outcomes count again")
    choice.add_argument("--drop", action="store_true", help="remove the rule from the playbook")
    resolve.add_argument("--reason", required=True, help="why: the record of why a rule changed is the point")
    feedback.set_defaults(func=cmd_feedback)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        skills_dir = Path(args.skills_dir) if args.skills_dir else ROOT / config["skills_dir"]
        return args.func(args, config, SkillStore(skills_dir))
    except (VersioningError, github_pr.PullRequestError, feedback_loop.FeedbackError,
            proposal.ProposalError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
