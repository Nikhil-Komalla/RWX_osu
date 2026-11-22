"""Unit and regression tests for v4 Blast Radius selector.py (no sample/answers dependencies)."""
from __future__ import annotations

import ast
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

        # 2) New module added under sqlglot/ picks up pkg_walkers + sibling lazy_test_importers
        eng.lazy_test_importers["sqlglot/typing/existing.py"] = ["tests/test_beta.py"]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
