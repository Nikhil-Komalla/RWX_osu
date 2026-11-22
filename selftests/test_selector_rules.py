"""Unit and regression tests for v4 Blast Radius selector.py (no sample/answers dependencies)."""
from __future__ import annotations

import ast
import enum
import json
import os
import pickle
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

RWX_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RWX_DIR))

import build_maps  # noqa: E402
import score  # noqa: E402
import selector  # noqa: E402


def _make_mini_engine():
    tests_json = {
        "tests": [
            {"number": 0, "id": "tests/test_alpha.py::TestAlpha::test_a", "file": "tests/test_alpha.py", "seconds": 0.100},
            {"number": 1, "id": "tests/test_alpha.py::TestAlpha::test_b", "file": "tests/test_alpha.py", "seconds": 1.500},
            {"number": 2, "id": "tests/test_beta.py::TestBeta::test_c", "file": "tests/test_beta.py", "seconds": 4.000},
            {"number": 3, "id": "tests/test_typing.py::TestTyping::test_t", "file": "tests/test_typing.py", "seconds": 0.050},
        ]
    }
    test_ids = [t["id"] for t in tests_json["tests"]]
    maps = {
        "test_ids": test_ids,
        "line_cov": {
            "sqlglot/mod_a.py": {
                "5": [0],
                "6": [0],
                "10": [1],
                "16": [0, 1],
            },
        },
        "file_access": {
            "tests/fixtures/foo.sql": [2],
            "sqlglot/typing/foo.py": [3],
        },
        "class_usage": {
            "sqlglot/mod_a.py::Widget": [1, 2],
        },
        "pkg_walkers": {"sqlglot/typing": [3]},
        "rev_imports": {"sqlglot/mod_a.py": ["tests/test_alpha.py"]},
        "dialect_typing_links": {},
    }
    return selector.SelectorEngine(tests_json, maps, RWX_DIR / "_sqlglot_baseline")


class SelectorUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_disabled = set(selector.DISABLED)
        self._orig_trace = selector.TRACE

    def tearDown(self) -> None:
        selector.DISABLED.clear()
        selector.DISABLED.update(self._orig_disabled)
        selector.TRACE = self._orig_trace

    def test_pack_workers_optimal_k_tradeoff(self) -> None:
        num_to_ms = [5000, 5000, 5000]
        bins = selector.pack_workers({0, 1, 2}, num_to_ms)
        self.assertEqual(len(bins), 3)

        num_to_ms_small = [100, 100, 100]
        bins_small = selector.pack_workers({0, 1, 2}, num_to_ms_small)
        self.assertEqual(len(bins_small), 1)
        self.assertEqual(set(bins_small[0]), {0, 1, 2})

        self.assertEqual(selector.pack_workers(set(), num_to_ms), [])

    def test_ast_diff_comment_vs_docstring_vs_code_vs_syntax(self) -> None:
        eng = _make_mini_engine()
        sample_mod_a = "\n".join([
            "import os",
            "class Widget:",
            "    FLAG = True",
            "    def compute(self, x):",
            "        if x > 0:",
            "            return x + 1",
            "        return 0",
            "",
            "    def doc_fn(self):",
            "        y = 1",
            "        return y",
            "",
            "def helper():",
            "    return 42",
            "def outer():",
            "    return helper()",
        ]) + "\n"

        eng._ast_cache["sqlglot/mod_a.py"] = (sample_mod_a, ast.parse(sample_mod_a))
        eng._ast_cache["tests/test_alpha.py"] = (
            "class TestAlpha:\n    def test_a(self):\n        assert 1 == 1\n    def test_b(self):\n        assert 2 == 2\n",
            ast.parse("class TestAlpha:\n    def test_a(self):\n        assert 1 == 1\n    def test_b(self):\n        assert 2 == 2\n"),
        )

        p_comment = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -5,2 +5,3 @@ class Widget:\n"
            "         if x > 0:\n"
            "+            # harmless comment\n"
            "             return x + 1\n"
        )
        self.assertEqual(eng.select_commit(p_comment), set())

        eng._ast_cache["sqlglot/typing/foo.py"] = ("X = {}\n", ast.parse("X = {}\n"))
        p_typing_comment = (
            "diff --git a/sqlglot/typing/foo.py b/sqlglot/typing/foo.py\n"
            "--- a/sqlglot/typing/foo.py\n"
            "+++ b/sqlglot/typing/foo.py\n"
            "@@ -1,1 +1,2 @@\n"
            "+# comment\n"
            " X = {}\n"
        )
        self.assertEqual(eng.select_commit(p_typing_comment), {3})

        p_docstring = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -9,3 +9,4 @@ class Widget:\n"
            "     def doc_fn(self):\n"
            '+        """# looks like comment"""\n'
            "         y = 1\n"
            "         return y\n"
        )
        self.assertEqual(eng.select_commit(p_docstring), {1})

        p_code = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -5,3 +5,3 @@ class Widget:\n"
            "         if x > 0:\n"
            "-            return x + 1\n"
            "+            return x + 2\n"
            "         return 0\n"
        )
        self.assertEqual(eng.select_commit(p_code), {0})

        p_cls = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -2,3 +2,3 @@ class Widget:\n"
            " class Widget:\n"
            "-    FLAG = True\n"
            "+    FLAG = False\n"
            "     def compute(self, x):\n"
        )
        self.assertEqual(eng.select_commit(p_cls), {1, 2})

        p_syntax = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -5,3 +5,3 @@ class Widget:\n"
            "-        if x > 0:\n"
            "+        if x > :\n"
        )
        self.assertEqual(eng.select_commit(p_syntax), {0, 1, 2})

        p_test = (
            "diff --git a/tests/test_alpha.py b/tests/test_alpha.py\n"
            "--- a/tests/test_alpha.py\n"
            "+++ b/tests/test_alpha.py\n"
            "@@ -2,3 +2,3 @@ class TestAlpha:\n"
            "     def test_a(self):\n"
            "-        assert 1 == 1\n"
            "+        assert 1 == 2\n"
        )
        self.assertEqual(eng.select_commit(p_test), {0})

    def test_v6_import_trigger_rule(self) -> None:
        eng = _make_mini_engine()
        src = (
            "class Widget:\n"
            "    def compute(self, x):\n"
            "        if x > 0:\n"
            "            return x + 1\n"
            "        return 0\n"
            "def helper():\n"
            "    return 42\n"
        )
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.raw_line_cov["sqlglot/mod_a.py"] = {"4": [0], "7": [3]}
        eng.import_trigger_lines = {"sqlglot/mod_a.py": {6: ["sqlglot/mod_b.py"], 7: ["sqlglot/mod_b.py"]}}
        eng.rev_imports["sqlglot/mod_b.py"] = ["tests/test_beta.py"]
        eng._line_cache.clear()
        p = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -6,2 +6,2 @@\n"
            " def helper():\n"
            "-    return 42\n"
            "+    return 43\n"
        )
        # With import_trigger enabled, helper() keeps runtime cov {3} and adds mod_b's importer test {2} (no mod_a nuclear escalation {0, 1}).
        self.assertEqual(eng.select_commit(p), {2, 3})
        # With import_trigger disabled, falls back to once_escalation -> mod_a users + importers {0, 1, 2, 3}.
        selector.DISABLED.add("import_trigger")
        self.assertEqual(eng.select_commit(p), {0, 1, 2, 3})

    def test_v6_dict_zero_trust_rule(self) -> None:
        eng = _make_mini_engine()
        src = "class Widget:\n    KEYWORDS = {'a': 1}\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.tracked_dict_classes = {"sqlglot/mod_a.py::Widget"}
        eng.raw_dict_key_usage = {"KEYWORDS": {"sqlglot/mod_a.py::Widget": {"str:b": [0]}}}
        p = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,2 +1,2 @@\n"
            " class Widget:\n"
            "-    KEYWORDS = {'a': 1}\n"
            "+    KEYWORDS = {'a': 2}\n"
        )
        # Zero readers with no __all__ and not in unsafe/str_const -> trusted 0-test selection (no empty_guard).
        self.assertEqual(eng.select_commit(p), set())

        # Unsafe import-value read -> keeps empty_guard ({1, 2}).
        eng.dict_value_import_reads = frozenset({"KEYWORDS"})
        self.assertEqual(eng.select_commit(p), {1, 2})
        eng.dict_value_import_reads = frozenset()

        # String constant mention -> keeps empty_guard ({1, 2}).
        eng.str_const_idents = frozenset({"KEYWORDS"})
        self.assertEqual(eng.select_commit(p), {1, 2})
        eng.str_const_idents = frozenset()

        # Whole-dict __all__ reader -> selected directly ({3}).
        eng.raw_dict_key_usage["KEYWORDS"]["sqlglot/mod_a.py::Widget"]["__all__"] = [3]
        self.assertEqual(eng.select_commit(p), {3})

    def test_v6_added_module_narrow_rule(self) -> None:
        eng = _make_mini_engine()
        eng._ast_cache["sqlglot/mod_a.py"] = ("import os\n", ast.parse("import os\n"))
        eng.known_words = frozenset({"existing_mod"})
        eng.lazy_test_importers["sqlglot/typing/foo.py"] = ["tests/test_beta.py"]

        def _added_patch(name: str, extra: str = "") -> str:
            return (
                f"diff --git a/sqlglot/typing/{name}.py b/sqlglot/typing/{name}.py\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/sqlglot/typing/{name}.py\n"
                "@@ -0,0 +1 @@\n"
                "+X = 1\n"
            ) + extra

        # Unknown stem -> narrows to pkg_walkers {3} only (drops lazy sibling test 2).
        self.assertEqual(eng.select_commit(_added_patch("brand_new_xyz")), {3})
        # Known stem -> escalates to {2, 3}.
        self.assertEqual(eng.select_commit(_added_patch("existing_mod")), {2, 3})
        # Referenced in another section's added lines -> escalates to {2, 3}.
        extra_ref = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1 +1,2 @@\n"
            "+# import brand_new_xyz\n"
            " import os\n"
        )
        self.assertEqual(eng.select_commit(_added_patch("brand_new_xyz", extra_ref)), {2, 3})

    def test_v6_decorator_removal_narrow_rule(self) -> None:
        eng = _make_mini_engine()
        src_bare = "class Widget:\n    @my_dec\n    def compute(self, x):\n        return x\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_bare, ast.parse(src_bare))
        eng.raw_line_cov["sqlglot/mod_a.py"] = {"2": [0], "3": [0], "4": [0]}
        eng.lazy_test_importers = {"sqlglot/mod_a.py": ["tests/test_beta.py"]}
        eng._line_cache.clear()
        p_bare = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,4 +1,3 @@\n"
            " class Widget:\n"
            "-    @my_dec\n"
            "     def compute(self, x):\n"
            "         return x\n"
        )
        self.assertEqual(eng.select_commit(p_bare), {0})

        # Decorator Call removal does NOT narrow.
        src_call = "class Widget:\n    @my_dec(1)\n    def compute(self, x):\n        return x\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_call, ast.parse(src_call))
        p_call = p_bare.replace("@my_dec", "@my_dec(1)")
        self.assertEqual(eng.select_commit(p_call), {0, 1, 2})

        # Adding a decorator does NOT narrow.
        src_none = "class Widget:\n    def compute(self, x):\n        return x\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_none, ast.parse(src_none))
        p_add = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,3 +1,4 @@\n"
            " class Widget:\n"
            "+    @my_dec\n"
            "     def compute(self, x):\n"
            "         return x\n"
        )
        self.assertEqual(eng.select_commit(p_add), {0, 1, 2})

        # Removing @property does NOT narrow.
        src_prop = "class Widget:\n    @property\n    def compute(self):\n        return 1\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_prop, ast.parse(src_prop))
        p_prop = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,4 +1,3 @@\n"
            " class Widget:\n"
            "-    @property\n"
            "     def compute(self):\n"
            "         return 1\n"
        )
        self.assertEqual(eng.select_commit(p_prop), {0, 1, 2})

    def test_v6_noop_notop_and_type_checking(self) -> None:
        def _cmp_site(src: str) -> tuple[int, int, int, int]:
            for n in ast.walk(ast.parse(src)):
                if isinstance(n, ast.Compare):
                    return (n.lineno, n.col_offset, n.end_lineno or n.lineno, n.end_col_offset or n.col_offset)
            raise AssertionError("no Compare")

        def _check(old_s: str, new_s: str, *, use_safe: bool = False) -> tuple[bool, set[str]]:
            om = ast.parse(old_s)
            nm = ast.parse(new_s)
            tc1, al1 = selector._typing_guard_info(om)
            tc2, al2 = selector._typing_guard_info(nm)
            sites = frozenset({_cmp_site(old_s)}) if use_safe else frozenset()
            ctx = selector._SemEqCtx(
                safe_sites=sites,
                allow_opswap=True,
                allow_notop=True,
                allow_tc=True,
                has_from_tc=tc1 and tc2,
                typing_aliases=frozenset(al1 & al2),
            )
            ok = selector._sem_eq(om, nm, ctx)
            return ok, set(ctx.kinds)

        # notop: is / is not and in / not in (both directions)
        for old_e, new_e in [
            ("not (x is y)", "x is not y"),
            ("x is not y", "not (x is y)"),
            ("not (x in y)", "x not in y"),
            ("x not in y", "not (x in y)"),
        ]:
            ok, kinds = _check(f"def f(x, y):\n    return {old_e}\n", f"def f(x, y):\n    return {new_e}\n")
            self.assertTrue(ok, f"{old_e} <-> {new_e}")
            self.assertEqual(kinds, {"notop"})

        # notop: == / != requires safe site
        ok, kinds = _check("def f(x, y):\n    return not (x == y)\n", "def f(x, y):\n    return x != y\n", use_safe=True)
        self.assertTrue(ok)
        self.assertEqual(kinds, {"notop"})
        ok, _ = _check("def f(x, y):\n    return not (x == y)\n", "def f(x, y):\n    return x != y\n", use_safe=False)
        self.assertFalse(ok)

        # TYPE_CHECKING: valid guards vs non-typing / arbitrary name
        for hdr, tst in [
            ("from typing import TYPE_CHECKING\n", "TYPE_CHECKING"),
            ("import typing as t\n", "t.TYPE_CHECKING"),
            ("", "False"),
        ]:
            ok, kinds = _check(
                f"{hdr}if {tst}:\n    from sqlglot import exp\nX = 1\n",
                f"{hdr}if {tst}:\n    from sqlglot import parser\nX = 1\n",
            )
            self.assertTrue(ok)
            self.assertEqual(kinds, {"type_checking"})

        ok, _ = _check(
            "import other as t\nif t.TYPE_CHECKING:\n    A = 1\n",
            "import other as t\nif t.TYPE_CHECKING:\n    A = 2\n",
        )
        self.assertFalse(ok)
        ok, _ = _check("X = False\nif X:\n    A = 1\n", "X = False\nif X:\n    A = 2\n")
        self.assertFalse(ok)

        p_fix = "diff --git a/tests/fixtures/foo.sql b/tests/fixtures/foo.sql\n--- a/tests/fixtures/foo.sql\n+++ b/tests/fixtures/foo.sql\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertEqual(eng.select_commit(p_fix), {2})

        p_md = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertEqual(eng.select_commit(p_md), set())

    def test_encoding_cookie_and_nested_class(self) -> None:
        eng = _make_mini_engine()
        src = (
            "class Outer:\n"
            "    class Inner:\n"
            "        ESCAPES = {'a': 1}\n"
        )
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.raw_class_usage["sqlglot/mod_a.py::Outer.Inner"] = [2]

        p_nested = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,3 +1,3 @@\n"
            " class Outer:\n"
            "     class Inner:\n"
            "-        ESCAPES = {'a': 1}\n"
            "+        ESCAPES = {'a': 2}\n"
        )
        self.assertEqual(eng.select_commit(p_nested), {2})

        p_enc = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,3 +1,4 @@\n"
            "+# -*- coding: latin-1 -*-\n"
            " class Outer:\n"
            "     class Inner:\n"
            "         ESCAPES = {'a': 1}\n"
        )
        self.assertGreater(len(eng.select_commit(p_enc)), 0)

    def test_once_per_process_lines_escalate(self) -> None:
        eng = _make_mini_engine()
        src = "\n".join([
            "import os",
            "class Widget:",
            "    FLAG = True",
            "    def compute(self, x):",
            "        if x > 0:",
            "            return x + 1",
            "        return 0",
            "",
            "    def __init_subclass__(cls):",
            "        y = 1",
            "        return y",
        ]) + "\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.raw_line_cov["sqlglot/mod_a.py"]["10"] = [0]
        eng._line_cache.clear()
        p = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -9,3 +9,3 @@ class Widget:\n"
            "     def __init_subclass__(cls):\n"
            "-        y = 1\n"
            "+        y = 2\n"
            "         return y\n"
        )
        self.assertEqual(eng.select_commit(p), {0})
        eng.raw_once_lines = {"sqlglot/mod_a.py": [9, 10, 11]}
        eng._once_cache.clear()
        eng.lazy_test_importers = {"sqlglot/mod_a.py": ["tests/test_beta.py"]}
        self.assertEqual(eng.select_commit(p), {0, 1, 2})

        # Also when raw_once_lines marks a line that is runtime-covered inside an ordinary method (e.g. import-time helper).
        p_compute = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -4,4 +4,4 @@ class Widget:\n"
            "     def compute(self, x):\n"
            "         if x > 0:\n"
            "-            return x + 1\n"
            "+            return x + 2\n"
            "         return 0\n"
        )
        eng.raw_once_lines["sqlglot/mod_a.py"] = [6]
        eng._once_cache.clear()
        self.assertEqual(eng.select_commit(p_compute), {0, 1, 2})

    def test_collection_lines_join_line_coverage(self) -> None:
        """Lines executed during test file collection/import are attributed to all tests in that test file."""
        eng = _make_mini_engine()
        sample_mod_a = "\n".join([
            "class Widget:",
            "    def compute(self, x):",
            "        if x > 0:",
            "            return x + 1",
            "        return x + 2",
        ]) + "\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (sample_mod_a, ast.parse(sample_mod_a))
        eng.collection_lines = {"sqlglot/mod_a.py": {5: ["tests/test_beta.py"]}}
        eng._line_cache.clear()
        p = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -3,3 +3,3 @@ class Widget:\n"
            "         if x > 0:\n"
            "             return x + 1\n"
            "-        return x + 2\n"
            "+        return x + 3\n"
        )
        self.assertIn(2, eng.select_commit(p))
        self.assertEqual(eng._lines_cov("sqlglot/mod_a.py", [5]) & (1 << 2), 1 << 2)

    def test_out_of_tree_path_mentions(self) -> None:
        eng = _make_mini_engine()
        eng.path_mentions = {"sqlglotc": ["tests/test_beta.py"]}
        p = (
            "diff --git a/sqlglotc/setup.py b/sqlglotc/setup.py\n"
            "--- a/sqlglotc/setup.py\n"
            "+++ b/sqlglotc/setup.py\n"
            "@@ -1 +1 @@\n"
            "-a = 1\n"
            "+a = 2\n"
        )
        self.assertIn(2, eng.select_commit(p))

    def test_empty_guard_rule(self) -> None:
        eng = _make_mini_engine()
        src = "class Widget:\n    KEYWORDS = {'a': 1}\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.tracked_dict_classes = {"sqlglot/mod_a.py::Widget"}
        eng.raw_dict_key_usage = {"KEYWORDS": {"sqlglot/mod_a.py::Widget": {"str:b": [0]}}}
        p = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,2 +1,2 @@\n"
            " class Widget:\n"
            "-    KEYWORDS = {'a': 1}\n"
            "+    KEYWORDS = {'a': 2}\n"
        )
        selector.DISABLED.discard("empty_guard")
        # Under dict_zero_trust, confirmed 0-reader tracked dict suppresses empty_guard; disabling dict_zero_trust restores it.
        self.assertEqual(eng.select_commit(p), set())
        selector.DISABLED.add("dict_zero_trust")
        # Pending entry is recorded when apply_empty_guard=False; select_commit(apply_empty_guard=True) applies it.
        self.assertEqual(eng.select_commit(p, apply_empty_guard=False), set())
        self.assertIn("sqlglot/mod_a.py", eng._empty_guard_pending)
        self.assertEqual(eng.select_commit(p), {1, 2})
        # When empty_guard is disabled, returns empty.
        selector.DISABLED.add("empty_guard")
        self.assertEqual(eng.select_commit(p), set())

    def test_decorator_import_time_rule(self) -> None:
        eng = _make_mini_engine()
        src = (
            "class Widget:\n"
            "    @my_dec\n"
            "    def compute(self, x):\n"
            "        return x\n"
        )
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.raw_line_cov["sqlglot/mod_a.py"] = {"2": [0], "3": [0], "4": [0]}
        eng._line_cache.clear()
        eng.lazy_test_importers = {"sqlglot/mod_a.py": ["tests/test_beta.py"]}
        p = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,4 +1,3 @@\n"
            " class Widget:\n"
            "-    @my_dec\n"
            "     def compute(self, x):\n"
            "         return x\n"
        )
        selector.DISABLED.add("decorator_removal_narrow")
        self.assertEqual(eng.select_commit(p), {0, 1, 2})
        selector.DISABLED.add("decorator_import_time")
        self.assertEqual(eng.select_commit(p), {0})

    def test_uncertainty_escalation_rule(self) -> None:
        eng = _make_mini_engine()
        # 1) Unapplied hunk on non-test .py escalates (incl. lazy_test_importers)
        eng._ast_cache["sqlglot/mod_a.py"] = ("def f():\n    return 1\n", ast.parse("def f():\n    return 1\n"))
        eng.lazy_test_importers = {"sqlglot/mod_a.py": ["tests/test_typing.py"]}
        p_bad_hunk = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-def nonexistent_line():\n"
            "+def other():\n"
        )
        self.assertIn(3, eng.select_commit(p_bad_hunk))

        # 2) New module added under sqlglot/ picks up pkg_walkers + sibling lazy_test_importers (when not added-narrowed)
        eng.lazy_test_importers["sqlglot/typing/existing.py"] = ["tests/test_beta.py"]
        eng.known_words = frozenset({"zz_new"})
        p_new = (
            "diff --git a/sqlglot/typing/zz_new.py b/sqlglot/typing/zz_new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/sqlglot/typing/zz_new.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+X = 1\n"
        )
        self.assertEqual(eng.select_commit(p_new), {2, 3})

        # 3) Unknown non-inert path outside sqlglot/ and tests/ escalates to all tests
        p_unk = (
            "diff --git a/pyproject.toml b/pyproject.toml\n"
            "--- a/pyproject.toml\n"
            "+++ b/pyproject.toml\n"
            "@@ -1 +1 @@\n"
            "-a = 1\n"
            "+a = 2\n"
        )
        self.assertEqual(eng.select_commit(p_unk), {0, 1, 2, 3})
        selector.DISABLED.add("uncertainty_escalation")
        self.assertEqual(eng.select_commit(p_unk), set())

    def test_trace_stderr_only_and_stdout_strict_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            (data / "commits").mkdir(parents=True)
            tests_json = {
                "tests": [
                    {"number": 0, "id": "tests/test_a.py::TestA::test_x", "file": "tests/test_a.py", "seconds": 0.1},
                ]
            }
            (data / "tests.json").write_text(json.dumps(tests_json))
            (data / "commits" / "c1.patch").write_text(
                "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
            )
            src = root / "src"
            src.mkdir()
            maps = {"version": 3, "test_ids": ["tests/test_a.py::TestA::test_x"], "line_cov": {}}
            mp = root / "maps.pkl.zlib"
            mp.write_bytes(zlib.compress(pickle.dumps(maps, protocol=5), 1))

            import io
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with (
                mock.patch.object(selector, "DATA_DIR", data),
                mock.patch.object(selector, "SQLGLOT_SRC", src),
                mock.patch.object(selector, "MAPS_PATH", mp),
                mock.patch.object(selector, "TRACE", True),
                mock.patch.dict(os.environ, {"BLAST_TRACE": "1", "BLAST_DISABLE": "empty_guard"}),
                mock.patch.object(sys, "stdout", out_buf),
                mock.patch.object(sys, "stderr", err_buf),
            ):
                selector.main()

            out_lines = [l for l in out_buf.getvalue().splitlines() if l]
            self.assertEqual(len(out_lines), 1)
            obj = json.loads(out_lines[0])
            self.assertEqual(set(obj.keys()), {"commit", "workers"})
            self.assertEqual(obj["commit"], "c1")
            self.assertEqual(obj["workers"], [])
            self.assertIn('"reasons"', err_buf.getvalue())
            self.assertIn("inert:README.md", err_buf.getvalue())

    def test_setupclass_shared_fixture_covers_all_class_tests(self) -> None:
        """A shared fixture read in setUpClass (attributed to every test in the class) selects all class tests."""
        tests_json = {
            "tests": [
                {"number": 0, "id": "tests/test_exec.py::TestExec::test_a", "file": "tests/test_exec.py", "seconds": 0.1},
                {"number": 1, "id": "tests/test_exec.py::TestExec::test_b", "file": "tests/test_exec.py", "seconds": 0.2},
                {"number": 2, "id": "tests/test_other.py::TestOther::test_c", "file": "tests/test_other.py", "seconds": 0.1},
            ]
        }
        maps = {
            "test_ids": [t["id"] for t in tests_json["tests"]],
            "line_cov": {},
            "file_access": {"tests/fixtures/shared.sql": [0, 1]},
        }
        eng = selector.SelectorEngine(tests_json, maps, RWX_DIR / "_sqlglot_baseline")
        p = (
            "diff --git a/tests/fixtures/shared.sql b/tests/fixtures/shared.sql\n"
            "--- a/tests/fixtures/shared.sql\n"
            "+++ b/tests/fixtures/shared.sql\n"
            "@@ -1 +1 @@\n"
            "-SELECT 1;\n"
            "+SELECT 2;\n"
        )
        self.assertEqual(eng.select_commit(p), {0, 1})

    def test_package_init_imports_transitive_submodules(self) -> None:
        """Importing pkg.sub executes pkg/__init__.py, so rev_imports of pkg/__init__.py includes submodule callers."""
        import build_maps  # local verification only

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "sqlglot/pkg").mkdir(parents=True)
            (repo / "tests").mkdir(parents=True)
            (repo / "sqlglot/__init__.py").write_text("", encoding="utf-8")
            (repo / "sqlglot/pkg/__init__.py").write_text("VAL = 1\n", encoding="utf-8")
            (repo / "sqlglot/pkg/sub.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
            (repo / "sqlglot/caller.py").write_text("from sqlglot.pkg.sub import fn\n", encoding="utf-8")
            (repo / "tests/test_caller.py").write_text("import sqlglot.caller\n", encoding="utf-8")

            _, rev_imports, _, _, _, _ = build_maps.build_static_graphs(repo)
            self.assertIn("sqlglot/caller.py", rev_imports.get("sqlglot/pkg/__init__.py", []))
            self.assertIn("tests/test_caller.py", rev_imports.get("sqlglot/pkg/__init__.py", []))

    def test_sem_eq_noop_positives_and_negatives(self) -> None:
        def _find_cmp_site(src: str) -> tuple[int, int, int, int]:
            m = ast.parse(src)
            for n in ast.walk(m):
                if isinstance(n, ast.Compare):
                    return (n.lineno, n.col_offset, n.end_lineno or n.lineno, n.end_col_offset or n.col_offset)
            raise AssertionError("no Compare found")

        def _is_sem_eq(old_s: str, new_s: str, *, use_safe_site: bool = False, future: bool = False) -> tuple[bool, set[str]]:
            om = ast.parse(old_s)
            nm = ast.parse(new_s)
            sites = frozenset({_find_cmp_site(old_s)}) if use_safe_site else frozenset()
            ctx = selector._SemEqCtx(
                safe_sites=sites,
                allow_opswap=True,
                allow_alpha=True,
                allow_ann=True,
                future_ann=future or (selector._has_future_annotations(om) and selector._has_future_annotations(nm)),
            )
            ok = selector._sem_eq(om, nm, ctx)
            return ok, set(ctx.kinds)

        # Must be no-op:
        # 1) x is None -> x == None with site in safe set
        ok, kinds = _is_sem_eq("def f(x):\n    return x is None\n", "def f(x):\n    return x == None\n", use_safe_site=True)
        self.assertTrue(ok)
        self.assertEqual(kinds, {"opswap"})

        # 2) Enum is -> == (and is not -> !=) with site in safe set
        ok, kinds = _is_sem_eq(
            "def f(x):\n    return x is Color.RED\n",
            "def f(x):\n    return x == Color.RED\n",
            use_safe_site=True,
        )
        self.assertTrue(ok)
        self.assertEqual(kinds, {"opswap"})

        # 3) Local variable alpha-rename
        ok, kinds = _is_sem_eq(
            "def f(a):\n    tmp = a + 1\n    return tmp * 2\n",
            "def f(a):\n    renamed = a + 1\n    return renamed * 2\n",
        )
        self.assertTrue(ok)
        self.assertEqual(kinds, {"alpha"})

        # 4) Local AnnAssign annotation change inside function body
        ok, kinds = _is_sem_eq(
            "def f():\n    x: int = 1\n    return x\n",
            "def f():\n    x: str = 1\n    return x\n",
        )
        self.assertTrue(ok)
        self.assertEqual(kinds, {"annotation"})

        # 5) Signature annotation change with future annotations and no decorator
        ok, kinds = _is_sem_eq(
            "from __future__ import annotations\ndef f(x: int) -> str:\n    return str(x)\n",
            "from __future__ import annotations\ndef f(x: float | int) -> object:\n    return str(x)\n",
        )
        self.assertTrue(ok)
        self.assertEqual(kinds, {"annotation"})

        # Must NOT be no-op:
        # 6) Op-swap at a site not in safe set
        ok, _ = _is_sem_eq("def f(x):\n    return x is None\n", "def f(x):\n    return x == None\n", use_safe_site=False)
        self.assertFalse(ok)

        # 7) Op-swap with a literal operand (x is 1 vs x == 1)
        ok, _ = _is_sem_eq("def f(x):\n    return x is 1\n", "def f(x):\n    return x == 1\n", use_safe_site=True)
        self.assertFalse(ok)

        # 9) Parameter rename
        ok, _ = _is_sem_eq("def f(x):\n    return x + 1\n", "def f(y):\n    return y + 1\n")
        self.assertFalse(ok)

        # 10) Rename of a local referenced in a nested def / lambda
        ok, _ = _is_sem_eq(
            "def f():\n    x = 1\n    g = lambda: x + 1\n    return g()\n",
            "def f():\n    z = 1\n    g = lambda: z + 1\n    return g()\n",
        )
        self.assertFalse(ok)
        ok, _ = _is_sem_eq(
            "def f():\n    x = 1\n    def inner():\n        return x + 1\n    return inner()\n",
            "def f():\n    z = 1\n    def inner():\n        return z + 1\n    return inner()\n",
        )
        self.assertFalse(ok)

        # 11) Rename onto a global/builtin used in the function
        ok, _ = _is_sem_eq(
            "def f(items):\n    x = len(items)\n    return x + len(items)\n",
            "def f(items):\n    len = len(items)\n    return len + len(items)\n",
        )
        self.assertFalse(ok)

        # 12) Function using locals()
        ok, _ = _is_sem_eq(
            "def f():\n    x = 1\n    return locals()\n",
            "def f():\n    y = 1\n    return locals()\n",
        )
        self.assertFalse(ok)

        # 13) except-as rename
        ok, _ = _is_sem_eq(
            "def f():\n    try:\n        return 1\n    except ValueError as err:\n        return str(err)\n",
            "def f():\n    try:\n        return 1\n    except ValueError as exc:\n        return str(exc)\n",
        )
        self.assertFalse(ok)

        # 14) f-string {x=} debug specifier rename
        ok, _ = _is_sem_eq(
            'def f():\n    x = 1\n    return f"{x=}"\n',
            'def f():\n    y = 1\n    return f"{y=}"\n',
        )
        self.assertFalse(ok)

        # 15) Signature annotation change without future import, or on decorated function
        ok, _ = _is_sem_eq(
            "def f(x: int) -> str:\n    return str(x)\n",
            "def f(x: float) -> str:\n    return str(x)\n",
        )
        self.assertFalse(ok)
        ok, _ = _is_sem_eq(
            "from __future__ import annotations\n@dec\ndef f(x: int) -> str:\n    return str(x)\n",
            "from __future__ import annotations\n@dec\ndef f(x: float) -> str:\n    return str(x)\n",
        )
        self.assertFalse(ok)

        # 16) Class-level AnnAssign change (including class defined inside a function)
        ok, _ = _is_sem_eq(
            "from __future__ import annotations\nclass C:\n    x: int = 1\n",
            "from __future__ import annotations\nclass C:\n    x: str = 1\n",
        )
        self.assertFalse(ok)
        ok, _ = _is_sem_eq(
            "from __future__ import annotations\ndef f():\n    class Inner:\n        x: int = 1\n    return Inner\n",
            "from __future__ import annotations\ndef f():\n    class Inner:\n        x: str = 1\n    return Inner\n",
        )
        self.assertFalse(ok)

        # 17) is -> == mixed with a real change in the same file
        ok, _ = _is_sem_eq(
            "def f(x):\n    return x is None\ndef g():\n    return 1\n",
            "def f(x):\n    return x == None\ndef g():\n    return 2\n",
            use_safe_site=True,
        )
        self.assertFalse(ok)

    def test_semantic_noop_engine_guards_and_trace(self) -> None:
        eng = _make_mini_engine()
        src_a = (
            "class Widget:\n"
            "    def compute(self, x):\n"
            "        if x is None:\n"
            "            return 1\n"
            "        return 0\n"
        )
        eng._ast_cache["sqlglot/mod_a.py"] = (src_a, ast.parse(src_a))
        eng._ast_cache["sqlglot/typing/foo.py"] = ("X = 1\n", ast.parse("X = 1\n"))
        eng.raw_line_cov["sqlglot/mod_a.py"] = {"3": [0], "4": [0]}
        eng._line_cache.clear()
        cmp_node = next(n for n in ast.walk(ast.parse(src_a)) if isinstance(n, ast.Compare))
        eng.eq_safe_sites = {
            "sqlglot/mod_a.py": frozenset({
                (cmp_node.lineno, cmp_node.col_offset, cmp_node.end_lineno, cmp_node.end_col_offset)
            })
        }

        p_one = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -2,3 +2,3 @@ class Widget:\n"
            "     def compute(self, x):\n"
            "-        if x is None:\n"
            "+        if x == None:\n"
            "             return 1\n"
        )
        # Single-section safe opswap -> empty selection + trace note.
        selector.TRACE = True
        self.assertEqual(eng.select_commit(p_one), set())
        self.assertTrue(any("semantic_noop:sqlglot/mod_a.py:opswap" == r for r in eng.reasons))

        # Disabled -> falls back to line coverage {0}.
        selector.DISABLED.add("noop_opswap")
        self.assertEqual(eng.select_commit(p_one), {0})
        selector.DISABLED.clear()

        # 8) Op-swap in a 2-section patch -> must NOT apply opswap!
        p_two = p_one + (
            "diff --git a/sqlglot/typing/foo.py b/sqlglot/typing/foo.py\n"
            "--- a/sqlglot/typing/foo.py\n"
            "+++ b/sqlglot/typing/foo.py\n"
            "@@ -1 +1 @@\n"
            "-X = 1\n"
            "+X = 2\n"
        )
        self.assertIn(0, eng.select_commit(p_two))

        # Child-record failure fallback adds subprocess_test_ids when opswap fires.
        eng.eq_sites_stats = {"child_records": []}
        eng.subprocess_test_ids = ["tests/test_beta.py::TestBeta::test_c"]
        eng._subprocess_livemask = None
        self.assertEqual(eng.select_commit(p_one), {2})

        # Never applies to test files even if opswap/alpha exists inside a test method.
        test_src = "class TestAlpha:\n    def test_a(self):\n        v = 1\n        assert v == 1\n"
        eng._ast_cache["tests/test_alpha.py"] = (test_src, ast.parse(test_src))
        p_test = (
            "diff --git a/tests/test_alpha.py b/tests/test_alpha.py\n"
            "--- a/tests/test_alpha.py\n"
            "+++ b/tests/test_alpha.py\n"
            "@@ -2,3 +2,3 @@ class TestAlpha:\n"
            "     def test_a(self):\n"
            "-        v = 1\n"
            "-        assert v == 1\n"
            "+        w = 1\n"
            "+        assert w == 1\n"
        )
        self.assertEqual(eng.select_commit(p_test), {0})

    def test_v6_import_trigger_rule(self) -> None:
        eng = _make_mini_engine()
        src = (
            "class Widget:\n"
            "    def compute(self, x):\n"
            "        if x > 0:\n"
            "            return x + 1\n"
            "        return 0\n"
            "def helper():\n"
            "    return 42\n"
        )
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.raw_line_cov["sqlglot/mod_a.py"] = {"4": [0], "7": [3]}
        eng.import_trigger_lines = {"sqlglot/mod_a.py": {6: ["sqlglot/mod_b.py"], 7: ["sqlglot/mod_b.py"]}}
        eng.rev_imports["sqlglot/mod_b.py"] = ["tests/test_beta.py"]
        eng._line_cache.clear()
        p = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -6,2 +6,2 @@\n"
            " def helper():\n"
            "-    return 42\n"
            "+    return 43\n"
        )
        # With import_trigger enabled, helper() keeps runtime cov {3} and adds mod_b's importer test {2} (no mod_a nuclear escalation {0, 1}).
        self.assertEqual(eng.select_commit(p), {2, 3})
        # With import_trigger disabled, falls back to once_escalation -> mod_a users + importers {0, 1, 2, 3}.
        selector.DISABLED.add("import_trigger")
        self.assertEqual(eng.select_commit(p), {0, 1, 2, 3})

    def test_v6_dict_zero_trust_rule(self) -> None:
        eng = _make_mini_engine()
        src = "class Widget:\n    KEYWORDS = {'a': 1}\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.tracked_dict_classes = {"sqlglot/mod_a.py::Widget"}
        eng.raw_dict_key_usage = {"KEYWORDS": {"sqlglot/mod_a.py::Widget": {"str:b": [0]}}}
        p = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,2 +1,2 @@\n"
            " class Widget:\n"
            "-    KEYWORDS = {'a': 1}\n"
            "+    KEYWORDS = {'a': 2}\n"
        )
        # Zero readers with no __all__ and not in unsafe/str_const -> trusted 0-test selection (no empty_guard).
        self.assertEqual(eng.select_commit(p), set())

        # Unsafe import-value read -> keeps empty_guard ({1, 2}).
        eng.dict_value_import_reads = frozenset({"KEYWORDS"})
        self.assertEqual(eng.select_commit(p), {1, 2})
        eng.dict_value_import_reads = frozenset()

        # String constant mention -> keeps empty_guard ({1, 2}).
        eng.str_const_idents = frozenset({"KEYWORDS"})
        self.assertEqual(eng.select_commit(p), {1, 2})
        eng.str_const_idents = frozenset()

        # Whole-dict __all__ reader -> selected directly ({3}).
        eng.raw_dict_key_usage["KEYWORDS"]["sqlglot/mod_a.py::Widget"]["__all__"] = [3]
        self.assertEqual(eng.select_commit(p), {3})

    def test_v6_added_module_narrow_rule(self) -> None:
        eng = _make_mini_engine()
        eng._ast_cache["sqlglot/mod_a.py"] = ("import os\n", ast.parse("import os\n"))
        eng.known_words = frozenset({"existing_mod"})
        eng.lazy_test_importers["sqlglot/typing/foo.py"] = ["tests/test_beta.py"]

        def _added_patch(name: str, extra: str = "") -> str:
            return (
                f"diff --git a/sqlglot/typing/{name}.py b/sqlglot/typing/{name}.py\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/sqlglot/typing/{name}.py\n"
                "@@ -0,0 +1 @@\n"
                "+X = 1\n"
            ) + extra

        # Unknown stem -> narrows to pkg_walkers {3} only (drops lazy sibling test 2).
        self.assertEqual(eng.select_commit(_added_patch("brand_new_xyz")), {3})
        # Known stem -> escalates to {2, 3}.
        self.assertEqual(eng.select_commit(_added_patch("existing_mod")), {2, 3})
        # Referenced in another section's added lines -> escalates to {2, 3}.
        extra_ref = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1 +1,2 @@\n"
            "+# import brand_new_xyz\n"
            " import os\n"
        )
        self.assertEqual(eng.select_commit(_added_patch("brand_new_xyz", extra_ref)), {2, 3})

    def test_v6_decorator_removal_narrow_rule(self) -> None:
        eng = _make_mini_engine()
        src_bare = "class Widget:\n    @my_dec\n    def compute(self, x):\n        return x\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_bare, ast.parse(src_bare))
        eng.raw_line_cov["sqlglot/mod_a.py"] = {"2": [0], "3": [0], "4": [0]}
        eng.lazy_test_importers = {"sqlglot/mod_a.py": ["tests/test_beta.py"]}
        eng._line_cache.clear()
        p_bare = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,4 +1,3 @@\n"
            " class Widget:\n"
            "-    @my_dec\n"
            "     def compute(self, x):\n"
            "         return x\n"
        )
        self.assertEqual(eng.select_commit(p_bare), {0})

        # Decorator Call removal does NOT narrow.
        src_call = "class Widget:\n    @my_dec(1)\n    def compute(self, x):\n        return x\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_call, ast.parse(src_call))
        p_call = p_bare.replace("@my_dec", "@my_dec(1)")
        self.assertEqual(eng.select_commit(p_call), {0, 1, 2})

        # Adding a decorator does NOT narrow.
        src_none = "class Widget:\n    def compute(self, x):\n        return x\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_none, ast.parse(src_none))
        p_add = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,3 +1,4 @@\n"
            " class Widget:\n"
            "+    @my_dec\n"
            "     def compute(self, x):\n"
            "         return x\n"
        )
        self.assertEqual(eng.select_commit(p_add), {0, 1, 2})

        # Removing @property does NOT narrow.
        src_prop = "class Widget:\n    @property\n    def compute(self):\n        return 1\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_prop, ast.parse(src_prop))
        p_prop = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,4 +1,3 @@\n"
            " class Widget:\n"
            "-    @property\n"
            "     def compute(self):\n"
            "         return 1\n"
        )
        self.assertEqual(eng.select_commit(p_prop), {0, 1, 2})

    def test_v6_noop_notop_and_type_checking(self) -> None:
        def _cmp_site(src: str) -> tuple[int, int, int, int]:
            for n in ast.walk(ast.parse(src)):
                if isinstance(n, ast.Compare):
                    return (n.lineno, n.col_offset, n.end_lineno or n.lineno, n.end_col_offset or n.col_offset)
            raise AssertionError("no Compare")

        def _check(old_s: str, new_s: str, *, use_safe: bool = False) -> tuple[bool, set[str]]:
            om = ast.parse(old_s)
            nm = ast.parse(new_s)
            tc1, al1 = selector._typing_guard_info(om)
            tc2, al2 = selector._typing_guard_info(nm)
            sites = frozenset({_cmp_site(old_s)}) if use_safe else frozenset()
            ctx = selector._SemEqCtx(
                safe_sites=sites,
                allow_opswap=True,
                allow_notop=True,
                allow_tc=True,
                has_from_tc=tc1 and tc2,
                typing_aliases=frozenset(al1 & al2),
            )
            ok = selector._sem_eq(om, nm, ctx)
            return ok, set(ctx.kinds)

        # notop: is / is not and in / not in (both directions)
        for old_e, new_e in [
            ("not (x is y)", "x is not y"),
            ("x is not y", "not (x is y)"),
            ("not (x in y)", "x not in y"),
            ("x not in y", "not (x in y)"),
        ]:
            ok, kinds = _check(f"def f(x, y):\n    return {old_e}\n", f"def f(x, y):\n    return {new_e}\n")
            self.assertTrue(ok, f"{old_e} <-> {new_e}")
            self.assertEqual(kinds, {"notop"})

        # notop: == / != requires safe site
        ok, kinds = _check("def f(x, y):\n    return not (x == y)\n", "def f(x, y):\n    return x != y\n", use_safe=True)
        self.assertTrue(ok)
        self.assertEqual(kinds, {"notop"})
        ok, _ = _check("def f(x, y):\n    return not (x == y)\n", "def f(x, y):\n    return x != y\n", use_safe=False)
        self.assertFalse(ok)

        # TYPE_CHECKING: valid guards vs non-typing / arbitrary name
        for hdr, tst in [
            ("from typing import TYPE_CHECKING\n", "TYPE_CHECKING"),
            ("import typing as t\n", "t.TYPE_CHECKING"),
            ("", "False"),
        ]:
            ok, kinds = _check(
                f"{hdr}if {tst}:\n    from sqlglot import exp\nX = 1\n",
                f"{hdr}if {tst}:\n    from sqlglot import parser\nX = 1\n",
            )
            self.assertTrue(ok)
            self.assertEqual(kinds, {"type_checking"})

        ok, _ = _check(
            "import other as t\nif t.TYPE_CHECKING:\n    A = 1\n",
            "import other as t\nif t.TYPE_CHECKING:\n    A = 2\n",
        )
        self.assertFalse(ok)
        ok, _ = _check("X = False\nif X:\n    A = 1\n", "X = False\nif X:\n    A = 2\n")
        self.assertFalse(ok)

    def test_v6b_enum_dict_key_normalization(self) -> None:
        class MyIntEnum(enum.IntEnum):
            AUTO_INCREMENT = 7

        class MyStrEnum(str, enum.Enum):
            INT = "int_val"

        class MyFlag(enum.IntFlag):
            A = 1
            B = 2

        self.assertEqual(build_maps._norm_dict_key(MyIntEnum.AUTO_INCREMENT), "enum:MyIntEnum.AUTO_INCREMENT")
        self.assertEqual(build_maps._norm_dict_key(MyStrEnum.INT), "enum:MyStrEnum.INT")
        self.assertEqual(
            build_maps._norm_dict_key((MyIntEnum.AUTO_INCREMENT, "x")),
            "tuple:(enum:MyIntEnum.AUTO_INCREMENT,str:x)",
        )
        self.assertEqual(build_maps._norm_dict_key(42), "int:42")
        self.assertEqual(build_maps._norm_dict_key("abc"), "str:abc")
        self.assertIsNone(build_maps._norm_dict_key(MyFlag.A | MyFlag.B))
        self.assertIsNone(build_maps._norm_dict_key(MyFlag(4)))

    def test_v6b_defensive_enum_lookup_fallback(self) -> None:
        eng = _make_mini_engine()
        src = (
            "class Widget:\n"
            "    TOKEN_MAPPING = {\n"
            "        TokenType.AUTO_INCREMENT: 'GENERATED BY DEFAULT AS IDENTITY',\n"
            "        TokenType.VAR: 'VARCHAR',\n"
            "    }\n"
        )
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.tracked_dict_classes.add("sqlglot/mod_a.py::Widget")
        eng.raw_dict_key_usage["TOKEN_MAPPING"] = {
            "sqlglot/mod_a.py::Widget": {
                "int:TokenType.AUTO_INCREMENT": [2],
                "enum:TokenType.VAR": [1],
            }
        }
        patch = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,5 +1,5 @@\n"
            " class Widget:\n"
            "     TOKEN_MAPPING = {\n"
            "-        TokenType.AUTO_INCREMENT: 'GENERATED BY DEFAULT AS IDENTITY',\n"
            "+        TokenType.AUTO_INCREMENT: 'AUTOINCREMENT',\n"
            "         TokenType.VAR: 'VARCHAR',\n"
            "     }\n"
        )
        self.assertEqual(eng.select_commit(patch), {2})

    def test_v6b_dict_key_mismatch_attrs_blocks_zero_trust(self) -> None:
        eng = _make_mini_engine()
        src = (
            "class Widget:\n"
            "    WEIRD_MAP = {\n"
            "        'dead': 1,\n"
            "        'live': 2,\n"
            "    }\n"
        )
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.tracked_dict_classes.add("sqlglot/mod_a.py::Widget")
        eng.raw_dict_key_usage["WEIRD_MAP"] = {
            "sqlglot/mod_a.py::Widget": {
                "str:live": [1],
            }
        }
        patch = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,5 +1,5 @@\n"
            " class Widget:\n"
            "     WEIRD_MAP = {\n"
            "-        'dead': 1,\n"
            "+        'dead': 9,\n"
            "         'live': 2,\n"
            "     }\n"
        )
        # Without WEIRD_MAP in dict_key_mismatch_attrs, dict_zero_trust skips empty_guard -> set().
        self.assertEqual(eng.select_commit(patch), set())
        # With WEIRD_MAP flagged in dict_key_mismatch_attrs, empty_guard must fire.
        eng.dict_key_mismatch_attrs = frozenset({"WEIRD_MAP"})
        selected = eng.select_commit(patch)
        self.assertEqual(selected, {1, 2})

    def test_v7_module_new_fn_rule(self) -> None:
        eng = _make_mini_engine()
        src = "import sys\n\ndef existing(x):\n    return x\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))

        def _patch_append(fn_text: str) -> str:
            return (
                "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
                "--- a/sqlglot/mod_a.py\n"
                "+++ b/sqlglot/mod_a.py\n"
                "@@ -1,4 +1,7 @@\n"
                " import sys\n"
                " \n"
                " def existing(x):\n"
                "     return x\n"
                + fn_text
            )

        # Fresh private top-level helper -> 0 tests
        p_ok = _patch_append("+\n+def _zz_helper(x):\n+    return x\n")
        self.assertEqual(eng.select_commit(p_ok), set())
        selector.DISABLED.add("module_new_fn")
        self.assertTrue(len(eng.select_commit(p_ok)) > 0)
        selector.DISABLED.discard("module_new_fn")

        # Negatives: existing name, builtin, decorated, call default
        for bad_fn in (
            "+\n+def existing(x):\n+    return 2\n",
            "+\n+def len(x):\n+    return 0\n",
            "+@my_dec\n+def _zz_helper(x):\n+    return x\n",
            "+\n+def _zz_helper(x=foo()):\n+    return x\n",
        ):
            self.assertTrue(len(eng.select_commit(_patch_append(bad_fn))) > 0, bad_fn)

    def test_v7_noop_stdlib_import_rule(self) -> None:
        eng = _make_mini_engine()
        src = "import re\nimport sys\nfrom sqlglot import mod_b\n\nX = 1\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))

        p_add = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,3 +1,4 @@\n"
            "+import heapq\n"
            " import re\n"
            " import sys\n"
            " from sqlglot import mod_b\n"
        )
        self.assertEqual(eng.select_commit(p_add), set())
        selector.DISABLED.add("noop_stdlib_import")
        self.assertTrue(len(eng.select_commit(p_add)) > 0)
        selector.DISABLED.discard("noop_stdlib_import")

        p_local_add = p_add.replace("+import heapq", "+from sqlglot.mod_b import X")
        self.assertTrue(len(eng.select_commit(p_local_add)) > 0)

        src_pure = "import re\nimport sys\n\nX = 1\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_pure, ast.parse(src_pure))
        p_swap_std = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-import re\n"
            " import sys\n"
            "+import re\n"
            " \n"
        )
        self.assertEqual(eng.select_commit(p_swap_std), set())

        src_sg = "from sqlglot import a\nfrom sqlglot import b\n\nX = 1\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_sg, ast.parse(src_sg))
        p_swap_sg = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-from sqlglot import a\n"
            " from sqlglot import b\n"
            "+from sqlglot import a\n"
            " \n"
        )
        self.assertTrue(len(eng.select_commit(p_swap_sg)) > 0)

    def test_v7_module_const_narrow_rule(self) -> None:
        eng = _make_mini_engine()
        lines = [
            "LIMIT = 5",
            "",
            "",
            "",
            "def f0():",
            "    return 1",
            "",
            "",
            "def f_limit():",
            "    return LIMIT",  # line 10 -> covered only by test 1
        ]
        src = "\n".join(lines) + "\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        p_const = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-LIMIT = 5\n"
            "+LIMIT = 6\n"
            " \n"
        )
        self.assertEqual(eng.select_commit(p_const), {1})
        selector.DISABLED.add("module_const_narrow")
        self.assertEqual(eng.select_commit(p_const), {0, 1, 2})
        selector.DISABLED.discard("module_const_narrow")

        # Read at module level -> fallback ({0, 1, 2})
        src_mod = "\n".join(["LIMIT = 5", "OTHER = LIMIT + 1"] + lines[2:]) + "\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_mod, ast.parse(src_mod))
        self.assertEqual(eng.select_commit(p_const), {0, 1, 2})

        # Quoted string mention -> fallback ({0, 1, 2})
        src_q = "\n".join(["LIMIT = 5", "NAME = 'LIMIT'"] + lines[2:]) + "\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src_q, ast.parse(src_q))
        self.assertEqual(eng.select_commit(p_const), {0, 1, 2})

    def test_v7_class_new_method_rule(self) -> None:
        eng = _make_mini_engine()
        src = "class Widget:\n    def existing(self):\n        return 1\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        eng.known_words = frozenset({"helper_m"})

        def _patch_method(m_name: str) -> str:
            return (
                "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
                "--- a/sqlglot/mod_a.py\n"
                "+++ b/sqlglot/mod_a.py\n"
                "@@ -1,3 +1,6 @@\n"
                " class Widget:\n"
                "     def existing(self):\n"
                "         return 1\n"
                "+\n"
                f"+    def {m_name}(self):\n"
                "+        return 2\n"
            )

        p_ok = _patch_method("_zz_m")
        self.assertEqual(eng.select_commit(p_ok), set())
        selector.DISABLED.add("class_new_method")
        self.assertTrue(len(eng.select_commit(p_ok)) > 0)
        selector.DISABLED.discard("class_new_method")

        self.assertTrue(len(eng.select_commit(_patch_method("foo_sql"))) > 0)
        self.assertTrue(len(eng.select_commit(_patch_method("helper_m"))) > 0)

    def test_v7_notop_and_fstring_noop(self) -> None:
        for old_e, new_e in [("not x in y", "x not in y"), ("not x is y", "x is not y")]:
            ctx = selector._SemEqCtx()
            ok = selector._sem_eq(
                ast.parse(f"def f(x, y):\n    return {old_e}\n"),
                ast.parse(f"def f(x, y):\n    return {new_e}\n"),
                ctx,
            )
            self.assertTrue(ok)
            self.assertEqual(ctx.kinds, {"notop"})

        ctx1 = selector._SemEqCtx()
        self.assertTrue(
            selector._sem_eq(
                ast.parse('def f():\n    return f"abc"\n'),
                ast.parse('def f():\n    return "abc"\n'),
                ctx1,
            )
        )
        self.assertEqual(ctx1.kinds, {"fstring"})

        ctx2 = selector._SemEqCtx()
        self.assertFalse(
            selector._sem_eq(
                ast.parse('def f():\n    f"""d"""\n    return 1\n'),
                ast.parse('def f():\n    """d"""\n    return 1\n'),
                ctx2,
            )
        )
        ctx3 = selector._SemEqCtx()
        self.assertFalse(
            selector._sem_eq(
                ast.parse('def f(x):\n    return f"{x}"\n'),
                ast.parse('def f(x):\n    return "x"\n'),
                ctx3,
            )
        )

        # End-to-end toggle check on noop_fstring
        eng = _make_mini_engine()
        lines = [""] * 8 + ["def f():", '    return "abc"']  # line 10 covered by test {1}
        src = "\n".join(lines) + "\n"
        eng._ast_cache["sqlglot/mod_a.py"] = (src, ast.parse(src))
        p_fstr = (
            "diff --git a/sqlglot/mod_a.py b/sqlglot/mod_a.py\n"
            "--- a/sqlglot/mod_a.py\n"
            "+++ b/sqlglot/mod_a.py\n"
            "@@ -9,2 +9,2 @@\n"
            " def f():\n"
            '-    return "abc"\n'
            '+    return f"abc"\n'
        )
        self.assertEqual(eng.select_commit(p_fstr), set())
        selector.DISABLED.add("noop_fstring")
        self.assertEqual(eng.select_commit(p_fstr), {1})
        selector.DISABLED.discard("noop_fstring")

    @staticmethod
    def _make_real_engine():
        tests_json = json.loads((RWX_DIR / "sample/tests.json").read_text("utf-8"))
        maps = selector.load_maps(RWX_DIR / "maps/blast_maps.pkl.zlib")
        return selector.SelectorEngine(tests_json, maps, RWX_DIR / "_sqlglot_baseline")

    def test_v8_patch_overlay_finder_isolation(self) -> None:
        base = RWX_DIR / "_sqlglot_baseline"
        opt_init = (base / "sqlglot/optimizer/__init__.py").read_text("utf-8")
        finder = selector.PatchOverlayFinder(
            base,
            patched_files={
                "sqlglot/errors.py": b"class PatchSentinelError(Exception):\n    pass\n",
                "sqlglot/optimizer/__init__.py": opt_init.encode("utf-8"),
                "sqlglot/optimizer/zz_v8_mod.py": b"ZZ_VAL = 99\n",
            },
            deleted_files={"sqlglot/time.py"},
        )
        spec_err = finder.find_spec("sqlglot.errors")
        self.assertIsNotNone(spec_err)
        self.assertIsInstance(spec_err.loader, selector._MemSourceLoader)

        spec_pkg = finder.find_spec("sqlglot.optimizer")
        self.assertIsNotNone(spec_pkg)
        self.assertEqual(spec_pkg.submodule_search_locations, [str(base / "sqlglot/optimizer")])

        spec_sub = finder.find_spec("sqlglot.optimizer.zz_v8_mod")
        self.assertIsNotNone(spec_sub)

        with self.assertRaises(ModuleNotFoundError):
            finder.find_spec("sqlglot.time")

        # Parent process sys.modules remains clean after real engine probe.
        eng = self._make_real_engine()
        orig_mods = {m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")}
        ctx_src = (base / "sqlglot/executor/context.py").read_text("utf-8")
        ns = ctx_src + "\n    def _zz_v8_noop(self):\n        if False:\n            return 1\n"
        # Use a harmless branch edit inside execute_tpcds-covered function in executor/context.py
        target = "class Context:"
        self.assertIn(target, ctx_src)
        import difflib
        ns2 = ctx_src.replace("def __init__(", "def _zz_unused_inner():\n    return 1\n\ndef __init__(", 1) if "def __init__(" in ctx_src else ns
        diff = (
            "diff --git a/sqlglot/executor/context.py b/sqlglot/executor/context.py\n"
            "index 0000000..1111111 100644\n"
            + "".join(difflib.unified_diff(ctx_src.splitlines(True), ns2.splitlines(True), "a/sqlglot/executor/context.py", "b/sqlglot/executor/context.py", n=3))
        )
        _ = eng.select_commit(diff)
        after_mods = {m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")}
        self.assertEqual(orig_mods, after_mods)

    def test_v8_tpcds_probe_pass_fail_and_serde(self) -> None:
        import difflib

        eng = self._make_real_engine()
        eng.max_s3_exec_qids = 57
        eng._s3_budget_env = 25.0
        base = RWX_DIR / "_sqlglot_baseline"
        tpcds_num = eng.tpcds_num
        self.assertIsNotNone(tpcds_num)

        # 1) Harmless non-noop edit in tpcds-covered executor/context.py -> probe passes & drops #1106
        ctx_rel = "sqlglot/executor/context.py"
        ctx_src = (base / ctx_rel).read_text("utf-8")
        # Inject dead branch inside Context.__init__ covered by tpcds
        target_line = "        self.tables = tables"
        self.assertIn(target_line, ctx_src)
        ctx_new = ctx_src.replace(target_line, "        if False:\n            _zz_tpcds = 1\n" + target_line, 1)
        patch_ok = (
            f"diff --git a/{ctx_rel} b/{ctx_rel}\nindex 0000000..1111111 100644\n"
            + "".join(difflib.unified_diff(ctx_src.splitlines(True), ctx_new.splitlines(True), f"a/{ctx_rel}", f"b/{ctx_rel}", n=3))
        )
        selector.TRACE = True
        sel_ok = eng.select_commit(patch_ok)
        self.assertNotIn(tpcds_num, sel_ok)
        self.assertTrue(any(r.startswith("probe_drop:tpcds:") for r in eng.reasons), eng.reasons)

        # 2) Sample 2jrq -> fails at q1 -> keeps #1106
        p_2jrq = (RWX_DIR / "sample/commits/2jrq.patch").read_text("utf-8")
        sel_2jrq = eng.select_commit(p_2jrq)
        self.assertIn(tpcds_num, sel_2jrq)
        self.assertTrue(any(r.startswith("probe_keep:tpcds:") for r in eng.reasons), eng.reasons)

        # 3) Sample 3utz (serde.py) -> pickle round-trip catches KeyError -> keeps #1106
        p_3utz = (RWX_DIR / "sample/commits/3utz.patch").read_text("utf-8")
        sel_3utz = eng.select_commit(p_3utz)
        self.assertIn(tpcds_num, sel_3utz)
        self.assertTrue(any(r.startswith("probe_keep:tpcds:") for r in eng.reasons), eng.reasons)

    def test_v8_max_nodes_probe_pass_fail_and_guard(self) -> None:
        import difflib

        eng = self._make_real_engine()
        base = RWX_DIR / "_sqlglot_baseline"
        mn_num = eng.max_nodes_num
        self.assertIsNotNone(mn_num)

        # 1) Decorator change on non-max_nodes_files (generators/mysql.py) -> Probe A passes & drops #1490
        mysql_rel = "sqlglot/generators/mysql.py"
        msrc = (base / mysql_rel).read_text("utf-8")
        target = "def _str_to_date_sql("
        if target not in msrc:
            target = "    def "
        idx = msrc.index("class MySQLGenerator")
        def_pos = msrc.index("\n    def ", idx)
        mnew = msrc[: def_pos + 1] + "    @staticmethod\n" + msrc[def_pos + 1 :]
        # Instead add a dummy decorator-triggered function at top-level or modify an existing function with a no-op decorator
        # Use r010-equivalent: modify MySQLGenerator or top-level function with @property? Easier: add dummy @staticmethod helper that parses fine and escalates once/decorator!
        mnew = msrc.replace("class MySQLGenerator(", "@(lambda c: c)\nclass MySQLGenerator(", 1)
        patch_mn_ok = (
            f"diff --git a/{mysql_rel} b/{mysql_rel}\nindex 0000000..1111111 100644\n"
            + "".join(difflib.unified_diff(msrc.splitlines(True), mnew.splitlines(True), f"a/{mysql_rel}", f"b/{mysql_rel}", n=3))
        )
        selector.TRACE = True
        sel_mn_ok = eng.select_commit(patch_mn_ok)
        self.assertNotIn(mn_num, sel_mn_ok)
        self.assertIn("probe_drop:max_nodes", eng.reasons)

        # 2) Broken import in sqlglot/optimizer/__init__.py -> Probe A catches ImportError -> keeps #1490
        opt_rel = "sqlglot/optimizer/__init__.py"
        osrc = (base / opt_rel).read_text("utf-8")
        onew = osrc + "\nfrom sqlglot.optimizer.zz_missing_mod import MissingSym\n"
        patch_mn_fail = (
            f"diff --git a/{opt_rel} b/{opt_rel}\nindex 0000000..1111111 100644\n"
            + "".join(difflib.unified_diff(osrc.splitlines(True), onew.splitlines(True), f"a/{opt_rel}", f"b/{opt_rel}", n=3))
        )
        sel_mn_fail = eng.select_commit(patch_mn_fail)
        self.assertIn(mn_num, sel_mn_fail)
        self.assertIn("probe_keep:max_nodes", eng.reasons)

        # 3) Inductive max_nodes on sqlglot/tokens.py: benign edit drops #1490, >=400 literal skips probe, broken max_nodes keeps #1490
        tok_rel = "sqlglot/tokens.py"
        tsrc = (base / tok_rel).read_text("utf-8")
        tnew = tsrc.replace("if isinstance(item, str)", "if type(item) is str or isinstance(item, str)", 1)
        patch_tok = (
            f"diff --git a/{tok_rel} b/{tok_rel}\nindex 0000000..1111111 100644\n"
            + "".join(difflib.unified_diff(tsrc.splitlines(True), tnew.splitlines(True), f"a/{tok_rel}", f"b/{tok_rel}", n=3))
        )
        sel_tok = eng.select_commit(patch_tok)
        self.assertNotIn(mn_num, sel_tok, eng.reasons)
        self.assertIn("probe_drop:max_nodes", eng.reasons)

        t_big = tsrc.replace("if isinstance(item, str)", "if isinstance(item, str) and len(item) != 500", 1)
        patch_tok_big = (
            f"diff --git a/{tok_rel} b/{tok_rel}\nindex 0000000..1111111 100644\n"
            + "".join(difflib.unified_diff(tsrc.splitlines(True), t_big.splitlines(True), f"a/{tok_rel}", f"b/{tok_rel}", n=3))
        )
        sel_tok_big = eng.select_commit(patch_tok_big)
        self.assertIn(mn_num, sel_tok_big)
        self.assertFalse(any("max_nodes" in r for r in eng.reasons), eng.reasons)

        par_rel = "sqlglot/parser.py"
        psrc = (base / par_rel).read_text("utf-8")
        pnew = psrc.replace(
            'self.raise_error(f"Maximum number of AST nodes ({self.max_nodes}) exceeded")',
            'self.raise_error("AST limit hit")',
            1,
        )
        self.assertNotEqual(pnew, psrc)
        patch_par_bad = (
            f"diff --git a/{par_rel} b/{par_rel}\nindex 0000000..1111111 100644\n"
            + "".join(difflib.unified_diff(psrc.splitlines(True), pnew.splitlines(True), f"a/{par_rel}", f"b/{par_rel}", n=3))
        )
        sel_par_bad = eng.select_commit(patch_par_bad)
        self.assertIn(mn_num, sel_par_bad)
        self.assertIn("probe_keep:max_nodes", eng.reasons)

    def test_v8_probe_budget_and_disable_toggle(self) -> None:
        eng = self._make_real_engine()
        p_2jrq = (RWX_DIR / "sample/commits/2jrq.patch").read_text("utf-8")
        selector.TRACE = True

        # Budget 0.0 -> skips probing cleanly
        eng.probe_cpu_budget_s = 0.0
        sel_zero_b = eng.select_commit(p_2jrq)
        self.assertIn(eng.tpcds_num, sel_zero_b)
        self.assertEqual(eng.last_probe_cpu_s, 0.0)
        self.assertFalse(any(r.startswith("probe_") for r in eng.reasons), eng.reasons)

        # Disabled toggle -> skips probing cleanly
        eng.probe_cpu_budget_s = 45.0
        selector.DISABLED.add("dynamic_probe")
        sel_dis = eng.select_commit(p_2jrq)
        self.assertIn(eng.tpcds_num, sel_dis)
        self.assertEqual(eng.last_probe_cpu_s, 0.0)
        self.assertFalse(any(r.startswith("probe_") for r in eng.reasons), eng.reasons)
        selector.DISABLED.discard("dynamic_probe")

    def test_v9_tpcds_three_stage_shortcircuit(self) -> None:
        import difflib

        eng = self._make_real_engine()
        base = RWX_DIR / "_sqlglot_baseline"
        tpcds_num = eng.tpcds_num
        self.assertIsNotNone(tpcds_num)
        self.assertEqual(len(eng.tpcds_parse_digests), 99)
        self.assertEqual(len(eng.tpcds_opt_digests), 57)

        # 1) Stage classification invariants
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/parsers/snowflake.py"}), (False, False))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/generators/mysql.py"}), (False, False))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/serde.py"}), (False, False))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/optimizer/simplify.py"}), (True, False))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/typing/snowflake.py"}), (True, False))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/transforms.py"}), (True, False))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/executor/context.py"}), (True, True))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/expressions/builders.py"}), (True, True))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/planner.py"}), (True, True))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/generators/python.py"}), (True, True))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/schema.py"}), (True, True))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/trie.py"}), (True, True))
        self.assertEqual(eng._classify_tpcds_stages({"sqlglot/time.py"}), (True, True))

        def _make_patch(rel: str, old_s: str, new_s: str) -> str:
            return (
                f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n"
                + "".join(difflib.unified_diff(old_s.splitlines(True), new_s.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
            )

        selector.TRACE = True

        # 2) Stage 1 short-circuit + s1->s2 + s1->s3 fallthrough (sqlglot/serde.py constant -> sav_b ~14.9s)
        serde_rel = "sqlglot/serde.py"
        ssrc = (base / serde_rel).read_text("utf-8")
        lines = ssrc.splitlines(True)
        ins_idx = next(i for i, l in enumerate(lines) if l.startswith(("def ", "class ", "TYPE_ANNOTATION")))
        snew = "".join(lines[:ins_idx] + ["_ZZ_V9_MOD_CONST = 123\n"] + lines[ins_idx:])
        patch_s1 = _make_patch(serde_rel, ssrc, snew)

        sel_s1 = eng.select_commit(patch_s1)
        self.assertNotIn(tpcds_num, sel_s1)
        self.assertTrue(any(r.startswith("probe_drop:tpcds:s1:") for r in eng.reasons), eng.reasons)
        self.assertLess(eng.last_probe_cpu_s, 3.0)
        self.assertIsNone(eng._raw_tpcds_tables)

        orig_p0 = eng.tpcds_parse_digests[0]
        orig_o0 = eng.tpcds_opt_digests[0]
        try:
            eng.tpcds_parse_digests[0] = "mismatch"
            sel_s12 = eng.select_commit(patch_s1)
            self.assertNotIn(tpcds_num, sel_s12)
            self.assertTrue(any(r.startswith("probe_drop:tpcds:s1->s2:") for r in eng.reasons), eng.reasons)
            self.assertIsNone(eng._raw_tpcds_tables)

            eng.tpcds_opt_digests[0] = "mismatch"
            sel_s13 = eng.select_commit(patch_s1)
            self.assertNotIn(tpcds_num, sel_s13)
            self.assertTrue(any(r.startswith("probe_drop:tpcds:s1->s3:") for r in eng.reasons), eng.reasons)
        finally:
            eng.tpcds_parse_digests[0] = orig_p0
            eng.tpcds_opt_digests[0] = orig_o0

        # Sample 3utz (serde.py) -> Stage 1 catches KeyError & keeps #1106
        p_3utz = (RWX_DIR / "sample/commits/3utz.patch").read_text("utf-8")
        sel_3utz = eng.select_commit(p_3utz)
        self.assertIn(tpcds_num, sel_3utz)
        self.assertTrue(any(r.startswith("probe_keep:tpcds:s1:") for r in eng.reasons), eng.reasons)

        # 3) Stage 2 short-circuit + s2->s3 fallthrough (simplify.py -> sav_b > thresh_b)
        simp_rel = "sqlglot/optimizer/simplify.py"
        simp_src = (base / simp_rel).read_text("utf-8")
        simp_target = "    return Simplifier(dialect=dialect).simplify("
        self.assertIn(simp_target, simp_src)
        simp_new = simp_src.replace(simp_target, "    if False:\n        _zz_v9_s2 = 1\n" + simp_target, 1)
        patch_s2 = _make_patch(simp_rel, simp_src, simp_new)

        eng._raw_tpcds_tables = None
        sel_s2 = eng.select_commit(patch_s2)
        self.assertNotIn(tpcds_num, sel_s2)
        self.assertTrue(any(r.startswith("probe_drop:tpcds:s2:") for r in eng.reasons), eng.reasons)
        self.assertLess(eng.last_probe_cpu_s, 6.0)
        self.assertIsNone(eng._raw_tpcds_tables)

        q_hit = next(q for q in eng.tpcds_exec_qids if (eng._tpcds_cov_qmask >> q) & 1) if eng._tpcds_cov_qmask else 0
        orig_oq = eng.tpcds_opt_digests[q_hit]
        try:
            eng.tpcds_opt_digests[q_hit] = "mismatch"
            sel_s23 = eng.select_commit(patch_s2)
            self.assertNotIn(tpcds_num, sel_s23)
            self.assertTrue(any(r.startswith("probe_drop:tpcds:s2->s3:") for r in eng.reasons), eng.reasons)
        finally:
            eng.tpcds_opt_digests[q_hit] = orig_oq

        # Sample 2jrq (eliminate_joins.py) -> Stage 2 detects opt diff, falls through to Stage 3 at q1 & keeps #1106
        p_2jrq = (RWX_DIR / "sample/commits/2jrq.patch").read_text("utf-8")
        sel_2jrq = eng.select_commit(p_2jrq)
        self.assertIn(tpcds_num, sel_2jrq)
        self.assertTrue(any(r.startswith("probe_keep:tpcds:s2->s3:") for r in eng.reasons), eng.reasons)

    def test_v10_line_stage_and_gen_memory_probe(self) -> None:
        import difflib

        eng = self._make_real_engine()
        base = RWX_DIR / "_sqlglot_baseline"
        tpcds_num = eng.tpcds_num
        gm_num = eng.gen_mem_num
        self.assertIsNotNone(tpcds_num)
        self.assertIsNotNone(gm_num)
        self.assertTrue(eng.tpcds_s2_lines)
        self.assertTrue(eng.tpcds_s3_lines)
        self.assertTrue(eng.tpcds_s3_query_cov)

        # 1) Verify > 100 lines moved out of Stage 3 in expressions/*, dialect.py, generator.py
        for pred in (
            lambda r: r.startswith("sqlglot/expressions/"),
            lambda r: r == "sqlglot/dialects/dialect.py",
            lambda r: r == "sqlglot/generator.py",
        ):
            u_all: set[tuple[str, int]] = set()
            u_s3: set[tuple[str, int]] = set()
            for r in set(eng.tpcds_query_cov) | set(eng.tpcds_s2_lines) | set(eng.tpcds_s3_lines):
                if not pred(r):
                    continue
                for ln in set(eng.tpcds_query_cov.get(r, {})) | eng.tpcds_s2_lines.get(r, frozenset()) | eng.tpcds_s3_lines.get(r, frozenset()):
                    u_all.add((r, ln))
                    if ln in eng.tpcds_s3_lines.get(r, frozenset()):
                        u_s3.add((r, ln))
            self.assertGreater(len(u_all - u_s3), 100)

        def _make_patch(rel: str, old_s: str, new_s: str) -> str:
            return (
                f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n"
                + "".join(difflib.unified_diff(old_s.splitlines(True), new_s.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
            )

        selector.TRACE = True

        # 2) Line-level Stage 1 short-circuit on core file (sqlglot/helper.py:360)
        h_rel = "sqlglot/helper.py"
        h_src = (base / h_rel).read_text("utf-8")
        h_target = "            yield from flatten(value)\n"
        self.assertIn(h_target, h_src)
        h_new = h_src.replace(h_target, "            if False:\n                _zz_s1 = 1\n" + h_target, 1)
        eng._raw_tpcds_tables = None
        sel_h = eng.select_commit(_make_patch(h_rel, h_src, h_new))
        self.assertNotIn(tpcds_num, sel_h)
        self.assertTrue(any(r.startswith("probe_drop:tpcds:s1:") for r in eng.reasons), eng.reasons)
        self.assertLess(eng.last_probe_cpu_s, 3.0)
        self.assertIsNone(eng._raw_tpcds_tables)

        # 3) Line-level Stage 2 short-circuit on sqlglot/expressions/builders.py:680
        b_rel = "sqlglot/expressions/builders.py"
        b_src = (base / b_rel).read_text("utf-8")
        b_target = "    for k, v in tuple(expression.args.items()):\n"
        self.assertIn(b_target, b_src)
        b_new = b_src.replace(b_target, "    if False:\n        _zz_s2 = 1\n" + b_target, 1)
        eng._raw_tpcds_tables = None
        sel_b = eng.select_commit(_make_patch(b_rel, b_src, b_new))
        self.assertNotIn(tpcds_num, sel_b)
        self.assertTrue(any(r.startswith("probe_drop:tpcds:s2:") for r in eng.reasons), eng.reasons)
        self.assertLess(eng.last_probe_cpu_s, 6.0)
        self.assertIsNone(eng._raw_tpcds_tables)

        # 4) Partial Stage-3 query bitmask s3(k) on sqlglot/expressions/core.py:957 & non-line conservative fallback
        c_rel = "sqlglot/expressions/core.py"
        c_src = (base / c_rel).read_text("utf-8")
        c_target = "        if not table_alias:\n            return []\n"
        self.assertIn(c_target, c_src)
        c_new = c_src.replace(
            c_target,
            "        if not table_alias:\n            if False:\n                _zz_s3p = 1\n            return []\n",
            1,
        )
        orig_cap = eng.max_s3_exec_qids
        orig_s3b = eng._s3_budget_env
        eng.max_s3_exec_qids = 57
        eng._s3_budget_env = 25.0
        p_c = _make_patch(c_rel, c_src, c_new)
        try:
            sel_sc, secs_c, snap_c = eng._select_static_commit(p_c)
            ctx_c = eng._prepare_probe_context(secs_c)
            plan_c = eng._plan_commit_probes(sel_sc, snap_c, ctx_c, 45.0, 25.0, 45.0)
            self.assertEqual(len(plan_c["items"]["b"]["s3_exec_qids"]), 31)
            sel_c = eng.select_commit(p_c)
        finally:
            eng.max_s3_exec_qids = orig_cap
            eng._s3_budget_env = orig_s3b
        self.assertNotIn(tpcds_num, sel_c)
        self.assertTrue(
            any(r.startswith(("probe_drop:tpcds:s3(31):", "probe_drop:tpcds:s2+plan:")) for r in eng.reasons),
            eng.reasons,
        )

        # Non-line (cls_usage on generator.py) keeps _tpcds_non_line = True
        g_rel = "sqlglot/generator.py"
        g_src = (base / g_rel).read_text("utf-8")
        g_pos = g_src.index("class Generator:\n") + len("class Generator:\n")
        g_new = g_src[:g_pos] + "    _ZZ_V10_CLS = 1\n" + g_src[g_pos:]
        orig_b = eng.probe_cpu_budget_s
        eng.probe_cpu_budget_s = 0.0
        _ = eng.select_commit(_make_patch(g_rel, g_src, g_new))
        eng.probe_cpu_budget_s = orig_b
        self.assertTrue(eng._tpcds_non_line)

        # 5) test_generator_memory (#1224) pass & intentional memory-leak detection
        d_rel = "sqlglot/generators/dune.py"
        d_src = (base / d_rel).read_text("utf-8")
        d_ok = d_src.replace("    TRY_SUPPORTED = False\n", "    _ZZ_V10_OK = 1\n    TRY_SUPPORTED = False\n", 1)
        sel_d_ok = eng.select_commit(_make_patch(d_rel, d_src, d_ok))
        self.assertNotIn(gm_num, sel_d_ok)
        self.assertIn("probe_drop:gen_mem", eng.reasons)
        self.assertLess(eng.last_probe_cpu_s, 2.0)

        d_leak = "_V10_LEAK = []\n" + d_src.replace(
            "    TRY_SUPPORTED = False\n",
            "    TRY_SUPPORTED = False\n\n    def __init__(self, *args, **kwargs):\n        super().__init__(*args, **kwargs)\n        _V10_LEAK.append([0] * 100)\n",
            1,
        )
        sel_d_leak = eng.select_commit(_make_patch(d_rel, d_src, d_leak))
        self.assertIn(gm_num, sel_d_leak)
        self.assertIn("probe_keep:gen_mem", eng.reasons)

    def test_v11_fast_test_oracle_and_priority_caps(self) -> None:
        import difflib
        import importlib

        eng = self._make_real_engine()
        base = RWX_DIR / "_sqlglot_baseline"
        selector.TRACE = True

        # 1) Passing 0-fail sample commits drop to 0 tests (including TestOptimizer::test_optimize via opt_conn_digests!)
        for cid in ("2qn5", "5y72", "8y7j", "fryt", "ktz3"):
            patch_txt = (RWX_DIR / f"sample/commits/{cid}.patch").read_text("utf-8")
            chosen = eng.select_commit(patch_txt)
            self.assertEqual(chosen, set(), f"{cid}: expected empty selection, got {chosen}, reasons={eng.reasons}")
            self.assertTrue(any(r.startswith("probe_drop:fast:") for r in eng.reasons), (cid, eng.reasons))

        # 2) Failing partial sample commit 6ze8 keeps the failing test (& fast fail-fast stops importing extra micro-tests)
        p_6ze8 = (RWX_DIR / "sample/commits/6ze8.patch").read_text("utf-8")
        sel_6ze8 = eng.select_commit(p_6ze8)
        regexp_split_num = eng.id_to_num["tests/dialects/test_dremio.py::TestDremio::test_regexp_split"]
        self.assertIn(regexp_split_num, sel_6ze8, eng.reasons)
        self.assertTrue(any(r.startswith(("probe_drop:fast:", "probe_keep:fast:")) for r in eng.reasons), eng.reasons)

        # 3) Direct _probe_fast_tests check inside a forked overlay child:
        #    setUpClass runs once, failing assertion / SkipTest / _MissingDepError / broken import module all KEPT!
        fake_tests_json = {
            "tests": [
                {"number": 0, "id": "tests/test_zz_v11_unit.py::TestZZ::test_pass_a", "file": "tests/test_zz_v11_unit.py", "seconds": 0.50},
                {"number": 1, "id": "tests/test_zz_v11_unit.py::TestZZ::test_pass_b", "file": "tests/test_zz_v11_unit.py", "seconds": 0.50},
                {"number": 2, "id": "tests/test_zz_v11_unit.py::TestZZ::test_fail", "file": "tests/test_zz_v11_unit.py", "seconds": 0.50},
                {"number": 3, "id": "tests/test_zz_v11_unit.py::TestZZ::test_skip", "file": "tests/test_zz_v11_unit.py", "seconds": 0.50},
                {"number": 4, "id": "tests/test_zz_v11_unit.py::TestZZ::test_missing_dep", "file": "tests/test_zz_v11_unit.py", "seconds": 0.50},
                {"number": 5, "id": "tests/test_zz_v11_broken.py::TestBroken::test_never_pass", "file": "tests/test_zz_v11_broken.py", "seconds": 0.50},
            ],
        }
        fake_maps = {
            "version": 9,
            "test_ids": [t["id"] for t in fake_tests_json["tests"]],
            "line_cov": {},
        }
        fake_eng = selector.SelectorEngine(fake_tests_json, fake_maps, base)
        unit_src = (
            b"import unittest, tempfile\n"
            b"class TestZZ(unittest.TestCase):\n"
            b"    setup_calls = 0\n"
            b"    @classmethod\n"
            b"    def setUpClass(cls):\n"
            b"        cls.setup_calls += 1\n"
            b"    def test_pass_a(self):\n"
            b"        self.assertEqual(self.setup_calls, 1)\n"
            b"    def test_pass_b(self):\n"
            b"        self.assertEqual(self.setup_calls, 1)\n"
            b"    def test_fail(self):\n"
            b"        self.assertEqual(1, 2)\n"
            b"    def test_skip(self):\n"
            b"        self.skipTest('skip must keep')\n"
            b"    def test_missing_dep(self):\n"
            b"        tempfile.NamedTemporaryFile()\n"
        )
        broken_src = b"raise ImportError('broken test module')\n"
        import os as _os
        import json as _json
        import sys as _sys

        r_fd, w_fd = _os.pipe()
        pid = _os.fork()
        if pid == 0:
            _os.close(r_fd)
            try:
                src_str = str(base)
                if src_str not in _sys.path:
                    _sys.path.insert(0, src_str)
                _sys.meta_path.insert(
                    0,
                    selector.PatchOverlayFinder(
                        base,
                        {"tests/test_zz_v11_unit.py": unit_src, "tests/test_zz_v11_broken.py": broken_src},
                        set(),
                    ),
                )
                selector._install_fast_probe_sandbox()
                passed_nums = fake_eng._probe_fast_tests([0, 1, 2, 3, 4, 5], set(), False)
                mod_zz = importlib.import_module("tests.test_zz_v11_unit")
                _os.write(w_fd, _json.dumps({"passed": passed_nums, "setup_calls": mod_zz.TestZZ.setup_calls}).encode("ascii"))
            finally:
                _os.close(w_fd)
                _os._exit(0)
        _os.close(w_fd)
        raw = b""
        while True:
            chunk = _os.read(r_fd, 4096)
            if not chunk:
                break
            raw += chunk
        _os.close(r_fd)
        _os.waitpid(pid, 0)
        child_res = _json.loads(raw.decode("ascii"))
        self.assertEqual(child_res["setup_calls"], 1)
        self.assertEqual(sorted(child_res["passed"]), [0, 1])

        # 4) Stage-3 max_s3_exec_qids <= 20 cap & separate P1 (30s) / P2 (15s) budgets
        def _make_patch(rel: str, old_s: str, new_s: str) -> str:
            return (
                f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n"
                + "".join(difflib.unified_diff(old_s.splitlines(True), new_s.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
            )

        c_rel = "sqlglot/expressions/core.py"
        c_src = (base / c_rel).read_text("utf-8")
        c_new = c_src.replace(
            "        if not table_alias:\n            return []\n",
            "        if not table_alias:\n            if False:\n                _zz_cap = 1\n            return []\n",
            1,
        )
        with mock.patch.dict(os.environ, {"BLAST_MAX_S3_QIDS": "20"}):
            eng2 = self._make_real_engine()
            self.assertEqual(eng2.max_s3_exec_qids, 20)
            sel_cap = eng2.select_commit(_make_patch(c_rel, c_src, c_new))
            self.assertIn(eng2.tpcds_num, sel_cap)
            self.assertEqual(eng2.last_probe_cpu_s, 0.0)
            self.assertFalse(any(r.startswith("probe_drop:tpcds:") for r in eng2.reasons), eng2.reasons)

        # Exhaust P2 while P1 remains available -> P1 fast probes still run & drop!
        eng2.s3_probe_cpu_s = eng2.probe_cpu_budget_s
        eng2.fast_probe_cpu_s = 0.0
        eng2.total_probe_cpu_s = 15.0
        p_2qn5 = (RWX_DIR / "sample/commits/2qn5.patch").read_text("utf-8")
        sel_p1 = eng2.select_commit(p_2qn5)
        self.assertEqual(sel_p1, set(), eng2.reasons)
        self.assertTrue(any(r.startswith("probe_drop:fast:") for r in eng2.reasons), eng2.reasons)

    def test_v12_full_oracle_slow_probes_and_batch_roi(self) -> None:
        import difflib

        eng = self._make_real_engine()
        base = RWX_DIR / "_sqlglot_baseline"
        selector.TRACE = True

        # 1) Verify v12 map schema and expanded default cap
        maps = selector.load_maps(RWX_DIR / "maps/blast_maps.pkl.zlib")
        self.assertGreaterEqual(int(maps.get("version", 0)), 10)
        self.assertLessEqual(len(maps.get("fast_unprobed_ids", ())), 10)
        self.assertEqual(eng.max_s3_exec_qids, 57)
        self.assertTrue(bool(eng.tpcds_opt_asts_zlib))
        self.assertEqual(len(eng.opt_tpch_digests), 22)
        self.assertEqual(len(eng.opt_tpcds_parse_digests), 99)

        def _make_patch(rel: str, old_s: str, new_s: str) -> str:
            return (
                f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n"
                + "".join(difflib.unified_diff(old_s.splitlines(True), new_s.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
            )

        # 2) Direct Plan(opt_q)+PythonExecutor drops benign Stage-3 executor edit and keeps broken one!
        env_rel = "sqlglot/executor/env.py"
        env_src = (base / env_rel).read_text("utf-8")
        env_ok = env_src.replace(
            "def sql_not(value):\n",
            "def sql_not(value):\n    if False:\n        _zz_v12 = 1\n",
            1,
        )
        eng._raw_tpcds_tables = None
        sel_env_ok = eng.select_commit(_make_patch(env_rel, env_src, env_ok))
        self.assertNotIn(eng.tpcds_num, sel_env_ok, eng.reasons)
        self.assertTrue(any(r.startswith("probe_drop:tpcds:s3") for r in eng.reasons), eng.reasons)

        env_bad = env_src.replace(
            "    return None if value is None else not bool(value)\n",
            "    return None if value is None else bool(value)\n",
            1,
        )
        sel_env_bad = eng.select_commit(_make_patch(env_rel, env_src, env_bad))
        self.assertIn(eng.tpcds_num, sel_env_bad, eng.reasons)
        self.assertTrue(any(r.startswith("probe_keep:tpcds:s3") for r in eng.reasons), eng.reasons)

        # 3) #1295 test_annotate_funcs: drops benign leaf edit and fails fast on sample commit j6uz
        p_j6uz = (RWX_DIR / "sample/commits/j6uz.patch").read_text("utf-8")
        sel_j6uz = eng.select_commit(p_j6uz)
        self.assertIn(eng.ann_funcs_num, sel_j6uz, eng.reasons)
        self.assertLess(eng.last_probe_cpu_s, 0.60)

        bq_rel = "sqlglot/parsers/bigquery.py"
        bq_src = (base / bq_rel).read_text("utf-8")
        bq_new = bq_src.replace(
            "        for arg_key in MAKE_INTERVAL_KWARGS:\n",
            "        if False:\n            _zz_v12_bq = 1\n        for arg_key in MAKE_INTERVAL_KWARGS:\n",
            1,
        )
        bq_patch = _make_patch(bq_rel, bq_src, bq_new)
        static_bq, _, _ = eng._select_static_commit(bq_patch)
        self.assertIn(eng.ann_funcs_num, static_bq)
        sel_bq = eng.select_commit(bq_patch)
        self.assertEqual(sel_bq, set(), eng.reasons)
        self.assertIn("probe_drop:ann_funcs:leaf", eng.reasons)
        if eng.opt_tpch_num is not None:
            self.assertNotIn(eng.opt_tpch_num, sel_bq, eng.reasons)

        # 4) Global ROI Priority Queue in select_batch: under tight budget (3.0s), Tier-1 high-ROI commit
        #    (2qn5) runs BEFORE Stage-3 heavy commit (3utz) even though 3utz appears first in input order!
        eng_roi = self._make_real_engine()
        eng_roi.probe_cpu_budget_s = 3.0
        p_3utz = (RWX_DIR / "sample/commits/3utz.patch").read_text("utf-8")
        p_2qn5 = (RWX_DIR / "sample/commits/2qn5.patch").read_text("utf-8")
        res = eng_roi.select_batch([p_3utz, p_2qn5], scale_budget=False)
        self.assertEqual(len(res), 2)
        self.assertIn(eng_roi.tpcds_num, res[0][0])
        self.assertEqual(res[1][0], set(), res[1][1])

    def test_v13_roi_turbo_inductive_max_nodes_and_topk(self) -> None:
        import difflib

        eng = self._make_real_engine()
        base = RWX_DIR / "_sqlglot_baseline"
        selector.TRACE = True

        def _make_patch(rel: str, old_s: str, new_s: str) -> str:
            return (
                f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n"
                + "".join(difflib.unified_diff(old_s.splitlines(True), new_s.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
            )

        # 1) Line-level s3 qmask in sqlglot/executor/env.py runs <= 10 queries instead of 57
        env_rel = "sqlglot/executor/env.py"
        env_src = (base / env_rel).read_text("utf-8")
        env_ok = env_src.replace(
            "def sql_not(value):\n",
            "def sql_not(value):\n    if False:\n        _zz_v13 = 1\n",
            1,
        )
        patch_env = _make_patch(env_rel, env_src, env_ok)
        sel_static_env, secs_env, snap_env = eng._select_static_commit(patch_env)
        ctx_env = eng._prepare_probe_context(secs_env)
        self.assertIsNotNone(ctx_env)
        plan_env = eng._plan_commit_probes(sel_static_env, snap_env, ctx_env, 45.0, 28.0, 45.0)
        self.assertIsNotNone(plan_env)
        self.assertIn("b", plan_env["items"])
        self.assertLessEqual(len(plan_env["items"]["b"]["s3_exec_qids"]), 10)

        # 2) Inductive max_nodes on core sqlglot/expressions/core.py drops #1490
        core_rel = "sqlglot/expressions/core.py"
        core_src = (base / core_rel).read_text("utf-8")
        core_new = core_src.replace(
            "        if self._meta is None:\n            self._meta = {}\n",
            "        if self._meta is None:\n            if False:\n                _zz_v13_core = 1\n            self._meta = {}\n",
            1,
        )
        core_patch = _make_patch(core_rel, core_src, core_new)
        stat_core, _, _ = eng._select_static_commit(core_patch)
        self.assertIn(eng.max_nodes_num, stat_core)
        sel_core = eng.select_commit(core_patch)
        self.assertNotIn(eng.max_nodes_num, sel_core, eng.reasons)
        self.assertIn("probe_drop:max_nodes", eng.reasons)

        # 3) Multi-Worker Cheap Slow Bundle / In-Fork Unmasking drops #1490, #1106, #1123, and #1107
        msrc = (base / "sqlglot/generators/mysql.py").read_text("utf-8")
        mnew = msrc.replace("class MySQLGenerator(", "@(lambda c: c)\nclass MySQLGenerator(", 1)
        p_mysql = _make_patch("sqlglot/generators/mysql.py", msrc, mnew)
        eng_m = self._make_real_engine()
        selector.TRACE = True
        selector.DISABLED.add("dynamic_probe")
        sel_stat = eng_m.select_commit(p_mysql)
        selector.DISABLED.discard("dynamic_probe")
        self.assertGreater(len(sel_stat), 300)

        sel_my = eng_m.select_commit(p_mysql)
        self.assertLess(len(sel_my), len(sel_stat))
        for reason in (
            "probe_drop:max_nodes",
            "probe_drop:tpcds:s1:57",
            "probe_drop:opt_tpch",
            "probe_drop:tpch",
        ):
            self.assertIn(reason, eng_m.reasons, eng_m.reasons)
        test_ids = [t["id"] for t in json.loads((RWX_DIR / "sample/tests.json").read_text("utf-8"))["tests"]]
        dropped_ids = {test_ids[n] for n in (sel_stat - sel_my)}
        kept_ids = {test_ids[n] for n in sel_my}
        for tid in (
            "tests/test_parser.py::TestParser::test_max_nodes",
            "tests/test_executor.py::TestExecutor::test_execute_tpcds",
            "tests/test_executor.py::TestExecutor::test_optimized_tpch",
            "tests/test_executor.py::TestExecutor::test_execute_tpch",
        ):
            self.assertIn(tid, dropped_ids, eng_m.reasons)
            self.assertNotIn(tid, kept_ids)

    def test_v14_stage3_restore_and_plan_shortcircuit(self) -> None:
        import difflib

        eng = self._make_real_engine()
        self.assertEqual(eng.max_s3_exec_qids, 57)
        base = RWX_DIR / "_sqlglot_baseline"
        pl_rel = "sqlglot/planner.py"
        pl_src = (base / pl_rel).read_text("utf-8")

        def _make_patch(old_s: str, new_s: str) -> str:
            return (
                f"diff --git a/{pl_rel} b/{pl_rel}\nindex 0000000..1111111 100644\n"
                + "".join(difflib.unified_diff(old_s.splitlines(True), new_s.splitlines(True), f"a/{pl_rel}", f"b/{pl_rel}", n=3))
            )

        selector.TRACE = True
        pl_ok = pl_src.replace("class Plan:", "@(lambda c: c)\nclass Plan:", 1)
        p_ok = _make_patch(pl_src, pl_ok)
        sel_ok = eng.select_commit(p_ok)
        self.assertNotIn(eng.tpcds_num, sel_ok, eng.reasons)
        self.assertIn("probe_drop:tpcds:s2+plan:57", eng.reasons, eng.reasons)

        pl_bad = pl_src.replace(
            "        self.root: Step = Step.from_expression(self.expression)",
            "        self.root: Step = Step.from_expression(self.expression)\n        raise RuntimeError('boom')",
            1,
        )
        p_bad = _make_patch(pl_src, pl_bad)
        sel_bad = eng.select_commit(p_bad)
        self.assertIn(eng.tpcds_num, sel_bad, eng.reasons)


if __name__ == "__main__":
    unittest.main(verbosity=2)
