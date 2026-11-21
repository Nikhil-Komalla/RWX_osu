#!/usr/bin/env python3
"""Blast Radius AST + coverage + dynamic-dependency test selector & optimal worker packer."""
from __future__ import annotations

import ast
import difflib
import gzip
import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

DATA_DIR = Path(os.environ.get("BLAST_DATA_DIR", "/data"))
DEFAULT_SRC = Path(__file__).resolve().parent / "sqlglot_src"
if not DEFAULT_SRC.exists():
    alt = Path(__file__).resolve().parent / "_sqlglot_baseline"
    if alt.exists():
        DEFAULT_SRC = alt
SQLGLOT_SRC = Path(os.environ.get("BLAST_SQLGLOT_DIR", str(DEFAULT_SRC)))

DEFAULT_MAPS = Path(__file__).resolve().parent / "maps" / "blast_maps.json.gz"
MAPS_PATH = Path(os.environ.get("BLAST_MAPS_PATH", str(DEFAULT_MAPS)))

MAX_WORKERS = 8
WORKER_COST_MS = 2000

_CODING_RE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
_INERT_EXTS = {
    ".md",
    ".markdown",
    ".rst",
    ".txt",
    ".yml",
    ".yaml",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".dockerignore",
    ".png",
    ".svg",
    ".jpg",
    ".jpeg",
    ".ico",
    ".pdf",
}
_INERT_NAMES = {
    "LICENSE",
    "NOTICE",
    "CHANGELOG",
    "AUTHORS",
    "CODEOWNERS",
    "Makefile",
    ".pre-commit-config.yaml",
    ".coveragerc",
}


def _encoding_cookie(text: str) -> Optional[str]:
    lines = text.splitlines()[:2]
    for line in lines:
        m = _CODING_RE.match(line.encode("utf-8", errors="ignore"))
        if m:
            return m.group(1).decode("ascii", errors="ignore").lower()
    return None


def _is_docstring_stmt(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _node_dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _stmt_sig(node: ast.stmt) -> Tuple[str, str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ("fn", node.name)
    if isinstance(node, ast.ClassDef):
        return ("cls", node.name)
    return ("st", _node_dump(node))


def _fn_signature_changed(old_fn: ast.AST, new_fn: ast.AST) -> bool:
    """True if function name, decorators, args, returns, or type_params changed."""
    if type(old_fn) is not type(new_fn):
        return True
    assert isinstance(old_fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    assert isinstance(new_fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    if old_fn.name != new_fn.name:
        return True
    if _node_dump(old_fn.args) != _node_dump(new_fn.args):
        return True
    if [_node_dump(d) for d in old_fn.decorator_list] != [_node_dump(d) for d in new_fn.decorator_list]:
        return True
    if (old_fn.returns is None) != (new_fn.returns is None):
        return True
    if old_fn.returns is not None and new_fn.returns is not None:
        if _node_dump(old_fn.returns) != _node_dump(new_fn.returns):
            return True
    if getattr(old_fn, "type_params", None) != getattr(new_fn, "type_params", None):
        old_tp = [_node_dump(x) for x in getattr(old_fn, "type_params", [])]
        new_tp = [_node_dump(x) for x in getattr(new_fn, "type_params", [])]
        if old_tp != new_tp:
            return True
    return False


def _header_lines(stmt: ast.stmt) -> range:
    """Return conservative line range covering the control header / signature of a compound stmt."""
    start = stmt.lineno
    end = start
    if isinstance(stmt, ast.If):
        end = getattr(stmt.test, "end_lineno", start) or start
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        end = getattr(stmt.iter, "end_lineno", start) or start
    elif isinstance(stmt, ast.While):
        end = getattr(stmt.test, "end_lineno", start) or start
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        if stmt.items:
            end = max(getattr(it.context_expr, "end_lineno", start) or start for it in stmt.items)
    elif isinstance(stmt, ast.Match):
        end = getattr(stmt.subject, "end_lineno", start) or start
    elif isinstance(stmt, ast.Try):
        end = start
    return range(start, end + 1)


def _find_inner_lambdas_only(old_expr: ast.AST, new_expr: ast.AST) -> Optional[List[ast.Lambda]]:
    """If old_expr -> new_expr differs ONLY inside nested Lambda bodies, return those old Lambdas."""
    if type(old_expr) is not type(new_expr):
        return None
    if isinstance(old_expr, ast.Lambda) and isinstance(new_expr, ast.Lambda):
        if _node_dump(old_expr.args) == _node_dump(new_expr.args):
            return [old_expr]
        return None
    if _node_dump(old_expr) == _node_dump(new_expr):
        return []
    if isinstance(old_expr, ast.Dict) and isinstance(new_expr, ast.Dict):
        if len(old_expr.keys) != len(new_expr.keys):
            return None
        out: List[ast.Lambda] = []
        for ok, ov, nk, nv in zip(old_expr.keys, old_expr.values, new_expr.keys, new_expr.values):
            if (ok is None) != (nk is None):
                return None
            if ok is not None and _node_dump(ok) != _node_dump(nk):  # type: ignore[arg-type]
                return None
            if _node_dump(ov) != _node_dump(nv):
                sub = _find_inner_lambdas_only(ov, nv)
                if sub is None:
                    return None
                out.extend(sub)
        return out
    if isinstance(old_expr, (ast.List, ast.Tuple, ast.Set)) and isinstance(new_expr, (ast.List, ast.Tuple, ast.Set)):
        if len(old_expr.elts) != len(new_expr.elts):
            return None
        out = []
        for oe, ne in zip(old_expr.elts, new_expr.elts):
            if _node_dump(oe) != _node_dump(ne):
                sub = _find_inner_lambdas_only(oe, ne)
                if sub is None:
                    return None
                out.extend(sub)
        return out
    if isinstance(old_expr, ast.Call) and isinstance(new_expr, ast.Call):
        if _node_dump(old_expr.func) != _node_dump(new_expr.func):
            return None
        if len(old_expr.args) != len(new_expr.args) or len(old_expr.keywords) != len(new_expr.keywords):
            return None
        out = []
        for oa, na in zip(old_expr.args, new_expr.args):
            if _node_dump(oa) != _node_dump(na):
                sub = _find_inner_lambdas_only(oa, na)
                if sub is None:
                    return None
                out.extend(sub)
        for okw, nkw in zip(old_expr.keywords, new_expr.keywords):
            if okw.arg != nkw.arg:
                return None
            if _node_dump(okw.value) != _node_dump(nkw.value):
                sub = _find_inner_lambdas_only(okw.value, nkw.value)
                if sub is None:
                    return None
                out.extend(sub)
        return out
    return None


def parse_git_diff(patch_text: str) -> List[Dict[str, Any]]:
    """Split unified git diff into per-file sections with hunks."""
    files: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    cur_hunk: Optional[Dict[str, Any]] = None

    lines = patch_text.splitlines(keepends=True)
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in lines:
        sline = line.rstrip("\r\n")
        if sline.startswith("diff --git "):
            if cur is not None:
                files.append(cur)
            parts = sline.split()
            a_path = parts[2][2:] if len(parts) >= 4 and parts[2].startswith("a/") else ""
            b_path = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            cur = {
                "a_path": a_path,
                "b_path": b_path,
                "rel_path": b_path or a_path,
                "status": "modified",
                "hunks": [],
            }
            cur_hunk = None
            continue
        if cur is None:
            continue
        if cur_hunk is None:
            if sline.startswith("--- "):
                p = sline[4:].strip()
                if p == "/dev/null":
                    cur["status"] = "added"
                elif p.startswith("a/"):
                    cur["a_path"] = p[2:]
                continue
            if sline.startswith("+++ "):
                p = sline[4:].strip()
                if p == "/dev/null":
                    cur["status"] = "deleted"
                    cur["rel_path"] = cur["a_path"]
                elif p.startswith("b/"):
                    cur["b_path"] = p[2:]
                    cur["rel_path"] = cur["b_path"]
                continue
            if sline.startswith("rename from "):
                cur["a_path"] = sline[12:].strip()
                continue
            if sline.startswith("rename to "):
                cur["b_path"] = sline[10:].strip()
                cur["rel_path"] = cur["b_path"]
                continue
        m = hunk_re.match(sline)
        if m:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            cur_hunk = {
                "old_start": old_start,
                "old_count": old_count,
                "new_start": new_start,
                "new_count": new_count,
                "lines": [],
            }
            cur["hunks"].append(cur_hunk)
            continue
        if cur_hunk is not None and (sline.startswith((" ", "+", "-", "\\")) or sline == ""):
            cur_hunk["lines"].append(line if sline != "" else " \n")

    if cur is not None:
        files.append(cur)
    return files


def apply_hunks_in_memory(old_text: str, hunks: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """Apply parsed unified hunks to old_text in memory. Returns (new_text, ok)."""
    if not hunks:
        return old_text, True
    old_lines = old_text.splitlines(keepends=True)
    out: List[str] = []
    pos = 0  # 0-based index into old_lines

    for h in hunks:
        old_start = h["old_start"]
        idx = max(0, old_start - 1) if h["old_count"] > 0 else max(0, old_start)
        if idx < pos:
            return "", False
        out.extend(old_lines[pos:idx])
        pos = idx
        for raw_l in h["lines"]:
            if not raw_l:
                continue
            tag = raw_l[0]
            content = raw_l[1:]
            if tag == "\\":
                continue
            elif tag == " ":
                if pos >= len(old_lines):
                    return "", False
                if old_lines[pos].rstrip("\r\n") != content.rstrip("\r\n"):
                    return "", False
                out.append(old_lines[pos])
                pos += 1
            elif tag == "-":
                if pos >= len(old_lines):
                    return "", False
                if old_lines[pos].rstrip("\r\n") != content.rstrip("\r\n"):
                    return "", False
                pos += 1
            elif tag == "+":
                out.append(content)

    out.extend(old_lines[pos:])
    return "".join(out), True


class SelectorEngine:
    """Precomputed-map + AST-diff test selector."""

    def __init__(self, tests_json: Dict[str, Any], maps: Dict[str, Any], src_root: Path) -> None:
        self.src_root = src_root
        tests = tests_json["tests"]

        self.id_to_num: Dict[str, int] = {}
        self.num_to_ms: List[int] = [0] * len(tests)
        self.file_to_nums: Dict[str, List[int]] = defaultdict(list)
        self.class_to_nums: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        self.method_to_num: Dict[Tuple[str, str, str], int] = {}
        self.func_to_num: Dict[Tuple[str, str], int] = {}
        self.all_nums: Set[int] = set()

        for t in tests:
            num = int(t["number"])
            tid = t["id"]
            tfile = t["file"]
            ms = math.floor(float(t["seconds"]) * 1000.0 + 0.5)
            self.id_to_num[tid] = num
            self.num_to_ms[num] = ms
            self.file_to_nums[tfile].append(num)
            self.all_nums.add(num)
            parts = tid.split("::")
            if len(parts) == 3:
                self.class_to_nums[(tfile, parts[1])].append(num)
                self.method_to_num[(tfile, parts[1], parts[2])] = num
            elif len(parts) == 2:
                self.func_to_num[(tfile, parts[1])] = num

        # Map baseline test indices -> live /data/tests.json numbers.
        test_ids: List[str] = maps["test_ids"]
        self.idx_to_num: List[Optional[int]] = [self.id_to_num.get(tid) for tid in test_ids]

        self.raw_line_cov: Dict[str, Dict[str, List[int]]] = maps.get("line_cov", {})
        self.raw_file_access: Dict[str, List[int]] = maps.get("file_access", {})
        self.raw_class_usage: Dict[str, List[int]] = maps.get("class_usage", {})
        self.raw_pkg_walkers: Dict[str, List[int]] = maps.get("pkg_walkers", {})
        self.rev_imports: Dict[str, List[str]] = maps.get("rev_imports", {})
        self.dialect_typing_links: Dict[str, List[str]] = maps.get("dialect_typing_links", {})

        # Caches populated lazily per touched file for fast CPU time.
        self._line_cache: Dict[str, Dict[int, Set[int]]] = {}
        self._mod_cov_cache: Dict[str, Set[int]] = {}
        self._file_acc_cache: Dict[str, Set[int]] = {}
        self._cls_use_cache: Dict[str, Set[int]] = {}
        self._ast_cache: Dict[str, Tuple[str, Optional[ast.Module]]] = {}

    def _remap(self, idxs: Iterable[int]) -> Set[int]:
        out: Set[int] = set()
        m = self.idx_to_num
        for i in idxs:
            n = m[i]
            if n is not None:
                out.add(n)
        return out

    def _file_access(self, rel_path: str) -> Set[int]:
        cached = self._file_acc_cache.get(rel_path)
        if cached is None:
            raw = self.raw_file_access.get(rel_path)
            cached = self._remap(raw) if raw else set()
            self._file_acc_cache[rel_path] = cached
        return cached

    def _class_usage(self, cls_key: str) -> Set[int]:
        cached = self._cls_use_cache.get(cls_key)
        if cached is None:
            raw = self.raw_class_usage.get(cls_key)
            cached = self._remap(raw) if raw else set()
            self._cls_use_cache[cls_key] = cached
        return cached

    def _line_map(self, rel_path: str) -> Dict[int, Set[int]]:
        cached = self._line_cache.get(rel_path)
        if cached is None:
            raw = self.raw_line_cov.get(rel_path)
            if not raw:
                cached = {}
            else:
                idx_map = self.idx_to_num
                cached = {}
                for lns, idxs in raw.items():
                    s = {idx_map[i] for i in idxs if idx_map[i] is not None}
                    if s:
                        cached[int(lns)] = s
            self._line_cache[rel_path] = cached
        return cached

    def _module_cov(self, rel_path: str) -> Set[int]:
        cached = self._mod_cov_cache.get(rel_path)
        if cached is None:
            lmap = self._line_map(rel_path)
            out: Set[int] = set()
            for s in lmap.values():
                out.update(s)
            cached = out
            self._mod_cov_cache[rel_path] = cached
        return cached

    def _lines_cov(self, rel_path: str, lines: Iterable[int]) -> Set[int]:
        lmap = self._line_map(rel_path)
        if not lmap:
            return set()
        out: Set[int] = set()
        for ln in lines:
            s = lmap.get(ln)
            if s:
                out.update(s)
        return out

    def _pkg_walker_tests(self, rel_path: str) -> Set[int]:
        out: Set[int] = set()
        parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        for pkg, idxs in self.raw_pkg_walkers.items():
            if parent == pkg or parent.startswith(pkg + "/"):
                out.update(self._remap(idxs))
        return out

    def _baseline_src_and_ast(self, rel_path: str) -> Tuple[str, Optional[ast.Module]]:
        cached = self._ast_cache.get(rel_path)
        if cached is not None:
            return cached
        p = self.src_root / rel_path
        if not p.is_file():
            res = ("", None)
            self._ast_cache[rel_path] = res
            return res
        src = p.read_text("utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            tree = None
        res = (src, tree)
        self._ast_cache[rel_path] = res
        return res

    def _importer_test_files(self, rel_path: str) -> Set[int]:
        out: Set[int] = set()
        for rev in self.rev_imports.get(rel_path, ()):
            if rev in self.file_to_nums:
                out.update(self.file_to_nums[rev])
        return out

    def _module_wide_fallback(self, rel_path: str, old_ast: Optional[ast.Module]) -> Set[int]:
        out: Set[int] = set()
        out.update(self._file_access(rel_path))
        out.update(self._pkg_walker_tests(rel_path))
        out.update(self._module_cov(rel_path))

        # For sqlglot/typing/<d>.py, include tests of linked dialect classes.
        for link_cls in self.dialect_typing_links.get(rel_path, ()):
            out.update(self._class_usage(link_cls))

        # Include class_usage for all classes defined in this module.
        if old_ast is not None:
            for node in ast.walk(old_ast):
                if isinstance(node, ast.ClassDef):
                    # Approximate top-level or nested class lookup by checking matching suffix keys.
                    prefix = f"{rel_path}::"
                    for ckey in self.raw_class_usage:
                        if ckey.startswith(prefix):
                            out.update(self._class_usage(ckey))
                    break

        is_dialect_mod = (
            (
                rel_path.startswith("sqlglot/dialects/")
                and rel_path not in ("sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py")
            )
            or (
                rel_path.startswith("sqlglot/parsers/")
                and rel_path not in ("sqlglot/parsers/__init__.py",)
            )
            or (
                rel_path.startswith("sqlglot/generators/")
                and rel_path not in ("sqlglot/generators/__init__.py",)
            )
            or rel_path.startswith("sqlglot/typing/")
        )
        if not is_dialect_mod:
            out.update(self._importer_test_files(rel_path))
            if rel_path == "sqlglot/__init__.py":
                out.update(self.all_nums)
        return out

    def _cls_key_candidates(self, rel_path: str, cls_stack: List[str]) -> str:
        return f"{rel_path}::{'.'.join(cls_stack)}"

    def _diff_fn_bodies(
        self,
        old_stmts: List[ast.stmt],
        new_stmts: List[ast.stmt],
        rel_path: str,
        cls_stack: List[str],
        old_fn_root: ast.stmt,
        selected: Set[int],
    ) -> None:
        """Recursively diff two function-body statement lists cleanly."""
        if old_stmts == new_stmts:
            return
        osigs = [_stmt_sig(s) for s in old_stmts]
        nsigs = [_stmt_sig(s) for s in new_stmts]
        sm = difflib.SequenceMatcher(None, osigs, nsigs, autojunk=False)

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                # Same signature (nested def/cls name match) -> check full AST & recurse if changed.
                for oi, nj in zip(range(i1, i2), range(j1, j2)):
                    os_ = old_stmts[oi]
                    ns_ = new_stmts[nj]
                    if _node_dump(os_) != _node_dump(ns_):
                        if isinstance(os_, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                            ns_, (ast.FunctionDef, ast.AsyncFunctionDef)
                        ):
                            if _fn_signature_changed(os_, ns_):
                                selected.update(self._lines_cov(rel_path, range(os_.lineno, (os_.end_lineno or os_.lineno) + 1)))
                            else:
                                self._diff_fn_bodies(os_.body, ns_.body, rel_path, cls_stack, os_, selected)
                        else:
                            selected.update(self._lines_cov(rel_path, range(os_.lineno, (os_.end_lineno or os_.lineno) + 1)))
                continue

            if tag == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1:
                os_ = old_stmts[i1]
                ns_ = new_stmts[j1]
                # Try descending into matching compound control-flow statements.
                if type(os_) is type(ns_) and isinstance(os_, ast.If) and isinstance(ns_, ast.If):
                    if _node_dump(os_.test) != _node_dump(ns_.test):
                        selected.update(self._lines_cov(rel_path, _header_lines(os_)))
                    if [_node_dump(x) for x in os_.body] != [_node_dump(x) for x in ns_.body]:
                        self._diff_fn_bodies(os_.body, ns_.body, rel_path, cls_stack, os_, selected)
                    if [_node_dump(x) for x in os_.orelse] != [_node_dump(x) for x in ns_.orelse]:
                        self._diff_fn_bodies(os_.orelse, ns_.orelse, rel_path, cls_stack, os_, selected)
                    continue
                if type(os_) is type(ns_) and isinstance(os_, (ast.For, ast.AsyncFor)) and isinstance(ns_, (ast.For, ast.AsyncFor)):
                    if _node_dump(os_.target) != _node_dump(ns_.target) or _node_dump(os_.iter) != _node_dump(ns_.iter):
                        selected.update(self._lines_cov(rel_path, _header_lines(os_)))
                    else:
                        self._diff_fn_bodies(os_.body, ns_.body, rel_path, cls_stack, os_, selected)
                        self._diff_fn_bodies(os_.orelse, ns_.orelse, rel_path, cls_stack, os_, selected)
                    continue
                if type(os_) is type(ns_) and isinstance(os_, ast.While) and isinstance(ns_, ast.While):
                    if _node_dump(os_.test) != _node_dump(ns_.test):
                        selected.update(self._lines_cov(rel_path, _header_lines(os_)))
                    self._diff_fn_bodies(os_.body, ns_.body, rel_path, cls_stack, os_, selected)
                    self._diff_fn_bodies(os_.orelse, ns_.orelse, rel_path, cls_stack, os_, selected)
                    continue
                if type(os_) is type(ns_) and isinstance(os_, (ast.With, ast.AsyncWith)) and isinstance(ns_, (ast.With, ast.AsyncWith)):
                    if [_node_dump(x) for x in os_.items] != [_node_dump(x) for x in ns_.items]:
                        selected.update(self._lines_cov(rel_path, _header_lines(os_)))
                    else:
                        self._diff_fn_bodies(os_.body, ns_.body, rel_path, cls_stack, os_, selected)
                    continue
                if type(os_) is type(ns_) and isinstance(os_, ast.Try) and isinstance(ns_, ast.Try):
                    self._diff_fn_bodies(os_.body, ns_.body, rel_path, cls_stack, os_, selected)
                    self._diff_fn_bodies(os_.orelse, ns_.orelse, rel_path, cls_stack, os_, selected)
                    self._diff_fn_bodies(os_.finalbody, ns_.finalbody, rel_path, cls_stack, os_, selected)
                    if len(os_.handlers) == len(ns_.handlers):
                        for oh, nh in zip(os_.handlers, ns_.handlers):
                            if _node_dump(oh) != _node_dump(nh):
                                self._diff_fn_bodies(oh.body, nh.body, rel_path, cls_stack, oh, selected)  # type: ignore[arg-type]
                    else:
                        selected.update(self._lines_cov(rel_path, range(os_.lineno, (os_.end_lineno or os_.lineno) + 1)))
                    continue

            if tag in ("replace", "delete"):
                for oi in range(i1, i2):
                    os_ = old_stmts[oi]
                    if _is_docstring_stmt(os_):
                        # Docstring changed -> select callers of the enclosing function!
                        selected.update(self._lines_cov(rel_path, range(old_fn_root.lineno, (old_fn_root.end_lineno or old_fn_root.lineno) + 1)))
                    else:
                        selected.update(self._lines_cov(rel_path, range(os_.lineno, (os_.end_lineno or os_.lineno) + 1)))

            if tag == "insert":
                # Statement(s) inserted into old_stmts at index i1 -> tests hitting adjacent
                # executable statements in this exact straight-line block execute the new line.
                cov_lines: List[int] = []
                for idx in range(i1 - 1, -1, -1):
                    cand = old_stmts[idx]
                    if not _is_docstring_stmt(cand):
                        cov_lines.extend(range(cand.lineno, (cand.end_lineno or cand.lineno) + 1))
                        break
                for idx in range(i1, len(old_stmts)):
                    cand = old_stmts[idx]
                    if not _is_docstring_stmt(cand):
                        cov_lines.extend(range(cand.lineno, (cand.end_lineno or cand.lineno) + 1))
                        break
                if not cov_lines:
                    cov_lines.extend(_header_lines(old_fn_root))
                selected.update(self._lines_cov(rel_path, cov_lines))

    def _diff_scope_stmts(
        self,
        old_stmts: List[ast.stmt],
        new_stmts: List[ast.stmt],
        rel_path: str,
        cls_stack: List[str],
        old_mod: Optional[ast.Module],
        selected: Set[int],
    ) -> None:
        """Diff Module or ClassDef body statement list."""
        osigs = [_stmt_sig(s) for s in old_stmts]
        nsigs = [_stmt_sig(s) for s in new_stmts]
        sm = difflib.SequenceMatcher(None, osigs, nsigs, autojunk=False)

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for oi, nj in zip(range(i1, i2), range(j1, j2)):
                    os_ = old_stmts[oi]
                    ns_ = new_stmts[nj]
                    if _node_dump(os_) == _node_dump(ns_):
                        continue
                    if isinstance(os_, ast.ClassDef) and isinstance(ns_, ast.ClassDef):
                        # Check if bases / decorators / keywords on the ClassDef header itself changed!
                        header_diff = (
                            [_node_dump(x) for x in os_.bases] != [_node_dump(x) for x in ns_.bases]
                            or [_node_dump(x) for x in os_.keywords] != [_node_dump(x) for x in ns_.keywords]
                            or [_node_dump(x) for x in os_.decorator_list] != [_node_dump(x) for x in ns_.decorator_list]
                        )
                        cls_stack.append(os_.name)
                        if header_diff:
                            selected.update(self._class_usage(self._cls_key_candidates(rel_path, cls_stack)))
                        self._diff_scope_stmts(os_.body, ns_.body, rel_path, cls_stack, old_mod, selected)
                        cls_stack.pop()
                    elif isinstance(os_, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                        ns_, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        fn_lines = range(os_.lineno, (os_.end_lineno or os_.lineno) + 1)
                        if _fn_signature_changed(os_, ns_):
                            cov = self._lines_cov(rel_path, fn_lines)
                            selected.update(cov)
                            if cls_stack and not cov:
                                selected.update(self._class_usage(self._cls_key_candidates(rel_path, cls_stack)))
                        else:
                            self._diff_fn_bodies(os_.body, ns_.body, rel_path, cls_stack, os_, selected)
                            # Check if function body edit was covered OR if untested override affects class:
                            # (Do NOT inflate covered methods with class_usage!)
                continue

            # Changed / inserted / deleted statements directly inside Module or ClassDef!
            if cls_stack:
                cls_key = self._cls_key_candidates(rel_path, cls_stack)
                # Special narrow path: Assign/AnnAssign inside ClassDef where ONLY nested Lambda body changed.
                if tag == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1:
                    os_ = old_stmts[i1]
                    ns_ = new_stmts[j1]
                    ov = os_.value if isinstance(os_, (ast.Assign, ast.AnnAssign)) else None
                    nv = ns_.value if isinstance(ns_, (ast.Assign, ast.AnnAssign)) else None
                    same_target = (
                        type(os_) is type(ns_)
                        and isinstance(os_, ast.Assign)
                        and isinstance(ns_, ast.Assign)
                        and [_node_dump(t) for t in os_.targets] == [_node_dump(t) for t in ns_.targets]
                    )
                    if same_target and ov is not None and nv is not None:
                        lambdas = _find_inner_lambdas_only(ov, nv)
                        if lambdas is not None:
                            for lam in lambdas:
                                selected.update(self._lines_cov(rel_path, range(lam.lineno, (lam.end_lineno or lam.lineno) + 1)))
                            continue
                selected.update(self._class_usage(cls_key))
                for oi in range(i1, i2):
                    os_ = old_stmts[oi]
                    selected.update(self._lines_cov(rel_path, range(os_.lineno, (os_.end_lineno or os_.lineno) + 1)))
            else:
                # Top-level Module change!
                if tag == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1:
                    os_ = old_stmts[i1]
                    ns_ = new_stmts[j1]
                    if isinstance(os_, ast.Assign) and isinstance(ns_, ast.Assign):
                        if [_node_dump(t) for t in os_.targets] == [_node_dump(t) for t in ns_.targets]:
                            lambdas = _find_inner_lambdas_only(os_.value, ns_.value)
                            if lambdas is not None:
                                for lam in lambdas:
                                    selected.update(self._lines_cov(rel_path, range(lam.lineno, (lam.end_lineno or lam.lineno) + 1)))
                                continue
                # General module top-level edit: fall back to module-wide tests safely!
                selected.update(self._module_wide_fallback(rel_path, old_mod))
                return

    def _analyze_test_file(
        self,
        rel_path: str,
        hunks: List[Dict[str, Any]],
        selected: Set[int],
    ) -> None:
        old_src, old_mod = self._baseline_src_and_ast(rel_path)
        new_src, ok = apply_hunks_in_memory(old_src, hunks)
        if not ok or old_mod is None:
            selected.update(self.file_to_nums.get(rel_path, ()))
            return
        try:
            new_mod = ast.parse(new_src)
        except SyntaxError:
            selected.update(self.file_to_nums.get(rel_path, ()))
            return

        if _encoding_cookie(old_src) != _encoding_cookie(new_src):
            selected.update(self.file_to_nums.get(rel_path, ()))
            return
        if _node_dump(old_mod) == _node_dump(new_mod):
            selected.update(self._file_access(rel_path))
            return

        # Diff test module top-level -> class -> test_* methods.
        osigs = [_stmt_sig(s) for s in old_mod.body]
        nsigs = [_stmt_sig(s) for s in new_mod.body]
        sm = difflib.SequenceMatcher(None, osigs, nsigs, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                selected.update(self.file_to_nums.get(rel_path, ()))
                return
            for oi, nj in zip(range(i1, i2), range(j1, j2)):
                os_ = old_mod.body[oi]
                ns_ = new_mod.body[nj]
                if _node_dump(os_) == _node_dump(ns_):
                    continue
                if isinstance(os_, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    num = self.func_to_num.get((rel_path, os_.name))
                    if num is not None:
                        selected.add(num)
                    else:
                        selected.update(self.file_to_nums.get(rel_path, ()))
                        return
                elif isinstance(os_, ast.ClassDef) and isinstance(ns_, ast.ClassDef):
                    csigs_o = [_stmt_sig(s) for s in os_.body]
                    csigs_n = [_stmt_sig(s) for s in ns_.body]
                    csm = difflib.SequenceMatcher(None, csigs_o, csigs_n, autojunk=False)
                    for ctag, ci1, ci2, cj1, cj2 in csm.get_opcodes():
                        if ctag == "equal":
                            for coi, cnj in zip(range(ci1, ci2), range(cj1, cj2)):
                                cos = os_.body[coi]
                                cns = ns_.body[cnj]
                                if _node_dump(cos) != _node_dump(cns):
                                    if isinstance(cos, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                        mnum = self.method_to_num.get((rel_path, os_.name, cos.name))
                                        if mnum is not None:
                                            selected.add(mnum)
                                        else:
                                            selected.update(self.class_to_nums.get((rel_path, os_.name), ()))
                                    else:
                                        selected.update(self.class_to_nums.get((rel_path, os_.name), ()))
                        else:
                            for coi in range(ci1, ci2):
                                cos = os_.body[coi]
                                if isinstance(cos, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    mnum = self.method_to_num.get((rel_path, os_.name, cos.name))
                                    if mnum is not None:
                                        selected.add(mnum)
                                        continue
                                selected.update(self.class_to_nums.get((rel_path, os_.name), ()))
                else:
                    selected.update(self.file_to_nums.get(rel_path, ()))
                    return

    def _analyze_py_source(
        self,
        rel_path: str,
        status: str,
        hunks: List[Dict[str, Any]],
        selected: Set[int],
    ) -> None:
        if status == "added":
            selected.update(self._pkg_walker_tests(rel_path))
            return

        old_src, old_mod = self._baseline_src_and_ast(rel_path)
        if status == "deleted" or old_mod is None:
            selected.update(self._module_wide_fallback(rel_path, old_mod))
            return

        new_src, ok = apply_hunks_in_memory(old_src, hunks)
        if not ok:
            selected.update(self._module_wide_fallback(rel_path, old_mod))
            return

        try:
            new_mod = ast.parse(new_src)
        except SyntaxError:
            selected.update(self._module_wide_fallback(rel_path, old_mod))
            return

        if _encoding_cookie(old_src) != _encoding_cookie(new_src):
            selected.update(self._module_wide_fallback(rel_path, old_mod))
            return

        # Pure comment / formatting change (strings & docstrings ARE reflected in AST constants!).
        if _node_dump(old_mod) == _node_dump(new_mod):
            selected.update(self._file_access(rel_path))
            return

        # Always include any tests that literally open/parse this source file (e.g. test_typing reads typing/*.py).
        selected.update(self._file_access(rel_path))
        self._diff_scope_stmts(old_mod.body, new_mod.body, rel_path, [], old_mod, selected)

    def select_commit(self, patch_text: str) -> Set[int]:
        """Select all potentially impacted test numbers for a single commit patch."""
        selected: Set[int] = set()
        sections = parse_git_diff(patch_text)

        for sec in sections:
            rel_path = sec["rel_path"]
            a_path = sec["a_path"]
            status = sec["status"]
            hunks = sec["hunks"]

            if not rel_path:
                continue

            if rel_path.endswith(".py"):
                if rel_path in self.file_to_nums:
                    self._analyze_test_file(rel_path, hunks, selected)
                else:
                    self._analyze_py_source(rel_path, status, hunks, selected)
            else:
                # Non-Python files (fixtures, CSVs, setup configs, docs).
                acc = self._file_access(rel_path) | (self._file_access(a_path) if a_path and a_path != rel_path else set())
                if acc:
                    selected.update(acc)
                    continue

                if rel_path.startswith("tests/"):
                    # Added or untracked fixture file inside tests/ -> match directory / sibling file access.
                    parent = rel_path.rsplit("/", 1)[0] + "/"
                    dir_hits: Set[int] = set()
                    for k, idxs in self.raw_file_access.items():
                        if k.startswith(parent):
                            dir_hits.update(self._remap(idxs))
                    if dir_hits:
                        selected.update(dir_hits)
                    else:
                        selected.update(self.all_nums)
                else:
                    fname = rel_path.rsplit("/", 1)[-1]
                    ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
                    if ext in _INERT_EXTS or fname in _INERT_NAMES or rel_path.startswith(("docs/", ".github/")):
                        continue
                    # Package config / build metadata (e.g. pyproject.toml, setup.py, sqlglotc files).
                    pkg_hits = self._pkg_walker_tests(rel_path)
                    if pkg_hits:
                        selected.update(pkg_hits)
                    elif rel_path.startswith("sqlglot/"):
                        selected.update(self.all_nums)

        return selected


def pack_workers(selected_nums: Set[int], num_to_ms: List[int]) -> List[List[int]]:
    """Optimal LPT + local refinement over k = 1..8 minimizing max(load) + 2000*k."""
    if not selected_nums:
        return []

    ordered = sorted(selected_nums, key=lambda n: num_to_ms[n], reverse=True)
    n = len(ordered)
    max_k = min(MAX_WORKERS, n)

    # Fast exact k=1.
    total_ms = sum(num_to_ms[x] for x in ordered)
    best_cost = total_ms + WORKER_COST_MS
    best_bins: List[List[int]] = [ordered]
    slowest_ms = num_to_ms[ordered[0]]

    for k in range(2, max_k + 1):
        # Lower bound pruning: cannot beat slowest test or balanced average + k*2000.
        lb = max(slowest_ms, (total_ms + k - 1) // k) + WORKER_COST_MS * k
        if lb >= best_cost:
            continue

        bins: List[List[int]] = [[] for _ in range(k)]
        loads = [0] * k
        for tnum in ordered:
            dur = num_to_ms[tnum]
            # Find bin with minimum load.
            min_i = 0
            min_l = loads[0]
            for i in range(1, k):
                li = loads[i]
                if li < min_l:
                    min_l = li
                    min_i = i
            bins[min_i].append(tnum)
            loads[min_i] = min_l + dur

        # Lightweight 1-item move refinement from busiest worker to lightest workers.
        for _ in range(32):
            hi = 0
            hi_l = loads[0]
            lo = 0
            lo_l = loads[0]
            for i in range(1, k):
                li = loads[i]
                if li > hi_l:
                    hi_l = li
                    hi = i
                if li < lo_l:
                    lo_l = li
                    lo = i
            if hi == lo or len(bins[hi]) <= 1:
                break
            improved = False
            gap = hi_l - lo_l
            best_idx = -1
            best_new_max = hi_l
            for pos, tnum in enumerate(bins[hi]):
                w = num_to_ms[tnum]
                if 0 < w < gap:
                    new_max = max(hi_l - w, lo_l + w)
                    if new_max < best_new_max:
                        best_new_max = new_max
                        best_idx = pos
            if best_idx >= 0:
                moved = bins[hi].pop(best_idx)
                w = num_to_ms[moved]
                bins[lo].append(moved)
                loads[hi] -= w
                loads[lo] += w
                improved = True
            if not improved:
                break

        cost = max(loads) + WORKER_COST_MS * k
        if cost < best_cost:
            best_cost = cost
            best_bins = [b for b in bins if b]

    return best_bins


def main() -> None:
    tests_json = json.loads((DATA_DIR / "tests.json").read_text("utf-8"))
    if MAPS_PATH.suffix == ".gz":
        with gzip.open(MAPS_PATH, "rb") as f:
            maps = json.loads(f.read().decode("utf-8"))
    else:
        maps = json.loads(MAPS_PATH.read_text("utf-8"))

    engine = SelectorEngine(tests_json, maps, SQLGLOT_SRC)
    commits_dir = DATA_DIR / "commits"
    patch_files = sorted(commits_dir.glob("*.patch"))

    total_selected = 0
    for patch_path in patch_files:
        patch_text = patch_path.read_text("utf-8", errors="replace")
        sel = engine.select_commit(patch_text)
        workers = pack_workers(sel, engine.num_to_ms)
        total_selected += len(sel)
        sys.stdout.write(json.dumps({"commit": patch_path.stem, "workers": workers}, allow_nan=False) + "\n")
    sys.stdout.flush()
    print(f"selected {total_selected} tests across {len(patch_files)} commits", file=sys.stderr)


if __name__ == "__main__":
    main()
