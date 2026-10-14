"""Renders a pattern set as a GitHub Copilot agent skill (SKILL.md).

Format (docs: learn.microsoft.com/visualstudio/ide/copilot-agent-skills):
  - YAML frontmatter with `name` (lowercase letters, digits, hyphens; matches the
    skill's folder name; at most 64 chars) and `description` (at most 1024 chars),
    plus optional `metadata`.
  - Markdown body with the instructions, kept under about 500 lines.

The rule text comes entirely from the pattern set. Only the headings, the short
"how to apply" notes and the "not covered" section are fixed template text.
"""

import re

SKILL_NAME = "java-to-kotlin-android"

CATEGORY_TITLES = {
    "null_safety": "Null safety",
    "data_classes": "Data classes",
    "constructors": "Constructors",
    "properties": "Properties and getters",
    "operators": "Operators",
    "lambdas": "Lambdas and anonymous classes",
    "interfaces": "Interfaces",
    "collections": "Collections",
    "statics": "Static members",
    "android_views": "Android views",
}

CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
CONFIDENCE_NOTE = {
    "high": "strong pattern",
    "medium": "established pattern",
    "low": "seen only a few times; apply when the construct matches and mention it in the PR",
}

MAX_DESCRIPTION = 1024


def _yaml_string(value: str) -> str:
    """Double-quoted YAML scalar, safe for colons, quotes and non-ASCII text."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def render_frontmatter(name: str, description: str, metadata: dict[str, str]) -> str:
    lines = ["---", f"name: {name}", f"description: {_yaml_string(description)}", "metadata:"]
    lines += [f"  {key}: {_yaml_string(str(value))}" for key, value in metadata.items()]
    lines.append("---")
    return "\n".join(lines)


def build_description(categories: list[str]) -> str:
    titles = [CATEGORY_TITLES.get(c, c.replace("_", " ").capitalize()).lower() for c in categories]
    covered = ", ".join(titles) if titles else "common Java constructs"
    text = (
        "Converts Java Android code to Kotlin the way this codebase has already done it, "
        "using patterns mined from its past Java-to-Kotlin migration commits. "
        f"Covers: {covered}. "
        "Use when asked to migrate, convert or port a Java class, file or module to Kotlin "
        "in an Android MVVM project."
    )
    if len(text) > MAX_DESCRIPTION:
        text = text[: MAX_DESCRIPTION - 1].rstrip() + "…"
    return text


def _code_block(language: str, code: str) -> list[str]:
    """A fenced block indented under its list item. The fence is longer than any backtick run in the code."""
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"  {fence}{language}", *[f"  {line}" if line else "" for line in code.splitlines()], f"  {fence}"]


def _render_rule(pattern: dict, total_pairs: int) -> list[str]:
    seen = pattern["sources"]["pairs"]
    note = CONFIDENCE_NOTE.get(pattern["confidence"], pattern["confidence"])
    reviewed = (pattern.get("feedback") or {}).get("accepted", 0)
    evidence = f" Accepted in {reviewed} reviewed draft(s)." if reviewed else ""
    lines = [f"- **{pattern['rule']}**",
             f"  Seen in {seen} of {total_pairs} converted files ({pattern['confidence']} confidence: {note}).{evidence}",
             ""]
    if pattern.get("examples"):
        example = pattern["examples"][0]
        lines += _code_block("java", example["java"])
        lines += _code_block("kotlin", example["kotlin"])
        lines.append("")
    return lines


def render_skill(patterns_doc: dict, version: int, generated_at: str, name: str = SKILL_NAME) -> str:
    patterns = patterns_doc.get("patterns", [])
    basis = patterns_doc.get("generated_from", {})
    total_pairs = basis.get("pairs", 0)
    total_commits = basis.get("commits", 0)

    rules = [p for p in patterns if p.get("confidence") != "needs_review"]
    unresolved = [p for p in patterns if p.get("confidence") == "needs_review"]

    by_category: dict[str, list[dict]] = {}
    for p in rules:
        by_category.setdefault(p["category"], []).append(p)
    # Categories ordered by their strongest pattern, then by name, so output is stable.
    ordered = sorted(
        by_category,
        key=lambda c: (min(CONFIDENCE_RANK.get(p["confidence"], 9) for p in by_category[c]),
                       -max(p["sources"]["pairs"] for p in by_category[c]), c),
    )

    frontmatter = render_frontmatter(
        name,
        build_description(ordered),
        {
            "version": str(version),
            "generated-at": generated_at,
            "source-pairs": str(total_pairs),
            "source-commits": str(total_commits),
        },
    )

    body = [
        "# Java to Kotlin migration rules (Android, MVVM)",
        "",
        f"Version {version}, generated {generated_at} from {total_pairs} converted files "
        f"across {total_commits} migration commits in this repository's history.",
        "",
        "## How to apply these rules",
        "",
        "- Convert one file at a time and keep its behavior identical.",
        "- Apply a rule when its Java construct appears in the file being converted.",
        "- If a construct is not covered here, use idiomatic Kotlin and say so in the PR description.",
        "",
        "## Rules",
        "",
    ]
    for category in ordered:
        # Each rule ends with its own blank line, so a heading only needs one after it.
        body += [f"### {CATEGORY_TITLES.get(category, category.replace('_', ' ').capitalize())}", ""]
        for p in sorted(by_category[category], key=lambda p: (-p["sources"]["pairs"], p["id"])):
            body += _render_rule(p, total_pairs)
    if not ordered:
        body += ["", "No patterns have been extracted yet."]

    if unresolved:
        body += ["", "## Unresolved: needs human review", "",
                 "These conflict with another source or were rejected in review. "
                 "Do not apply them until a person resolves them.", ""]
        for p in unresolved:
            reason = p.get("feedback_reason")
            body.append(f"- {p['rule']}" + (f" (needs review: {reason})" if reason else ""))

    body += [
        "",
        "## Not covered by this version",
        "",
        "- Library usage conventions and coding standards are not included. "
        "This version is generated from commit history only.",
        "",
    ]
    return frontmatter + "\n\n" + "\n".join(body)
