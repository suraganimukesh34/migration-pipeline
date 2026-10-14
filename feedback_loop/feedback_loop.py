"""Review feedback loop (TDD step 9; the slide's "every decision makes it smarter").

What a reviewer decides about a draft flows back into the playbook:

  merged                    -> an acceptance vote for every skill pattern listed in the PR
  closed without merging    -> a rejection vote for every pattern listed in the PR
  a reviewer's comment      -> a vote on one pattern, counted at once, even on a PR still in review:
                                 skill-feedback: reject <pattern_id> - why
                                 skill-feedback: accept <pattern_id>
  open (still in review)    -> what merging or closing implies is not counted until it is decided

A pattern with more rejections than acceptances moves to `needs_review`. If a reviewer voted on the
rule by name, only those explicit votes decide: they outweigh merges that merely contained the rule.
The skill generator keeps `needs_review` patterns out of the rules and lists them as unresolved, so a
bad pattern stops reinforcing itself until a person decides:

  keep  the rejection was a one-off; the rule stays. Only later outcomes count again.
  drop  the rule is wrong; it is removed from the playbook for good.

Accepted drafts that are merged also land in the repository history as migration commits, which the
history connector reads on the next run. That is the other half of "accepted drafts flow back".

Outcomes and decisions are plain JSON files, so the record of why a rule changed is easy to audit.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_PREFIX = "migrate/"
DECIDED = ("merged", "rejected")

_PATTERN_LINE = re.compile(r"Skill patterns detected in the conversion:\s*(.+)", re.I)
_VERSION_LINE = re.compile(r"java-to-kotlin-android\s+skill\s+v(\d+)", re.I)  # \s: the PR text wraps lines
_VOTE = re.compile(r"skill-feedback:\s*(reject|accept)\s+([A-Za-z0-9_]+)(?:[ \t]*[-:][ \t]*(.+))?", re.I)


class FeedbackError(RuntimeError):
    """A problem the user can act on."""


# --- reading what a PR says ------------------------------------------------------------

def parse_patterns(body: str) -> list:
    """Pattern ids from the PR line 'Skill patterns detected in the conversion: a x3, b x1'."""
    match = _PATTERN_LINE.search(body or "")
    if not match:
        return []
    ids = []
    for item in match.group(1).split(","):
        found = re.match(r"\s*([A-Za-z0-9_]+)", item)
        if found and found.group(1) not in ids:
            ids.append(found.group(1))
    return ids


def parse_skill_version(body: str) -> "int | None":
    match = _VERSION_LINE.search(body or "")
    return int(match.group(1)) if match else None


def parse_votes(text: str, by: str = "") -> list:
    """Votes from review or comment text: 'skill-feedback: reject getter_to_property - breaks Java callers'."""
    votes = []
    for match in _VOTE.finditer(text or ""):
        votes.append({"pattern": match.group(2), "verdict": match.group(1).lower(),
                      "reason": (match.group(3) or "").strip(), "by": by})
    return votes


def _items(payload: Any, *keys: str) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys + ("items",):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _author(item: dict) -> str:
    user = item.get("user") if isinstance(item, dict) else None
    return (user or {}).get("login", "") if isinstance(user, dict) else ""


def _safe(call_tool: Callable, args: dict) -> Any:
    """A review or comment fetch that fails must not lose the PR's outcome; the votes are a bonus."""
    try:
        return call_tool("pull_request_read", args)
    except RuntimeError:
        return []


def _state_of(pr: dict) -> str:
    # GitHub's PR *list* reports merged=False even for a merged PR (seen on the real server);
    # merged_at is what tells the truth, so either one means it was merged.
    if pr.get("merged") or pr.get("merged_at"):
        return "merged"
    if str(pr.get("state", "")).lower() == "closed":
        return "rejected"
    return "pending"


# --- collecting ------------------------------------------------------------------------

def collect_outcomes(call_tool: Callable, owner: str, repo: str, prefix: str = DEFAULT_PREFIX,
                     now: "datetime | None" = None) -> dict:
    """Read every migration PR in the repository and describe what the reviewers decided."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    outcomes = {}
    page = 1
    while True:
        batch = _items(call_tool("list_pull_requests", {
            "owner": owner, "repo": repo, "state": "all", "perPage": 100, "page": page}), "pull_requests")
        for pr in batch:
            head = (pr.get("head") or {}).get("ref", "")
            if not head.startswith(prefix):
                continue  # not one of the agent's PRs
            number = pr["number"]
            base = {"owner": owner, "repo": repo, "pullNumber": number}
            reviews = _items(_safe(call_tool, {**base, "method": "get_reviews"}), "reviews")
            conversation = _items(_safe(call_tool, {**base, "method": "get_comments"}), "comments")
            inline = _items(_safe(call_tool, {**base, "method": "get_review_comments"}), "comments", "review_comments")

            votes = []
            for item in reviews + conversation + inline:
                votes += parse_votes(item.get("body", ""), _author(item))
            states = {str(r.get("state", "")).upper() for r in reviews}
            body = pr.get("body") or ""
            outcomes[f"{owner}/{repo}#{number}"] = {
                "repo": f"{owner}/{repo}", "pr": number, "title": pr.get("title", ""), "head": head,
                "url": pr.get("html_url", ""), "state": _state_of(pr), "draft": bool(pr.get("draft")),
                "patterns": parse_patterns(body), "skill_version": parse_skill_version(body),
                "approved": "APPROVED" in states, "changes_requested": "CHANGES_REQUESTED" in states,
                "votes": votes, "updated_at": pr.get("updated_at") or stamp, "collected_at": stamp,
            }
        if len(batch) < 100:
            return outcomes
        page += 1


# --- storing ---------------------------------------------------------------------------

def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
    tmp.replace(path)


def load_outcomes(directory: "str | Path") -> dict:
    return _read_json(Path(directory) / "outcomes.json", {})


def save_outcomes(directory: "str | Path", outcomes: dict) -> None:
    _write_json(Path(directory) / "outcomes.json", outcomes)


def merge_outcomes(existing: dict, collected: dict) -> dict:
    """New and changed outcomes replace old ones; a PR that is no longer listed keeps its last record."""
    merged = dict(existing)
    merged.update(collected)
    return merged


def load_decisions(directory: "str | Path") -> dict:
    return _read_json(Path(directory) / "decisions.json", {})


def record_decision(directory: "str | Path", pattern_id: str, decision: str, reason: str,
                    now: "datetime | None" = None) -> dict:
    if decision not in ("keep", "drop"):
        raise FeedbackError("a decision is either keep or drop")
    if not reason or not reason.strip():
        raise FeedbackError("give a reason: the record of why a rule changed is the point of the loop")
    decisions = load_decisions(directory)
    decisions[pattern_id] = {"decision": decision, "reason": reason.strip(),
                             "at": (now or datetime.now(timezone.utc)).isoformat()}
    _write_json(Path(directory) / "decisions.json", decisions)
    return decisions[pattern_id]


# --- applying --------------------------------------------------------------------------

def _when(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def tally_votes(outcomes: dict, decisions: dict) -> dict:
    """{pattern id: {accepted, rejected, reasons}} from decided outcomes, ignoring votes a person already ruled on."""
    tally: dict = {}
    for outcome in outcomes.values():
        decided = outcome.get("state") in DECIDED
        explicit = {v["pattern"]: v for v in outcome.get("votes", [])}
        # A reviewer's explicit vote counts as soon as it is made, even on a PR still in review.
        # What merging or closing implies counts only once the PR is decided.
        implicit = set(outcome.get("patterns", [])) if decided else set()
        for pattern in implicit | set(explicit):
            ruling = decisions.get(pattern)
            if ruling and _when(outcome.get("updated_at") or outcome.get("collected_at")) <= _when(ruling["at"]):
                continue  # decided after this outcome: the person's ruling stands
            if pattern in explicit:
                verdict, reason = explicit[pattern]["verdict"], explicit[pattern].get("reason", "")
            else:
                verdict = "accept" if outcome["state"] == "merged" else "reject"
                reason = f"PR #{outcome['pr']} was {'merged' if verdict == 'accept' else 'closed without merging'}"
            entry = tally.setdefault(pattern, {"accepted": 0, "rejected": 0, "explicit_accepted": 0,
                                               "explicit_rejected": 0, "reasons": []})
            counter = "accepted" if verdict == "accept" else "rejected"
            entry[counter] += 1
            if pattern in explicit:
                entry["explicit_" + counter] += 1
            if verdict == "reject" and reason:
                entry["reasons"].append(reason)
    return tally


def apply_feedback(doc: dict, outcomes: dict, decisions: dict) -> dict:
    """The pattern set after review feedback: demotions, drops, and the evidence behind each."""
    result = json.loads(json.dumps(doc))
    tally = tally_votes(outcomes, decisions)
    kept, demoted, dropped = [], [], []
    for pattern in result.get("patterns", []):
        pid = pattern["id"]
        if decisions.get(pid, {}).get("decision") == "drop":
            dropped.append(pid)
            continue
        votes = tally.get(pid)
        if votes:
            pattern["feedback"] = {"accepted": votes["accepted"], "rejected": votes["rejected"]}
            # A reviewer who names a rule outweighs merges that merely contained it: when anyone voted on
            # the rule explicitly, only those votes decide; otherwise what merging or closing implied does.
            if votes["explicit_accepted"] + votes["explicit_rejected"]:
                rejected_wins = votes["explicit_rejected"] > votes["explicit_accepted"]
            else:
                rejected_wins = votes["rejected"] > votes["accepted"]
            if rejected_wins and pattern["confidence"] != "needs_review":
                pattern["previous_confidence"] = pattern["confidence"]
                pattern["confidence"] = "needs_review"
                pattern["feedback_reason"] = (f"rejected in {votes['rejected']} reviewed draft(s), "
                                              f"accepted in {votes['accepted']}")
                if votes["reasons"]:
                    pattern["feedback_reason"] += ": " + "; ".join(dict.fromkeys(votes["reasons"]))
                demoted.append(pid)
        kept.append(pattern)
    result["patterns"] = kept
    decided = [o for o in outcomes.values() if o.get("state") in DECIDED]
    result["feedback"] = {
        "decided": len(decided),
        "pending": sum(1 for o in outcomes.values() if o.get("state") == "pending"),
        "demoted": demoted,
        "dropped": dropped,
    }
    return result


def event_count(outcomes: dict, decisions: dict) -> int:
    """How many review decisions exist: used to tell when there is something new to learn from.

    Counts decided PRs, explicit votes (on any PR, decided or not) and a person's rulings.
    """
    decided = sum(1 for o in outcomes.values() if o.get("state") in DECIDED)
    votes = sum(len(o.get("votes", [])) for o in outcomes.values())
    return decided + votes + len(decisions)


def describe(outcomes: dict, decisions: dict, doc: "dict | None" = None) -> str:
    """A readable summary of outcomes, votes and what would change."""
    lines = []
    for key in sorted(outcomes, key=lambda k: outcomes[k]["pr"]):
        o = outcomes[key]
        extras = []
        if o.get("approved"):
            extras.append("approved")
        if o.get("changes_requested"):
            extras.append("changes requested")
        if o.get("votes"):
            extras.append(f"{len(o['votes'])} rule vote(s)")
        lines.append(f"  #{o['pr']:<4} {o['state']:9} {o['head']}  ({len(o.get('patterns', []))} pattern(s)"
                     + (f"; {', '.join(extras)}" if extras else "") + ")")
    if not lines:
        lines.append("  (no migration PRs recorded yet)")
    tally = tally_votes(outcomes, decisions)
    if tally:
        lines += ["", "Votes per pattern (accepted / rejected):"]
        for pid, v in sorted(tally.items(), key=lambda kv: (-kv[1]["rejected"], kv[0])):
            lines.append(f"  {pid:34} {v['accepted']} / {v['rejected']}")
    if doc is not None:
        after = apply_feedback(doc, outcomes, decisions)["feedback"]
        lines += ["", f"Would move to needs_review: {', '.join(after['demoted']) or 'none'}",
                  f"Dropped by a person's decision: {', '.join(after['dropped']) or 'none'}"]
    if decisions:
        lines += ["", "Decisions by a person:"]
        for pid, d in sorted(decisions.items()):
            lines.append(f"  {pid}: {d['decision']} on {d['at'][:10]} - {d['reason']}")
    return "\n".join(lines)
