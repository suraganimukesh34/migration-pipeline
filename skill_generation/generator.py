"""Generate a Copilot agent skill from a pattern set (TDD section 8.8).

Writes <out-dir>/<skill-name>/SKILL.md, so the folder name matches the skill's
`name` as the Copilot skill format requires, then validates the result against the
format's limits before reporting success.

Usage: py -3 generator.py <patterns.json> <out-dir> [--version N]
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_template import MAX_DESCRIPTION, SKILL_NAME, render_skill  # noqa: E402

MAX_NAME = 64
MAX_BODY_LINES = 500
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class SkillValidationError(ValueError):
    pass


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal reader for the frontmatter this generator writes (flat keys + one metadata map)."""
    if not text.startswith("---\n"):
        raise SkillValidationError("SKILL.md must start with YAML frontmatter")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise SkillValidationError("frontmatter is not closed with ---")
    fields: dict = {}
    current_map = None
    for line in head.splitlines():
        if line.startswith("  ") and current_map is not None:
            key, _, value = line.strip().partition(": ")
            fields[current_map][key] = json.loads(value)
        else:
            key, _, value = line.partition(":")
            value = value.strip()
            if not value:
                current_map = key
                fields[key] = {}
            else:
                current_map = None
                fields[key] = json.loads(value) if value.startswith('"') else value
    return fields, body


def validate(skill_dir: Path) -> None:
    skill_file = skill_dir / "SKILL.md"
    fields, body = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    name = fields.get("name", "")
    if not NAME_RE.match(name) or len(name) > MAX_NAME:
        raise SkillValidationError(f"invalid skill name {name!r}")
    if name != skill_dir.name:
        raise SkillValidationError(f"name {name!r} must match the folder name {skill_dir.name!r}")
    description = fields.get("description", "")
    if not description:
        raise SkillValidationError("description is required")
    if len(description) > MAX_DESCRIPTION:
        raise SkillValidationError(f"description is {len(description)} chars (max {MAX_DESCRIPTION})")
    if len(body.splitlines()) > MAX_BODY_LINES:
        raise SkillValidationError(f"body is {len(body.splitlines())} lines (max about {MAX_BODY_LINES})")


def generate_from_doc(patterns_doc: dict, out_dir: str, version: int = 1,
                      now: datetime | None = None) -> Path:
    """Render a pattern set to <out_dir>/<skill-name>/SKILL.md and validate it."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    skill_dir = Path(out_dir) / SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        render_skill(patterns_doc, version, stamp), encoding="utf-8", newline="\n")
    validate(skill_dir)
    return skill_dir


def generate(patterns_path: str, out_dir: str, version: int = 1, now: datetime | None = None) -> Path:
    with open(patterns_path, encoding="utf-8") as fh:
        return generate_from_doc(json.load(fh), out_dir, version, now)


def main() -> int:
    args = sys.argv[1:]
    version = 1
    if "--version" in args:
        i = args.index("--version")
        version = int(args[i + 1])
        del args[i:i + 2]
    if len(args) != 2:
        print(__doc__)
        return 2
    skill_dir = generate(args[0], args[1], version)
    print(f"Wrote and validated {skill_dir / 'SKILL.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
