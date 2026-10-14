"""Tests for the commit pattern extractor.

Run from the migration-pipeline folder:  py -3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "connectors"))
sys.path.insert(0, str(ROOT / "extraction"))

import commit_pattern_extractor as cpe  # noqa: E402
from git_app_connector import GitCliSource, collect_pairs, find_migrations  # noqa: E402

FIXTURE = str(ROOT / "fixtures" / "migration-fixture-android")


def pair(before, after, sha="a" * 40, name="Foo.java"):
    return {"before_text": before, "after_text": after, "before_path": f"app/{name}",
            "after_path": f"app/{name[:-5]}.kt", "commit_sha": sha, "conversion_sha": ""}


def ids(result):
    return {p["id"] for p in result["patterns"]}


class DetectorTests(unittest.TestCase):
    def check(self, detector, before, after, expected=True):
        got = detector(before, after)
        self.assertEqual(got is not None, expected, f"{detector.__name__}: {got}")

    def test_data_class(self):
        java = "public class P { private final String a; public String getA() { return a; } }"
        self.check(cpe.data_class, java, "data class P(val a: String)")
        self.check(cpe.data_class, java, "class P(val a: String)", expected=False)

    def test_getter_to_property(self):
        self.check(cpe.getter_to_property, "x = user.getName();", "val x = user.name")
        self.check(cpe.getter_to_property, "x = user.getName();", "val x = user.getName()", expected=False)

    def test_boolean_is_getter_to_property(self):
        self.check(cpe.getter_to_property, "if (p.isInStock()) {}", "if (p.inStock) {}")
        self.check(cpe.getter_to_property, "if (p.isInStock()) {}", "if (p.isInStock) {}")
        self.check(cpe.getter_to_property, "if (p.isInStock()) {}", "if (p.isInStock()) {}", expected=False)

    def test_implicit_it_lambda_on_listener(self):
        java = "b.setOnClickListener(new View.OnClickListener() { @Override public void onClick(View v) {} });"
        self.check(cpe.anon_class_to_lambda, java, "b.setOnClickListener {\n  go()\n}")

    def test_getter_to_property_through_safe_call(self):
        self.check(cpe.getter_to_property, "user.getName()", "user?.name")

    def test_equals_to_operator(self):
        self.check(cpe.equals_to_operator, "if (a.equals(b)) {}", "if (a == b) {}")
        self.check(cpe.equals_to_operator, "if (a.equals(b)) {}", "if (a.equals(b)) {}", expected=False)

    def test_null_ternary_to_elvis(self):
        self.check(cpe.null_ternary_to_elvis, 'n = u != null ? u.getName() : "x";', 'n = u?.name ?: "x"')
        self.check(cpe.null_ternary_to_elvis, "n = a > b ? 1 : 2;", "n = if (a > b) 1 else 2", expected=False)

    def test_nullable_types(self):
        self.check(cpe.nullable_types, "return null;", "fun f(): User? = null")
        self.check(cpe.nullable_types, "return x;", "fun f(): User = x", expected=False)

    def test_anon_class_to_lambda_vs_object_expression(self):
        java = "r.load(id, new Callback() { @Override public void on(User u) {} });"
        self.check(cpe.anon_class_to_lambda, java, "r.load(id) { u -> }")
        self.check(cpe.anon_class_to_lambda, java, "r.load(id, object : Callback { })", expected=False)
        self.check(cpe.anon_class_to_object_expression, java, "r.load(id, object : Callback { })")

    def test_interface_to_fun_interface_needs_single_method(self):
        one = "interface Cb { void on(int x); }"
        two = "interface Cb { void on(int x); void off(); }"
        self.check(cpe.interface_to_fun_interface, one, "fun interface Cb { fun on(x: Int) }")
        self.check(cpe.interface_to_fun_interface, two, "interface Cb { fun on(x: Int) }", expected=False)

    def test_loop_to_first_or_null_handles_nested_parens(self):
        java = "for (U u : us) { if (u.getId().equals(id)) { return u; } } return null;"
        self.check(cpe.loop_to_first_or_null, java, "= us.firstOrNull { it.id == id }")
        self.check(cpe.loop_to_first_or_null, java, "for (u in us) { if (u.id == id) return u }", expected=False)

    def test_loop_to_collection_operation(self):
        self.check(cpe.loop_to_collection_operation, "for (A a : as) { out.add(a); }", "val out = as.map { it }")
        self.check(cpe.loop_to_collection_operation, "int x = 1;", "val x = 1", expected=False)

    def test_collection_constructor(self):
        self.check(cpe.collection_constructor, "new ArrayList<>()", "mutableListOf<User>()")
        self.check(cpe.collection_constructor, "new ArrayList<>()", "ArrayList<User>()", expected=False)

    def test_getter_only_to_private_set(self):
        java = "public String getName() { return name; }"
        self.check(cpe.getter_only_to_private_set, java, "var name: String? = null\n    private set")
        self.check(cpe.getter_only_to_private_set, java, "val name: String = n", expected=False)

    def test_static_to_companion(self):
        self.check(cpe.static_to_companion, "public static int twice(int x)", "companion object { fun twice(x: Int) }")
        self.check(cpe.static_to_companion, "public static class Inner {}", "class Inner", expected=False)

    def test_find_view_by_id_to_binding(self):
        self.check(cpe.find_view_by_id_to_binding, "findViewById(R.id.title)", "binding.title.text = t")
        self.check(cpe.find_view_by_id_to_binding, "findViewById(R.id.title)", "findViewById(R.id.title)", expected=False)

    def test_plain_conversion_with_no_patterns_yields_nothing(self):
        result = cpe.extract_patterns([pair("class A {}", "class A")])
        self.assertEqual(result["patterns"], [])


class ExcerptTests(unittest.TestCase):
    def test_constructor_excerpt_is_the_whole_constructor(self):
        java = ("public class P {\n    private final String a;\n\n"
                "    public P(String a) {\n        this.a = a;\n    }\n\n"
                "    public String getA() { return a; }\n}\n")
        kotlin = "class P(private val a: String) {\n    fun x() = 1\n}\n"
        before, after = cpe.constructor_to_primary_val(java, kotlin)
        self.assertEqual(before, "public P(String a) {\n    this.a = a;\n}")
        # Only the header: a block excerpt would drag in the class body.
        self.assertEqual(after, "class P(private val a: String) {")

    def test_loop_excerpt_includes_method_signature_and_closing_brace(self):
        java = ("class R {\n    public U find(String id) {\n        for (U u : us) {\n"
                "            if (u.getId().equals(id)) {\n                return u;\n            }\n"
                "        }\n        return null;\n    }\n}\n")
        kotlin = "class R {\n    fun find(id: String): U? = us.firstOrNull { it.id == id }\n}\n"
        before, after = cpe.loop_to_first_or_null(java, kotlin)
        self.assertTrue(before.startswith("public U find(String id) {"))
        self.assertTrue(before.rstrip().endswith("}"))
        self.assertIn("return null;", before)
        self.assertEqual(after, "fun find(id: String): U? = us.firstOrNull { it.id == id }")

    def test_expression_level_patterns_stay_on_one_line(self):
        java = "class A {\n    void f() {\n        if (x) {\n            y = user.getName();\n        }\n    }\n}\n"
        kotlin = "class A {\n    fun f() {\n        if (x) {\n            y = user.name\n        }\n    }\n}\n"
        self.assertEqual(cpe.getter_to_property(java, kotlin), ("y = user.getName();", "y = user.name"))

    def test_multiline_data_class_header_is_complete(self):
        java = "class U { private final String id; public String getId() { return id; } }"
        kotlin = "package p\n\ndata class U(\n    val id: String,\n    val name: String,\n)\n"
        _, after = cpe.data_class(java, kotlin)
        self.assertEqual(after, "data class U(\n    val id: String,\n    val name: String,\n)")

    def test_long_excerpts_are_capped_with_an_ellipsis(self):
        body = "".join(f"        step{i}();\n" for i in range(40))
        java = f"class A {{\n    void f() {{\n        new R() {{ @Override public void run() {{\n{body}        }}\n    }}\n}}\n"
        kotlin = "val r = Runnable {\n    go()\n}\n"
        text = "class A { void f() { x = new Runnable() { @Override public void run() {\n" + body + "} }; } }"
        got = cpe.anon_class_to_lambda(text, kotlin)
        self.assertIsNotNone(got)
        lines = got[0].splitlines()
        self.assertLessEqual(len(lines), cpe.MAX_EXCERPT_LINES)
        self.assertEqual(lines[-1].strip(), "...")

    def test_blank_lines_and_trailing_spaces_are_dropped(self):
        java = "class A {\n    A(int x) {   \n\n        this.x = x;\n\n    }\n}\n"
        kotlin = "class A(val x: Int)\n"
        before, _ = cpe.constructor_to_primary_val(java, kotlin)
        self.assertEqual(before, "A(int x) {\n    this.x = x;\n}")


class ExampleSelectionTests(unittest.TestCase):
    def test_nullable_example_prefers_return_null_pairing(self):
        # The first pair has only a null check (shorter); the second has `return null;` and a T? return.
        check_only = pair("void f() {\n    if (x == null) {}\n}\n", "fun f(x: String?) {}\n", sha="1" * 40, name="A.java")
        returning = pair("U g() {\n    return null;\n}\n", "fun g(): U? = null\n", sha="2" * 40, name="B.java")
        result = cpe.extract_patterns([check_only, returning])
        nullable = next(p for p in result["patterns"] if p["id"] == "nullable_types")
        self.assertEqual(nullable["examples"][0]["file"], "B.java")
        self.assertEqual(nullable["examples"][0]["java"], "return null;")

    def test_shorter_example_wins_when_quality_is_equal(self):
        long_pair = pair("r.load(new Callback() { @Override public void on(U u) {\n    a();\n    b();\n    c();\n} });",
                         "r.load { u ->\n    a()\n    b()\n    c()\n}", sha="1" * 40, name="Long.java")
        short_pair = pair("r.load(new Callback() { @Override public void on(U u) {} });",
                          "r.load { u -> }", sha="2" * 40, name="Short.java")
        result = cpe.extract_patterns([long_pair, short_pair])
        lam = next(p for p in result["patterns"] if p["id"] == "anon_class_to_lambda")
        self.assertEqual(lam["examples"][0]["file"], "Short.java")

    def test_quality_score_is_not_leaked_into_the_output(self):
        result = cpe.extract_patterns([pair("U g() { return null; }", "fun g(): U? = null")])
        for p in result["patterns"]:
            for e in p["examples"]:
                self.assertEqual(set(e), {"file", "commit", "java", "kotlin"})


class AggregationTests(unittest.TestCase):
    ANON = "r.load(id, new Callback() { @Override public void on(User u) {} });"
    LAM = "r.load(id) { u -> }"

    def test_frequency_ranking_and_confidence(self):
        pairs = [pair(self.ANON, self.LAM, sha=f"{i:040d}") for i in range(10)]
        pairs.append(pair("n = new ArrayList<>();", "val n = mutableListOf<String>()", sha="f" * 40))
        result = cpe.extract_patterns(pairs)
        self.assertEqual(result["patterns"][0]["id"], "anon_class_to_lambda")
        self.assertEqual(result["patterns"][0]["confidence"], "high")
        self.assertEqual(result["patterns"][0]["sources"], {"commits": 10, "pairs": 10})
        last = result["patterns"][-1]
        self.assertEqual((last["id"], last["confidence"]), ("collection_constructor", "low"))

    def test_medium_confidence_threshold(self):
        pairs = [pair(self.ANON, self.LAM, sha=f"{i:040d}") for i in range(3)]
        self.assertEqual(cpe.extract_patterns(pairs)["patterns"][0]["confidence"], "medium")

    def test_pattern_counted_once_per_pair(self):
        two_in_one = self.ANON + " " + self.ANON
        result = cpe.extract_patterns([pair(two_in_one, self.LAM)])
        self.assertEqual(result["patterns"][0]["sources"]["pairs"], 1)

    def test_examples_are_capped(self):
        pairs = [pair(self.ANON, self.LAM, sha=f"{i:040d}") for i in range(8)]
        self.assertEqual(len(cpe.extract_patterns(pairs)["patterns"][0]["examples"]), cpe.MAX_EXAMPLES)

    def test_two_step_pairs_cite_the_conversion_commit(self):
        p = pair(self.ANON, self.LAM, sha="1" * 40)
        p["conversion_sha"] = "2" * 40
        example = cpe.extract_patterns([p])["patterns"][0]["examples"][0]
        self.assertEqual(example["commit"], "2" * 7)


class FixtureIntegrationTests(unittest.TestCase):
    def test_patterns_found_in_the_fixture_history(self):
        cli = GitCliSource(FIXTURE)
        pairs, skipped = collect_pairs(find_migrations(cli), cli.read_file)
        self.assertEqual(skipped, [])
        self.assertEqual(len(pairs), 17)
        result = cpe.extract_patterns([p.to_dict() for p in pairs])
        counts = {p["id"]: p["sources"]["pairs"] for p in result["patterns"]}
        # Counts follow from how the fixture was built: each class with this.x = x, each class
        # with a Java null and a Kotlin T?, and so on.
        self.assertEqual(counts, {
            "constructor_to_primary_val": 9,
            "getter_to_property": 8,
            "nullable_types": 8,
            "collection_constructor": 4,
            "data_class": 4,
            "loop_to_collection_operation": 4,
            "getter_only_to_private_set": 3,
            "anon_class_to_lambda": 2,
            "equals_to_operator": 2,
            "interface_to_fun_interface": 2,
            "loop_to_first_or_null": 2,
            "null_ternary_to_elvis": 2,
            "static_to_companion": 2,
            "anon_class_to_object_expression": 1,
            "find_view_by_id_to_binding": 1,
        })

    def test_fixture_ranking_is_descending_and_every_detector_fires(self):
        cli = GitCliSource(FIXTURE)
        pairs, _ = collect_pairs(find_migrations(cli), cli.read_file)
        result = cpe.extract_patterns([p.to_dict() for p in pairs])
        found = [p["sources"]["pairs"] for p in result["patterns"]]
        self.assertEqual(found, sorted(found, reverse=True))
        self.assertEqual(ids(result), {spec.id for spec in cpe.PATTERNS})


if __name__ == "__main__":
    unittest.main()
