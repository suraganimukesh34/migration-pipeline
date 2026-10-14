"""Which skill patterns a finished conversion uses.

The migration agent puts this in its PR, and the feedback loop reads it back from the PR, so a
reviewer's decision on the PR can be traced to the patterns it used. It compares each converted
file with the Java it replaced, using the same detectors that built the playbook.
"""

import subprocess
from pathlib import Path

from commit_pattern_extractor import extract_patterns
from git_app_connector import GitCliSource


def conversion_pairs(module: "str | Path", base: str) -> list:
    """before/after pairs for every .kt in the module whose .java existed at git revision `base`.

    The agent converts files in place (X.java becomes X.kt), so the original is the same path with
    a .java extension.
    """
    top = subprocess.run(["git", "-C", str(module), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        raise SystemExit(f"{module} is not inside a git repository")
    root = Path(top.stdout.strip()).resolve()
    source = GitCliSource(str(root))
    pairs = []
    for kotlin in sorted(Path(module).rglob("*.kt")):
        relative = kotlin.resolve().relative_to(root).as_posix()
        java_path = relative[: -len(".kt")] + ".java"
        before = source.read_file(java_path, base)
        if before is None:
            continue  # a new Kotlin file, not a conversion
        pairs.append({"before_text": before, "after_text": kotlin.read_text(encoding="utf-8", errors="replace"),
                      "before_path": java_path, "commit_sha": base})
    return pairs


def describe(pairs: list) -> str:
    """The two lines the PR carries; the second is the one the feedback loop reads."""
    patterns = extract_patterns(pairs)["patterns"] if pairs else []
    used = ", ".join(f"{p['id']} x{p['sources']['pairs']}" for p in patterns) or "none"
    return (f"Converted files: {len(pairs)}\n"
            f"Skill patterns detected in the conversion: {used}")
