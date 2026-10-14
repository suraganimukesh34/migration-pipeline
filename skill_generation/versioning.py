"""Skill versioning and the regeneration trigger (TDD section 8.9).

Layout under the skills root:

  state.json                       what exists, what is adopted, the trigger baseline
  v1/ v2/ ...                      one immutable folder per generated version
    java-to-kotlin-android/SKILL.md
    patterns.json                  the pattern set this version was rendered from
    CHANGES.md                     what changed since the previous version
  current/java-to-kotlin-android/  a copy of the adopted version, for the agent to load

A version is never overwritten. New versions start as "pending"; a person reads
CHANGES.md and adopts one, and adopting an older version is the rollback.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generator  # noqa: E402
from skill_template import SKILL_NAME  # noqa: E402

DEFAULT_CONFIG = {
    "skills_dir": "skills",
    "trigger": {"mode": "either", "new_commits": 10, "schedule_days": 7},
}
TRIGGER_MODES = ("new_commits", "schedule", "either")


class VersioningError(RuntimeError):
    pass


# --- config and trigger ------------------------------------------------------------

def load_config(path: "str | Path | None") -> dict:
    """Defaults, overridden by the JSON file if it exists. Validated before use."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path is not None and Path(path).exists():
        user = json.loads(Path(path).read_text(encoding="utf-8"))
        config["skills_dir"] = user.get("skills_dir", config["skills_dir"])
        config["trigger"].update(user.get("trigger", {}))
    trigger = config["trigger"]
    if trigger["mode"] not in TRIGGER_MODES:
        raise VersioningError(f"trigger.mode must be one of {TRIGGER_MODES}, got {trigger['mode']!r}")
    for key in ("new_commits", "schedule_days"):
        if not isinstance(trigger[key], int) or isinstance(trigger[key], bool) or trigger[key] < 1:
            raise VersioningError(f"trigger.{key} must be a whole number of at least 1")
    return config


class TriggerDecision:
    def __init__(self, should_run: bool, reason: str):
        self.should_run = should_run
        self.reason = reason


def evaluate_trigger(config: dict, state: dict, commits_now: int, now: datetime,
                     feedback_events: int = 0) -> TriggerDecision:
    """Decide whether a regeneration is due: N new migration commits, a schedule, or new review decisions."""
    baseline = state.get("baseline")
    if baseline is None:
        return TriggerDecision(True, "no previous generation")
    trigger = config["trigger"]
    new_commits = commits_now - baseline["commits"]
    due_by_commits = new_commits >= trigger["new_commits"]
    last = datetime.fromisoformat(baseline["at"])
    due_by_schedule = now - last >= timedelta(days=trigger["schedule_days"])
    new_feedback = feedback_events - baseline.get("feedback", 0)
    due_by_feedback = new_feedback > 0  # a reviewer's decision is always a reason to regenerate

    facts = (f"{new_commits} new migration commit(s) since the last run (threshold {trigger['new_commits']}), "
             f"last run {baseline['at'][:10]} (schedule {trigger['schedule_days']} day(s)), "
             f"{max(new_feedback, 0)} new review decision(s)")
    mode = trigger["mode"]
    if mode == "new_commits":
        due = due_by_commits
    elif mode == "schedule":
        due = due_by_schedule
    else:
        due = due_by_commits or due_by_schedule
    return TriggerDecision(due or due_by_feedback, facts)


# --- comparing versions ------------------------------------------------------------

def _evidence(pattern: dict) -> tuple:
    """(accepted, rejected) review votes behind a pattern."""
    votes = pattern.get("feedback") or {}
    return votes.get("accepted", 0), votes.get("rejected", 0)


def diff_patterns(old: "dict | None", new: dict) -> dict:
    old_by_id = {p["id"]: p for p in (old or {}).get("patterns", [])}
    new_by_id = {p["id"]: p for p in new.get("patterns", [])}
    return {
        "evidence": sorted(
            (i, _evidence(old_by_id[i]), _evidence(new_by_id[i]))
            for i in set(old_by_id) & set(new_by_id)
            if _evidence(old_by_id[i]) != _evidence(new_by_id[i])),
        "added": sorted(set(new_by_id) - set(old_by_id)),
        "removed": sorted(set(old_by_id) - set(new_by_id)),
        "confidence": sorted(
            (i, old_by_id[i]["confidence"], new_by_id[i]["confidence"])
            for i in set(old_by_id) & set(new_by_id)
            if old_by_id[i]["confidence"] != new_by_id[i]["confidence"]),
        "counts": sorted(
            (i, old_by_id[i]["sources"]["pairs"], new_by_id[i]["sources"]["pairs"])
            for i in set(old_by_id) & set(new_by_id)
            if old_by_id[i]["sources"]["pairs"] != new_by_id[i]["sources"]["pairs"]),
    }


def is_empty(diff: dict) -> bool:
    return not any(diff.values())


def is_material(diff: dict) -> bool:
    """True if a new version would change what the agent is taught, or rest on new source data.

    A rule added, removed or demoted, a confidence change, or new seen-in counts are material.
    A change in review evidence alone (more "accepted" votes) is not: it is carried into the next
    version that does change the skill, so people are not asked to review a version for it.
    """
    return any(diff[key] for key in ("added", "removed", "confidence", "counts"))


def format_changes(version: int, previous: "int | None", diff: dict, old: "dict | None", new: dict) -> str:
    new_basis = new.get("generated_from", {})
    if previous is None:
        lines = [f"# v{version}: initial version", "",
                 f"Built from {new_basis.get('pairs', 0)} converted files across "
                 f"{new_basis.get('commits', 0)} migration commits.", ""]
        lines += [f"- Added `{i}`" for i in diff["added"]] or ["No patterns."]
        return "\n".join(lines) + "\n"

    old_basis = (old or {}).get("generated_from", {})
    lines = [f"# v{version}: changes since v{previous}", "",
             f"Source: {old_basis.get('pairs', 0)} -> {new_basis.get('pairs', 0)} converted files, "
             f"{old_basis.get('commits', 0)} -> {new_basis.get('commits', 0)} migration commits.", ""]
    if is_empty(diff):
        lines.append("No pattern changes.")
    sections = [
        ("Added", [f"`{i}`" for i in diff["added"]]),
        ("Removed", [f"`{i}`" for i in diff["removed"]]),
        ("Confidence changed", [f"`{i}`: {a} -> {b}" for i, a, b in diff["confidence"]]),
        ("Seen-in counts changed", [f"`{i}`: {a} -> {b} files" for i, a, b in diff["counts"]]),
        ("Review evidence changed (accepted/rejected)",
         [f"`{i}`: {a[0]}/{a[1]} -> {b[0]}/{b[1]}" for i, a, b in diff["evidence"]]),
    ]
    for title, items in sections:
        if items:
            lines += [f"## {title} ({len(items)})", ""] + [f"- {item}" for item in items] + [""]
    return "\n".join(lines).rstrip() + "\n"


# --- the store ---------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


class SkillStore:
    def __init__(self, root: "str | Path"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"

    # state ----------------------------------------------------------------------
    def load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"current": None, "adopted": [], "versions": {}, "baseline": None}

    def _save_state(self, state: dict) -> None:
        _atomic_write(self.state_path, json.dumps(state, indent=2))

    def _version_dir(self, n: int) -> Path:
        return self.root / f"v{n}"

    def _next_version(self, state: dict) -> int:
        # Look at folders as well as state, so a lost or stale state file can never cause overwrites.
        known = [int(v) for v in state["versions"]]
        known += [int(p.name[1:]) for p in self.root.glob("v[0-9]*") if p.name[1:].isdigit()]
        return max(known, default=0) + 1

    def latest_version(self) -> "int | None":
        state = self.load_state()
        known = [int(v) for v in state["versions"]]
        return max(known) if known else None

    def read_patterns(self, version: int) -> dict:
        return json.loads((self._version_dir(version) / "patterns.json").read_text(encoding="utf-8"))

    # creating versions ----------------------------------------------------------
    def create_version(self, patterns_doc: dict, now: datetime,
                       render: Callable = generator.generate_from_doc,
                       feedback_events: int = 0) -> "tuple[int, dict]":
        """Write the next version. Returns (version, diff against the previous one)."""
        state = self.load_state()
        version = self._next_version(state)
        previous = self.latest_version()
        old_doc = self.read_patterns(previous) if previous is not None else None
        diff = diff_patterns(old_doc, patterns_doc)

        # Build in a staging folder and rename into place, so a failure never leaves a partial version.
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        try:
            render(patterns_doc, str(staging), version, now)
            _atomic_write(staging / "patterns.json", json.dumps(patterns_doc, indent=2, ensure_ascii=False))
            _atomic_write(staging / "CHANGES.md", format_changes(version, previous, diff, old_doc, patterns_doc))
            os.rename(staging, self._version_dir(version))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        basis = patterns_doc.get("generated_from", {})
        stamp = now.isoformat()
        state["versions"][str(version)] = {
            "created": stamp, "pairs": basis.get("pairs", 0), "commits": basis.get("commits", 0),
            "patterns": len(patterns_doc.get("patterns", [])), "status": "pending",
        }
        state["baseline"] = {"commits": basis.get("commits", 0), "at": stamp, "feedback": feedback_events}
        self._save_state(state)
        return version, diff

    def mark_proposed(self, version: int, url: str) -> None:
        """Record that a version was put up for review as a pull request. Merging it is the adoption."""
        state = self.load_state()
        if str(version) not in state["versions"]:
            raise VersioningError(f"v{version} does not exist")
        state["versions"][str(version)]["status"] = "proposed"
        state["versions"][str(version)]["proposal"] = url
        self._save_state(state)

    def record_check(self, commits_now: int, now: datetime, feedback_events: int = 0) -> None:
        """A run that passed the trigger but found nothing new still resets the trigger baseline."""
        state = self.load_state()
        state["baseline"] = {"commits": commits_now, "at": now.isoformat(), "feedback": feedback_events}
        self._save_state(state)

    # adopting and rolling back --------------------------------------------------
    def adopt(self, version: int) -> None:
        state = self.load_state()
        if str(version) not in state["versions"]:
            raise VersioningError(f"v{version} does not exist")
        source = self._version_dir(version) / SKILL_NAME
        if not (source / "SKILL.md").exists():
            raise VersioningError(f"v{version} has no {SKILL_NAME}/SKILL.md")

        current_dir = self.root / "current"
        staging = Path(tempfile.mkdtemp(prefix=".adopt-", dir=self.root))
        try:
            shutil.copytree(source, staging / SKILL_NAME)
            if current_dir.exists():
                shutil.rmtree(current_dir)
            os.rename(staging, current_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        previous = state["current"]
        if previous is not None and previous != version:
            state["versions"][str(previous)]["status"] = "superseded"
        state["versions"][str(version)]["status"] = "current"
        state["current"] = version
        state["adopted"].append(version)
        self._save_state(state)

    def rollback(self) -> int:
        """Re-adopt the version that was current before the present one."""
        state = self.load_state()
        history = state["adopted"]
        earlier = [v for v in history[:-1] if v != state["current"]]
        if state["current"] is None or not earlier:
            raise VersioningError("nothing to roll back to: no earlier adopted version")
        target = earlier[-1]
        self.adopt(target)
        return target

    def history(self) -> list[dict]:
        state = self.load_state()
        return [{"version": int(v), **info} for v, info in sorted(state["versions"].items(), key=lambda kv: int(kv[0]))]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
