"""Pattern extraction from app-repo migration pairs (TDD section 8.7, commit source).

Input:  before/after pairs written by the app-repo connector (list of dicts with
        before_text, after_text, before_path, after_path, commit_sha, ...).
Output: a frequency-ranked pattern set (JSON), the intermediate representation
        that the merge step and skill generator consume.

Each detector looks for a Java construct in the "before" text and its Kotlin
counterpart in the "after" text. A pattern is counted once per pair that shows
it, so a transformation seen in 40 pairs ranks above one seen once.

Each pattern keeps a few examples. An example is a real excerpt of the code, not
a fragment: whole lines, expanded to the enclosing construct (the full
constructor, the whole method around a loop, the complete data class header),
so a reader can see the conversion without opening the file.

Detection is regex based, not a parser. It reads constructs, not lines, but it
is a heuristic: expect misses on unusual formatting and occasional false hits.
Treat counts from a small corpus as illustrative.

Usage: py -3 commit_pattern_extractor.py <pairs.json> [--out patterns.json]
"""

import json
import re
import sys
import textwrap
from dataclasses import dataclass, field
from typing import Callable

HIGH_CONFIDENCE_MIN = 10   # pairs showing the pattern
MEDIUM_CONFIDENCE_MIN = 3
MAX_EXAMPLES = 3
MAX_EXCERPT_LINES = 14
HEADER_REACH = 3           # pull in the function header if the match is this close to it

# A detector returns (before_excerpt, after_excerpt) when the pair shows the pattern.
# It may add a third item, a quality score (higher is a clearer example); default 0.
Evidence = tuple
Detector = Callable[[str, str], "Evidence | None"]


# --- excerpts ----------------------------------------------------------------------

def _delta(line: str) -> int:
    """Net bracket depth change of a line: opens minus closes of {}, () and []."""
    return sum(line.count(c) for c in "{([") - sum(line.count(c) for c in "})]")


_FUNCTION_HEADER = re.compile(r"\)\s*(?::\s*[\w<>.?, ]+\s*)?\{\s*$")
_NOT_A_FUNCTION = re.compile(r"\b(?:class|interface|object|enum)\b")


def _enclosing_function(lines: list[str], index: int) -> "int | None":
    """Index of the line opening the function that contains `index`, if there is one."""
    balance = 0
    for i in range(index - 1, -1, -1):
        balance += _delta(lines[i])
        if balance > 0:
            text = lines[i]
            if _FUNCTION_HEADER.search(text) and not _NOT_A_FUNCTION.search(text):
                return i
            return None
    return None


def _ctx(text: str, match: "re.Match", block: bool = True) -> str:
    """Whole-line excerpt around a match.

    block=False returns just the matched lines (for expression-level patterns such
    as a getter call). block=True also pulls in the function header when the match
    is near the top of its function, then runs forward until brackets balance, so
    constructs come out complete.
    """
    lines = text.splitlines()
    start = text.count("\n", 0, match.start())
    end = text.count("\n", 0, max(match.end() - 1, match.start()))
    if block:
        header = _enclosing_function(lines, start)
        if header is not None and start - header <= HEADER_REACH:
            start = header
        balance = sum(_delta(line) for line in lines[start:end + 1])
        while balance > 0 and end + 1 < len(lines) and end - start < MAX_EXCERPT_LINES * 2:
            end += 1
            balance += _delta(lines[end])
    # Blank lines and trailing spaces carry nothing in an example and cost room.
    excerpt = [line.rstrip() for line in lines[start:end + 1] if line.strip()]
    if len(excerpt) > MAX_EXCERPT_LINES:
        indent = re.match(r"\s*", excerpt[0]).group(0)
        excerpt = excerpt[:MAX_EXCERPT_LINES - 1] + [f"{indent}..."]
    return textwrap.dedent("\n".join(excerpt)).strip("\n")


def _find(pattern: str, text: str, flags: int = re.S) -> "re.Match | None":
    return re.search(pattern, text, flags)


# --- detectors ---------------------------------------------------------------------

def data_class(before: str, after: str) -> "Evidence | None":
    fields = _find(r"private\s+(?:final\s+)?[\w<>\[\],.? ]+\s+\w+\s*;", before)
    getter = _find(r"\bget[A-Z]\w*\s*\(\s*\)\s*\{", before)
    kt = _find(r"\bdata\s+class\s+\w+\s*\([^)]*\)", after)
    if fields and getter and kt:
        java = f"{_ctx(before, fields, block=False)}\n// ...\n{_ctx(before, getter)}"
        return java, _ctx(after, kt, block=False)  # the match already spans a multi-line header
    return None


def constructor_to_primary_val(before: str, after: str) -> "Evidence | None":
    assign = _find(r"this\.(\w+)\s*=\s*\1\s*;", before)
    primary = _find(r"\bclass\s+\w+\s*\(\s*(?:private\s+)?va[lr]\s+\w+[^)]*\)", after)
    if assign and primary:
        # Just the class header: a block excerpt would drag in the whole class body.
        return _ctx(before, assign), _ctx(after, primary, block=False)
    return None


def getter_to_property(before: str, after: str) -> "Evidence | None":
    for m in re.finditer(r"\.(get|is)([A-Z]\w*)\s*\(\s*\)", before):
        prefix, name = m.group(1), m.group(2)
        lowered = name[0].lower() + name[1:]
        # getFoo() -> .foo ; boolean isFoo() -> .foo or .isFoo (both are common in converted code)
        props = [lowered] if prefix == "get" else [lowered, f"is{name}"]
        for prop in props:
            if re.search(rf"\.{prop}\b(?!\s*\()", after) and not re.search(rf"\.{prefix}{name}\s*\(", after):
                used = _find(rf"[^\n]*\.{prop}\b(?!\s*\()[^\n]*", after)
                return _ctx(before, m, block=False), _ctx(after, used, block=False)
    return None


def equals_to_operator(before: str, after: str) -> "Evidence | None":
    eq = _find(r"[\w.()]+\.equals\s*\([^)]*\)", before)
    if eq and "==" in after and ".equals(" not in after:
        op = _find(r"[^\n]*[\w.?!()]+\s*==\s*[\w.?!()]+[^\n]*", after)
        if op:
            return _ctx(before, eq, block=False), _ctx(after, op, block=False)
    return None


def null_ternary_to_elvis(before: str, after: str) -> "Evidence | None":
    tern = _find(r"\w+\s*!=\s*null\s*\?[^:;]+:[^;]+", before)
    kt = _find(r"[^\n]*\?:[^\n]*|[^\n]*\w\?\.\w+[^\n]*", after)
    if tern and kt:
        return _ctx(before, tern, block=False), _ctx(after, kt, block=False)
    return None


def nullable_types(before: str, after: str) -> "Evidence | None":
    # Prefer the clearest pairing: a Java `return null;` against a Kotlin nullable return type.
    java_return = _find(r"[^\n]*return\s+null\s*;[^\n]*", before)
    java_null = java_return or _find(r"[^\n]*[!=]=\s*null[^\n]*", before)
    kt_return = _find(r"[^\n]*\)\s*:\s*[\w.<>\[\]]+\?(?=[\s,)={]|$)[^\n]*", after, re.M)
    kt_nullable = kt_return or _find(r"[^\n]*:\s*[\w.<>\[\]]+\?(?=[\s,)={]|$)[^\n]*", after, re.M)
    if java_null and kt_nullable:
        clear_pairing = 1 if (java_return and kt_return) else 0
        return _ctx(before, java_null, block=False), _ctx(after, kt_nullable, block=False), clear_pairing
    return None


_LAMBDA_MARKER = r"\{\s*\w+\s*->|\bset\w*(?:Listener|Callback)\s*\{|\b(?:Runnable|Callable)\s*\{"


def anon_class_to_lambda(before: str, after: str) -> "Evidence | None":
    anon = _find(r"new\s+[\w.]+\s*\(\s*\)\s*\{\s*@Override", before)
    lam = _find(_LAMBDA_MARKER, after)
    if anon and lam and not re.search(r"\bobject\s*:", after):
        return _ctx(before, anon), _ctx(after, lam)
    return None


def anon_class_to_object_expression(before: str, after: str) -> "Evidence | None":
    anon = _find(r"new\s+[\w.]+\s*\(\s*\)\s*\{\s*@Override", before)
    obj = _find(r"\bobject\s*:\s*[\w.<>]+", after)
    if anon and obj:
        return _ctx(before, anon), _ctx(after, obj)
    return None


def interface_to_fun_interface(before: str, after: str) -> "Evidence | None":
    for m in re.finditer(r"\binterface\s+(\w+)\s*\{([^}]*)\}", before, re.S):
        name, body = m.group(1), m.group(2)
        kt = _find(rf"\bfun\s+interface\s+{name}\b", after)
        if body.count(";") == 1 and kt:
            return _ctx(before, m), _ctx(after, kt)
    return None


def loop_to_first_or_null(before: str, after: str) -> "Evidence | None":
    # The if-condition may contain calls with their own parentheses, so match lazily up to ") {".
    loop = _find(r"for\s*\([^)]*:[^)]*\)\s*\{\s*if\s*\(.*?\)\s*\{\s*return\s+\w+\s*;", before)
    kt = _find(r"[^\n]*\b(?:firstOrNull|find|first)\s*\{[^\n]*", after)
    if loop and kt and _find(r"return\s+null\s*;", before):
        return _ctx(before, loop), _ctx(after, kt, block=False)
    return None


def loop_to_collection_operation(before: str, after: str) -> "Evidence | None":
    loop = _find(r"for\s*\([^)]*:[^)]*\)", before)
    op = _find(r"[^\n]*\.(?:map|filter|any|all|count|sumOf|forEach)\s*\{[^\n]*", after)
    if loop and op:
        return _ctx(before, loop), _ctx(after, op, block=False)
    return None


def collection_constructor(before: str, after: str) -> "Evidence | None":
    new = _find(r"[^\n]*new\s+(?:ArrayList|LinkedList|HashMap|LinkedHashMap|HashSet)\s*<[^>]*>\s*\(\s*\)[^\n]*", before)
    kt = _find(r"[^\n]*\b(?:mutableListOf|mutableMapOf|mutableSetOf|arrayListOf|hashMapOf)\s*(?:<[^>]*>)?\s*\(\s*\)[^\n]*", after)
    if new and kt:
        return _ctx(before, new, block=False), _ctx(after, kt, block=False)
    return None


def getter_only_to_private_set(before: str, after: str) -> "Evidence | None":
    getter = _find(r"\bget[A-Z]\w*\s*\(\s*\)\s*\{", before)
    prop = _find(r"[^\n]*va[rl]\s+\w+\s*:[^\n]*\n\s*private\s+set", after)
    if getter and prop:
        return _ctx(before, getter), _ctx(after, prop, block=False)
    return None


def static_to_companion(before: str, after: str) -> "Evidence | None":
    static = _find(r"[^\n]*\bstatic\s+(?!class\b)[\w<>\[\]]+\s+\w+[^\n]*", before)
    kt = _find(r"[^\n]*\bcompanion\s+object\b[^\n]*|^[^\n]*\bobject\s+\w+[^\n]*", after, re.M)
    if static and kt:
        return _ctx(before, static, block=False), _ctx(after, kt)
    return None


def find_view_by_id_to_binding(before: str, after: str) -> "Evidence | None":
    fvb = _find(r"[^\n]*findViewById\s*\([^)]*\)[^\n]*", before)
    if fvb and "findViewById" not in after:
        kt = (_find(r"[^\n]*\w+Binding\.inflate[^\n]*", after)
              or _find(r"[^\n]*\bbinding\.\w+[^\n]*", after))
        if kt:
            return _ctx(before, fvb, block=False), _ctx(after, kt, block=False)
    return None


@dataclass
class PatternSpec:
    id: str
    category: str
    rule: str
    detect: Detector


PATTERNS: list[PatternSpec] = [
    PatternSpec("data_class", "data_classes",
                "A Java class made of private fields and getters becomes a Kotlin data class.",
                data_class),
    PatternSpec("constructor_to_primary_val", "constructors",
                "Constructors that only assign fields (this.x = x) become primary-constructor val/var parameters.",
                constructor_to_primary_val),
    PatternSpec("getter_to_property", "properties",
                "getFoo() calls become property access (.foo).",
                getter_to_property),
    PatternSpec("getter_only_to_private_set", "properties",
                "A private field with a getter and no setter becomes a var with a private set.",
                getter_only_to_private_set),
    PatternSpec("equals_to_operator", "operators",
                "a.equals(b) becomes a == b.",
                equals_to_operator),
    PatternSpec("null_ternary_to_elvis", "null_safety",
                "x != null ? x.foo() : default becomes x?.foo() ?: default.",
                null_ternary_to_elvis),
    PatternSpec("nullable_types", "null_safety",
                "Values that can be null in Java (null returns, null checks) are declared with nullable Kotlin types (T?).",
                nullable_types),
    PatternSpec("anon_class_to_lambda", "lambdas",
                "Anonymous single-method classes passed as callbacks become lambdas.",
                anon_class_to_lambda),
    PatternSpec("anon_class_to_object_expression", "lambdas",
                "Anonymous classes that cannot be a lambda become object expressions (object : Type { ... }).",
                anon_class_to_object_expression),
    PatternSpec("interface_to_fun_interface", "interfaces",
                "Single-method callback interfaces become fun interfaces so callers can pass lambdas.",
                interface_to_fun_interface),
    PatternSpec("loop_to_first_or_null", "collections",
                "A for-loop that returns the first match (or null) becomes firstOrNull { ... }.",
                loop_to_first_or_null),
    PatternSpec("loop_to_collection_operation", "collections",
                "for-each loops that build or test collections become map/filter/any/all/count operations.",
                loop_to_collection_operation),
    PatternSpec("collection_constructor", "collections",
                "new ArrayList<>() / HashMap<>() become mutableListOf() / mutableMapOf().",
                collection_constructor),
    PatternSpec("static_to_companion", "statics",
                "static members move into a companion object (or top-level declarations).",
                static_to_companion),
    PatternSpec("find_view_by_id_to_binding", "android_views",
                "findViewById calls are replaced with view binding.",
                find_view_by_id_to_binding),
]


# --- aggregation -------------------------------------------------------------------

@dataclass
class PatternResult:
    spec: PatternSpec
    pairs: int = 0
    commits: set = field(default_factory=set)
    candidates: list = field(default_factory=list)


def _confidence(pairs: int) -> str:
    if pairs >= HIGH_CONFIDENCE_MIN:
        return "high"
    if pairs >= MEDIUM_CONFIDENCE_MIN:
        return "medium"
    return "low"


def _best_examples(candidates: list[tuple[int, dict]]) -> list[dict]:
    """Clearer examples first (detector quality score), then the most compact: they teach with least noise."""
    def rank(item: tuple[int, dict]) -> tuple:
        quality, example = item
        lines = example["java"].count("\n") + example["kotlin"].count("\n") + 2
        return -quality, lines, len(example["java"]) + len(example["kotlin"]), example["file"]
    return [example for _, example in sorted(candidates, key=rank)[:MAX_EXAMPLES]]


def extract_patterns(pairs: list[dict]) -> dict:
    results = {spec.id: PatternResult(spec) for spec in PATTERNS}
    for pair in pairs:
        before, after = pair["before_text"], pair["after_text"]
        commit = pair.get("conversion_sha") or pair["commit_sha"]
        for spec in PATTERNS:
            evidence = spec.detect(before, after)
            if evidence is None:
                continue
            res = results[spec.id]
            res.pairs += 1
            res.commits.add(commit)
            quality = evidence[2] if len(evidence) > 2 else 0
            res.candidates.append((quality, {
                "file": pair["before_path"].rsplit("/", 1)[-1],
                "commit": commit[:7],
                "java": evidence[0],
                "kotlin": evidence[1],
            }))

    seen = [r for r in results.values() if r.pairs > 0]
    seen.sort(key=lambda r: (-r.pairs, r.spec.id))
    return {
        "generated_from": {
            "pairs": len(pairs),
            "commits": len({p.get("conversion_sha") or p["commit_sha"] for p in pairs}),
        },
        "patterns": [
            {
                "id": r.spec.id,
                "category": r.spec.category,
                "rule": r.spec.rule,
                "confidence": _confidence(r.pairs),
                "sources": {"commits": len(r.commits), "pairs": r.pairs},
                "examples": _best_examples(r.candidates),
            }
            for r in seen
        ],
    }


def main() -> int:
    args = sys.argv[1:]
    if not args or len(args) not in (1, 3) or (len(args) == 3 and args[1] != "--out"):
        print(__doc__)
        return 2
    with open(args[0], encoding="utf-8") as fh:
        pairs = json.load(fh)
    result = extract_patterns(pairs)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if len(args) == 3:
        with open(args[2], "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {len(result['patterns'])} pattern(s) from {len(pairs)} pair(s) to {args[2]}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
