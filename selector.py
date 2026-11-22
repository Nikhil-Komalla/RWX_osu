#!/usr/bin/env python3
"""Blast Radius AST + coverage + dynamic-dependency test selector & optimal worker packer."""
from __future__ import annotations

import ast
import builtins
import concurrent.futures
import copy
import csv
import difflib
import enum
import gc
import gzip
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import logging
import math
import os
import pickle
import re
import resource
import select
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
import zlib

sys.dont_write_bytecode = True
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple


def _canon_cell(v: Any) -> str:
    if v is None:
        return "N"
    if isinstance(v, bool):
        return "B1" if v else "B0"
    if isinstance(v, int):
        return f"I{v}"
    if isinstance(v, float):
        if math.isnan(v):
            return "Fnan"
        if v == 0.0:
            return "F0.0"
        return f"F{v:.12g}"
    if isinstance(v, str):
        return "S" + repr(v)
    return f"O{type(v).__name__}:{v!r}"


def _canon_digest(obj: Any) -> str:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()[:32]


def _total_cpu_seconds() -> float:
    s = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (s.ru_utime + s.ru_stime) + (c.ru_utime + c.ru_stime)


class _MemSourceLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname: str, path: str, data: Optional[bytes], code_obj: Optional[Any] = None) -> None:
        super().__init__(fullname, path)
        self._data = data
        self._code_obj = code_obj

    def path_stats(self, path: str) -> Any:
        # Force in-memory source execution without reading or writing stale .pyc files.
        raise OSError("in-memory")

    def get_data(self, path: str) -> bytes:
        if path == self.path and self._data is not None:
            return self._data
        return super().get_data(path)

    def get_code(self, fullname: str) -> Any:
        if self._code_obj is not None:
            return self._code_obj
        return super().get_code(fullname)


class PatchOverlayFinder(importlib.abc.MetaPathFinder):
    """In-memory overlay MetaPathFinder for patched/added/deleted sqlglot & test modules."""

    def __init__(
        self,
        base_dir: Path,
        patched_files: Dict[str, bytes],
        deleted_files: Set[str],
        code_cache: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.base_dir = base_dir
        self.patched_files = patched_files
        self.deleted_files = deleted_files
        self.code_cache = code_cache

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if not (
            fullname == "sqlglot"
            or fullname.startswith("sqlglot.")
            or fullname == "tests"
            or fullname.startswith("tests.")
        ):
            return None
        mod_rel = fullname.replace(".", "/")
        for rel, is_pkg in ((mod_rel + "/__init__.py", True), (mod_rel + ".py", False)):
            if rel in self.deleted_files:
                raise ModuleNotFoundError(f"Deleted module {fullname}", name=fullname)
            if rel in self.patched_files:
                vpath = str(self.base_dir / rel)
                loader = _MemSourceLoader(fullname, vpath, self.patched_files[rel])
                spec = importlib.machinery.ModuleSpec(fullname, loader, origin=vpath, is_package=is_pkg)
                spec.has_location = True
                if is_pkg:
                    spec.submodule_search_locations = [str((self.base_dir / rel).parent)]
                return spec
            if self.code_cache and rel in self.code_cache:
                vpath = str(self.base_dir / rel)
                loader = _MemSourceLoader(fullname, vpath, None, self.code_cache[rel])
                spec = importlib.machinery.ModuleSpec(fullname, loader, origin=vpath, is_package=is_pkg)
                spec.has_location = True
                if is_pkg:
                    spec.submodule_search_locations = [str((self.base_dir / rel).parent)]
                return spec
        return None


class _MissingDepError(RuntimeError):
    """Raised inside forked fast-probe child when a test exercises an unavailable third-party dep or disk/subprocess API."""


class _MissingStubObj:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _MissingStubObj(f"{self._name}.{attr}")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise _MissingDepError(f"missing dep called: {self._name}")

    def __enter__(self) -> Any:
        raise _MissingDepError(f"missing dep enter: {self._name}")

    def __exit__(self, *args: Any) -> bool:
        return False

    def __iter__(self) -> Any:
        raise _MissingDepError(f"missing dep iter: {self._name}")

    def __bool__(self) -> bool:
        raise _MissingDepError(f"missing dep bool: {self._name}")

    def __getitem__(self, key: Any) -> Any:
        raise _MissingDepError(f"missing dep getitem: {self._name}")


class _SyncPoolExecutor(concurrent.futures.Executor):
    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        fut: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None


class _DFToken:
    __slots__ = ("sql_s",)

    def __init__(self, sql_s: str) -> None:
        self.sql_s = sql_s


class _ConnVerifier:
    def __init__(self, expected_digests: Tuple[str, ...]) -> None:
        self.total_expected = len(expected_digests)
        self.remaining: Counter[str] = Counter(expected_digests)
        self.calls: List[str] = []
        self.matched = 0

    def execute(self, sql_s: Any, *args: Any, **kwargs: Any) -> Any:
        if args or kwargs or not isinstance(sql_s, str):
            raise _MissingDepError("conn.execute unsupported args")
        self.calls.append(sql_s)
        tok = _DFToken(sql_s)
        return types.SimpleNamespace(df=lambda: tok)

    def verify_pair(self, a: Any, b: Any, **kw: Any) -> None:
        if kw or not isinstance(a, _DFToken) or not isinstance(b, _DFToken):
            raise _MissingDepError("assert_frame_equal unsupported args")
        dig = hashlib.sha256((a.sql_s + "\0" + b.sql_s).encode("utf-8")).hexdigest()[:16]
        if self.remaining.get(dig, 0) <= 0:
            raise _MissingDepError("duckdb sql diverged from baseline")
        self.remaining[dig] -= 1
        self.matched += 1


class _StrictTestResult(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.any_bad = False

    def addError(self, test: Any, err: Any) -> None:
        self.any_bad = True
        super().addError(test, err)

    def addFailure(self, test: Any, err: Any) -> None:
        self.any_bad = True
        super().addFailure(test, err)

    def addSkip(self, test: Any, reason: str) -> None:
        self.any_bad = True
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: Any, err: Any) -> None:
        self.any_bad = True
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: Any) -> None:
        self.any_bad = True
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:
        if err is not None:
            self.any_bad = True
        super().addSubTest(test, subtest, err)


def _install_fast_probe_sandbox() -> None:
    """Install deterministic stubs for optional/disk/subprocess deps inside forked fast-probe child."""
    for root_mod in ("duckdb", "numpy", "pandas", "pandas.testing", "pytz", "dateutil", "dateutil.relativedelta"):
        for existing in [k for k in list(sys.modules) if k == root_mod or k.startswith(root_mod + ".")]:
            del sys.modules[existing]
        m = types.ModuleType(root_mod)
        m.__path__ = []
        m.__getattr__ = lambda attr, _r=root_mod: (  # type: ignore[method-assign]
            (_ for _ in ()).throw(AttributeError(attr))
            if (attr.startswith("__") and attr.endswith("__"))
            else _MissingStubObj(f"{_r}.{attr}")
        )
        sys.modules[root_mod] = m
    sys.modules["dateutil"].relativedelta = sys.modules["dateutil.relativedelta"]  # type: ignore[attr-defined]
    for tf_attr in ("TemporaryDirectory", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "mkstemp", "mkdtemp"):
        if hasattr(tempfile, tf_attr):
            setattr(tempfile, tf_attr, _MissingStubObj(f"tempfile.{tf_attr}"))
    for sp_attr in ("Popen", "run", "call", "check_call", "check_output"):
        if hasattr(subprocess, sp_attr):
            setattr(subprocess, sp_attr, _MissingStubObj(f"subprocess.{sp_attr}"))


DATA_DIR = Path(os.environ.get("BLAST_DATA_DIR", "/data"))
DEFAULT_SRC = Path(__file__).resolve().parent / "sqlglot_src"
if not DEFAULT_SRC.exists():
    alt = Path(__file__).resolve().parent / "_sqlglot_baseline"
    if alt.exists():
        DEFAULT_SRC = alt
SQLGLOT_SRC = Path(os.environ.get("BLAST_SQLGLOT_DIR", str(DEFAULT_SRC)))

DEFAULT_MAPS = Path(__file__).resolve().parent / "maps" / "blast_maps.pkl.zlib"
MAPS_PATH = Path(os.environ.get("BLAST_MAPS_PATH", str(DEFAULT_MAPS)))

TRACE = os.environ.get("BLAST_TRACE") == "1"
DEFAULT_DISABLED: Set[str] = set()
_INIT_DISABLE_ENV = os.environ.get("BLAST_DISABLE", "")
_INIT_ENABLE_ENV = os.environ.get("BLAST_ENABLE", "")
ENABLED = {x.strip() for x in _INIT_ENABLE_ENV.split(",") if x.strip()}
DISABLED = (DEFAULT_DISABLED - ENABLED) | {x.strip() for x in _INIT_DISABLE_ENV.split(",") if x.strip()}

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
_TPCDS_EXEC_CORE_FILES: FrozenSet[str] = frozenset({
    "sqlglot/__init__.py",
    "sqlglot/_typing.py",
    "sqlglot/dialects/dialect.py",
    "sqlglot/errors.py",
    "sqlglot/generators/python.py",
    "sqlglot/helper.py",
    "sqlglot/planner.py",
    "sqlglot/schema.py",
    "sqlglot/time.py",
    "sqlglot/trie.py",
})
_TPCDS_OPT_CORE_FILES: FrozenSet[str] = frozenset({
    "sqlglot/dialects/__init__.py",
    "sqlglot/transforms.py",
})
_TPCDS_FORCE_EXEC_FILES: FrozenSet[str] = frozenset({
    "sqlglot/planner.py",
    "sqlglot/generators/python.py",
})


def _is_leaf_gen_mod(mod_name: str) -> bool:
    """True if dotted module name is a per-dialect/leaf dialect, parser, generator, or typing module."""
    parts = mod_name.split(".")
    if len(parts) != 3 or parts[0] != "sqlglot":
        return False
    _, pkg, stem = parts
    if stem == "__init__":
        return False
    if pkg == "dialects":
        return stem != "dialect"
    if pkg == "parsers":
        return stem != "base"
    return pkg in ("generators", "typing")


def _encoding_cookie(text: str) -> Optional[str]:
    for line in text.splitlines()[:2]:
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


def _ast_eq(a: Any, b: Any) -> bool:
    """Fast structural AST equality without allocating intermediate ast.dump strings."""
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        fields = a._fields
        if fields != b._fields:
            return False
        for f in fields:
            va = getattr(a, f, None)
            vb = getattr(b, f, None)
            if type(va) is list:
                if type(vb) is not list or len(va) != len(vb):
                    return False
                for xa, xb in zip(va, vb):
                    if not _ast_eq(xa, xb):
                        return False
            elif isinstance(va, ast.AST):
                if not _ast_eq(va, vb):
                    return False
            elif va != vb or type(va) is not type(vb):
                return False
        return True
    if type(a) is list:
        if len(a) != len(b):
            return False
        return all(_ast_eq(x, y) for x, y in zip(a, b))
    return a == b and type(a) is type(b)


def _ast_list_eq(xs: Sequence[Any], ys: Sequence[Any]) -> bool:
    if len(xs) != len(ys):
        return False
    for x, y in zip(xs, ys):
        if not _ast_eq(x, y):
            return False
    return True


_UNSAFE_ALPHA_REFS = frozenset({
    "locals",
    "vars",
    "eval",
    "exec",
    "globals",
    "dir",
    "_getframe",
    "currentframe",
    "f_locals",
})


def _collect_all_ids(node: ast.AST, out: Set[str], unsafe_hit: List[bool]) -> None:
    """Collect every identifier mentioned anywhere inside a nested scope, flagging dynamic frame/scope calls."""
    stack: List[ast.AST] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Name):
            out.add(cur.id)
            if cur.id in _UNSAFE_ALPHA_REFS:
                unsafe_hit[0] = True
        elif isinstance(cur, ast.Attribute):
            if cur.attr in _UNSAFE_ALPHA_REFS:
                unsafe_hit[0] = True
        elif isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(cur.name)
            for tp in getattr(cur, "type_params", ()):
                tname = getattr(tp, "name", None)
                if tname:
                    out.add(tname)
        elif isinstance(cur, ast.arg):
            out.add(cur.arg)
        elif isinstance(cur, ast.ExceptHandler):
            if cur.name:
                out.add(cur.name)
        elif isinstance(cur, (ast.Global, ast.Nonlocal)):
            out.update(cur.names)
        elif isinstance(cur, (ast.Import, ast.ImportFrom)):
            for al in cur.names:
                out.add(al.asname or al.name.split(".", 1)[0])
        elif isinstance(cur, (ast.MatchAs, ast.MatchStar)):
            if cur.name:
                out.add(cur.name)
        elif isinstance(cur, ast.MatchMapping):
            if cur.rest:
                out.add(cur.rest)
        stack.extend(ast.iter_child_nodes(cur))


def _renamable_locals(fn: ast.AST) -> frozenset[str]:
    """Return local-only variable names inside fn that can be safely alpha-renamed."""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return frozenset()

    params: Set[str] = set()
    args = fn.args
    for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        params.add(a.arg)
    if args.vararg is not None:
        params.add(args.vararg.arg)
    if args.kwarg is not None:
        params.add(args.kwarg.arg)
    for tp in getattr(fn, "type_params", ()):
        tname = getattr(tp, "name", None)
        if tname:
            params.add(tname)

    stores: Set[str] = set()
    forbidden: Set[str] = set(params) | {"super", "__class__"}
    nested_ids: Set[str] = set()
    unsafe_hit = [False]

    # Defaults and decorators execute in the enclosing scope; flag any dynamic reflection there conservatively.
    for dep in (*fn.decorator_list, *args.defaults, *(d for d in args.kw_defaults if d is not None)):
        _collect_all_ids(dep, set(), unsafe_hit)
    if unsafe_hit[0]:
        return frozenset()

    stack: List[ast.AST] = list(fn.body)
    while stack:
        cur = stack.pop()
        if isinstance(
            cur,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                forbidden.add(cur.name)
            _collect_all_ids(cur, nested_ids, unsafe_hit)
            if unsafe_hit[0]:
                return frozenset()
            continue
        if isinstance(cur, ast.Name):
            if cur.id in _UNSAFE_ALPHA_REFS:
                return frozenset()
            if isinstance(cur.ctx, (ast.Store, ast.Del)):
                stores.add(cur.id)
        elif isinstance(cur, ast.Attribute):
            if cur.attr in _UNSAFE_ALPHA_REFS:
                return frozenset()
        elif isinstance(cur, ast.ExceptHandler):
            if cur.name:
                forbidden.add(cur.name)
        elif isinstance(cur, (ast.Global, ast.Nonlocal)):
            forbidden.update(cur.names)
        elif isinstance(cur, (ast.Import, ast.ImportFrom)):
            for al in cur.names:
                forbidden.add(al.asname or al.name.split(".", 1)[0])
        elif isinstance(cur, (ast.MatchAs, ast.MatchStar)):
            if cur.name:
                forbidden.add(cur.name)
        elif isinstance(cur, ast.MatchMapping):
            if cur.rest:
                forbidden.add(cur.rest)
        stack.extend(ast.iter_child_nodes(cur))

    return frozenset(n for n in stores if n not in forbidden and n not in nested_ids and not n.startswith("__"))


def _has_future_annotations(mod: ast.Module) -> bool:
    for stmt in mod.body:
        if _is_docstring_stmt(stmt):
            continue
        if isinstance(stmt, ast.ImportFrom):
            if stmt.module == "__future__" and any(a.name == "annotations" for a in stmt.names):
                return True
            continue
        break
    return False


def _is_allowed_opswap_operand(node: ast.AST) -> bool:
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
        return True
    if isinstance(node, ast.Constant):
        return node.value is None or type(node.value) is bool
    return False


def _typing_guard_info(mod: ast.Module) -> Tuple[bool, Set[str]]:
    """Detect top-level TYPE_CHECKING and typing module aliases not shadowed at module scope."""
    has_from_tc = False
    aliases: Set[str] = set()
    shadowed: Set[str] = set()
    for stmt in mod.body:
        if isinstance(stmt, ast.ImportFrom):
            if stmt.level == 0 and stmt.module == "typing":
                for al in stmt.names:
                    bound = al.asname or al.name
                    if al.name == "TYPE_CHECKING" and bound == "TYPE_CHECKING":
                        has_from_tc = True
                    else:
                        shadowed.add(bound)
            else:
                for al in stmt.names:
                    shadowed.add(al.asname or al.name)
        elif isinstance(stmt, ast.Import):
            for al in stmt.names:
                bound = al.asname or al.name.split(".", 1)[0]
                if al.name == "typing" and (al.asname or al.name == "typing"):
                    aliases.add(al.asname or "typing")
                else:
                    shadowed.add(bound)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            shadowed.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    shadowed.add(tgt.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            shadowed.add(stmt.target.id)
    if "TYPE_CHECKING" in shadowed:
        has_from_tc = False
    aliases -= shadowed
    return has_from_tc, aliases


def _node_site(node: ast.AST) -> Tuple[int, int, int, int]:
    ln = getattr(node, "lineno", 0) or 0
    col = getattr(node, "col_offset", 0) or 0
    eln = getattr(node, "end_lineno", None) or ln
    ecol = getattr(node, "end_col_offset", None) or col
    return (ln, col, eln, ecol)


def _is_tc_test(test: ast.AST, ctx: "_SemEqCtx") -> bool:
    if isinstance(test, ast.Constant):
        return test.value is False or (type(test.value) is int and test.value == 0)
    if not ctx.frames or ctx.frames[-1] is None:
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING" and ctx.has_from_tc:
            return True
        if (
            isinstance(test, ast.Attribute)
            and isinstance(test.value, ast.Name)
            and test.attr == "TYPE_CHECKING"
            and test.value.id in ctx.typing_aliases
        ):
            return True
    return False


class _SemEqCtx:
    __slots__ = (
        "safe_sites",
        "allow_opswap",
        "allow_alpha",
        "allow_ann",
        "allow_notop",
        "allow_tc",
        "allow_fstr",
        "future_ann",
        "has_from_tc",
        "typing_aliases",
        "old_lines",
        "new_lines",
        "frames",
        "scope_kind",
        "kinds",
    )

    def __init__(
        self,
        safe_sites: Any = frozenset(),
        allow_opswap: bool = True,
        allow_alpha: bool = True,
        allow_ann: bool = True,
        allow_notop: bool = True,
        allow_tc: bool = True,
        allow_fstr: bool = True,
        future_ann: bool = False,
        has_from_tc: bool = False,
        typing_aliases: FrozenSet[str] = frozenset(),
        old_lines: Optional[List[str]] = None,
        new_lines: Optional[List[str]] = None,
    ) -> None:
        self.safe_sites = safe_sites
        self.allow_opswap = allow_opswap
        self.allow_alpha = allow_alpha
        self.allow_ann = allow_ann
        self.allow_notop = allow_notop
        self.allow_tc = allow_tc
        self.allow_fstr = allow_fstr
        self.future_ann = future_ann
        self.has_from_tc = has_from_tc
        self.typing_aliases = typing_aliases
        self.old_lines = old_lines
        self.new_lines = new_lines
        self.frames: List[Optional[Tuple[frozenset[str], frozenset[str], Dict[str, str], Dict[str, str]]]] = []
        self.scope_kind: str = "module"
        self.kinds: Set[str] = set()


def _sem_eq_args(oa: ast.arguments, na: ast.arguments, ignore_ann: bool, ctx: _SemEqCtx) -> bool:
    for grp in ("posonlyargs", "args", "kwonlyargs"):
        xs = getattr(oa, grp)
        ys = getattr(na, grp)
        if len(xs) != len(ys):
            return False
        for a1, a2 in zip(xs, ys):
            if a1.arg != a2.arg or getattr(a1, "type_comment", None) != getattr(a2, "type_comment", None):
                return False
            if ignore_ann:
                if not _ast_eq(a1.annotation, a2.annotation):
                    ctx.kinds.add("annotation")
            elif not _sem_eq(a1.annotation, a2.annotation, ctx):
                return False
    for grp in ("vararg", "kwarg"):
        a1 = getattr(oa, grp)
        a2 = getattr(na, grp)
        if (a1 is None) != (a2 is None):
            return False
        if a1 is not None:
            if a1.arg != a2.arg or getattr(a1, "type_comment", None) != getattr(a2, "type_comment", None):
                return False
            if ignore_ann:
                if not _ast_eq(a1.annotation, a2.annotation):
                    ctx.kinds.add("annotation")
            elif not _sem_eq(a1.annotation, a2.annotation, ctx):
                return False
    if not _sem_eq(oa.defaults, na.defaults, ctx):
        return False
    if not _sem_eq(oa.kw_defaults, na.kw_defaults, ctx):
        return False
    return True


def _sem_eq(a: Any, b: Any, ctx: _SemEqCtx) -> bool:
    """Bounded semantic-equivalence AST comparator for verified-safe no-op edits."""
    if a is b:
        return True

    if ctx.allow_notop:
        if isinstance(a, ast.UnaryOp) and isinstance(a.op, ast.Not) and isinstance(b, ast.Compare):
            ua, cb = a.operand, b
        elif isinstance(b, ast.UnaryOp) and isinstance(b.op, ast.Not) and isinstance(a, ast.Compare):
            ua, cb = a, b.operand
        else:
            ua, cb = None, None
        if ua is not None and isinstance(ua, ast.Compare) and isinstance(cb, ast.Compare):
            if len(ua.ops) == 1 and len(cb.ops) == 1 and len(ua.comparators) == 1 and len(cb.comparators) == 1:
                op1, op2 = type(ua.ops[0]), type(cb.ops[0])
                pair = {op1, op2}
                if op1 is not op2 and pair in ({ast.Is, ast.IsNot}, {ast.In, ast.NotIn}):
                    if _sem_eq(ua.left, cb.left, ctx) and _sem_eq(ua.comparators[0], cb.comparators[0], ctx):
                        ctx.kinds.add("notop")
                        return True
                    return False
                if op1 is not op2 and pair == {ast.Eq, ast.NotEq}:
                    if (
                        ctx.allow_opswap
                        and _node_site(ua) in ctx.safe_sites
                        and _sem_eq(ua.left, cb.left, ctx)
                        and _sem_eq(ua.comparators[0], cb.comparators[0], ctx)
                    ):
                        ctx.kinds.add("notop")
                        return True
                    return False

    if ctx.allow_fstr and type(a) is not type(b):
        sa = _plain_fstr_value(a)
        if sa is not None:
            sb = _plain_fstr_value(b)
            if sb is not None and sa == sb:
                ctx.kinds.add("fstring")
                return True

    if type(a) is not type(b):
        return False

    if (
        ctx.old_lines is not None
        and ctx.new_lines is not None
        and (not ctx.frames or ctx.frames[-1] is None)
        and isinstance(a, ast.stmt)
        and getattr(a, "col_offset", -1) == getattr(b, "col_offset", -2)
    ):
        o0, o1 = _stmt_span(a)
        n0, n1 = _stmt_span(b)
        if (o1 - o0) == (n1 - n0) and 1 <= o0 <= o1 <= len(ctx.old_lines) and 1 <= n0 <= n1 <= len(ctx.new_lines):
            if ctx.old_lines[o0 - 1 : o1] == ctx.new_lines[n0 - 1 : n1]:
                return True

    if isinstance(a, ast.Compare):
        if (
            ctx.allow_opswap
            and len(a.ops) == 1
            and len(b.ops) == 1
            and len(a.comparators) == 1
            and len(b.comparators) == 1
            and type(a.ops[0]) is not type(b.ops[0])
            and {type(a.ops[0]), type(b.ops[0])} in ({ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq})
        ):
            site = (
                getattr(a, "lineno", 0) or 0,
                getattr(a, "col_offset", 0) or 0,
                getattr(a, "end_lineno", None) or (getattr(a, "lineno", 0) or 0),
                getattr(a, "end_col_offset", None) or (getattr(a, "col_offset", 0) or 0),
            )
            if (
                site in ctx.safe_sites
                and _is_allowed_opswap_operand(a.left)
                and _is_allowed_opswap_operand(a.comparators[0])
                and _is_allowed_opswap_operand(b.left)
                and _is_allowed_opswap_operand(b.comparators[0])
                and _sem_eq(a.left, b.left, ctx)
                and _sem_eq(a.comparators[0], b.comparators[0], ctx)
            ):
                ctx.kinds.add("opswap")
                return True
            return False

    elif isinstance(a, ast.Name):
        if type(a.ctx) is not type(b.ctx):
            return False
        frame = ctx.frames[-1] if ctx.frames else None
        if frame is None or not ctx.allow_alpha:
            return a.id == b.id
        ra, rb, a2b, b2a = frame
        in_a = a.id in ra
        in_b = b.id in rb
        if not in_a and not in_b:
            return a.id == b.id
        if in_a and in_b:
            mapped = a2b.get(a.id)
            if mapped is not None:
                if mapped != b.id:
                    return False
            elif b.id in b2a:
                return False
            else:
                a2b[a.id] = b.id
                b2a[b.id] = a.id
            if a.id != b.id:
                ctx.kinds.add("alpha")
            return True
        return False

    elif isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if a.name != b.name or getattr(a, "type_comment", None) != getattr(b, "type_comment", None):
            return False
        if not _sem_eq(a.decorator_list, b.decorator_list, ctx):
            return False
        ignore_sig_ann = bool(ctx.allow_ann and ctx.future_ann and not a.decorator_list and not b.decorator_list)
        if not _sem_eq_args(a.args, b.args, ignore_sig_ann, ctx):
            return False
        if ignore_sig_ann:
            if not _ast_eq(a.returns, b.returns):
                ctx.kinds.add("annotation")
        elif not _sem_eq(a.returns, b.returns, ctx):
            return False
        if not _sem_eq(getattr(a, "type_params", []), getattr(b, "type_params", []), ctx):
            return False
        if (
            ctx.old_lines is not None
            and ctx.new_lines is not None
            and len(a.body) == len(b.body)
            and a.body
            and (not ctx.frames or ctx.frames[-1] is None)
        ):
            b0 = _stmt_span(a.body[0])[0]
            b1 = _stmt_span(a.body[-1])[1]
            nb0 = _stmt_span(b.body[0])[0]
            nb1 = _stmt_span(b.body[-1])[1]
            if (b1 - b0) == (nb1 - nb0) and 1 <= b0 <= b1 <= len(ctx.old_lines) and 1 <= nb0 <= nb1 <= len(ctx.new_lines):
                if ctx.old_lines[b0 - 1 : b1] == ctx.new_lines[nb0 - 1 : nb1]:
                    return True
        frame = (_renamable_locals(a), _renamable_locals(b), {}, {}) if ctx.allow_alpha else None
        prev_scope = ctx.scope_kind
        ctx.frames.append(frame)
        ctx.scope_kind = "function"
        try:
            return _sem_eq(a.body, b.body, ctx)
        finally:
            ctx.scope_kind = prev_scope
            ctx.frames.pop()

    elif isinstance(a, ast.ClassDef):
        if a.name != b.name:
            return False
        if not (
            _sem_eq(a.decorator_list, b.decorator_list, ctx)
            and _sem_eq(a.bases, b.bases, ctx)
            and _sem_eq(a.keywords, b.keywords, ctx)
            and _sem_eq(getattr(a, "type_params", []), getattr(b, "type_params", []), ctx)
        ):
            return False
        prev_scope = ctx.scope_kind
        ctx.frames.append(None)
        ctx.scope_kind = "class"
        try:
            return _sem_eq(a.body, b.body, ctx)
        finally:
            ctx.scope_kind = prev_scope
            ctx.frames.pop()

    elif isinstance(a, ast.Lambda):
        if not _sem_eq_args(a.args, b.args, False, ctx):
            return False
        ctx.frames.append(None)
        try:
            return _sem_eq(a.body, b.body, ctx)
        finally:
            ctx.frames.pop()

    elif isinstance(a, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        ctx.frames.append(None)
        try:
            for f in a._fields:
                if not _sem_eq(getattr(a, f, None), getattr(b, f, None), ctx):
                    return False
            return True
        finally:
            ctx.frames.pop()

    elif isinstance(a, ast.If):
        if ctx.allow_tc and _ast_eq(a.test, b.test) and _is_tc_test(a.test, ctx):
            if not _sem_eq(a.orelse, b.orelse, ctx):
                return False
            if not _ast_eq(a.body, b.body):
                ctx.kinds.add("type_checking")
            return True

    elif isinstance(a, ast.AnnAssign):
        if ctx.allow_ann and ctx.scope_kind == "function" and a.simple and b.simple and isinstance(a.target, ast.Name) and isinstance(b.target, ast.Name):
            if not (_sem_eq(a.target, b.target, ctx) and _sem_eq(a.value, b.value, ctx)):
                return False
            if not _ast_eq(a.annotation, b.annotation):
                ctx.kinds.add("annotation")
            return True

    if isinstance(a, ast.AST):
        if isinstance(a, ast.Expr) and isinstance(b, ast.Expr):
            va, vb = a.value, b.value
            if (isinstance(va, ast.Constant) and isinstance(va.value, str) and isinstance(vb, ast.JoinedStr)) or (
                isinstance(vb, ast.Constant) and isinstance(vb.value, str) and isinstance(va, ast.JoinedStr)
            ):
                return False
        fields = a._fields
        if fields != b._fields:
            return False
        for f in fields:
            if not _sem_eq(getattr(a, f, None), getattr(b, f, None), ctx):
                return False
        return True
    if type(a) is list:
        if len(a) != len(b):
            return False
        return all(_sem_eq(x, y, ctx) for x, y in zip(a, b))
    return a == b and type(a) is type(b)


_DANGEROUS_REMOVED_DECORATORS = frozenset({
    "property",
    "cached_property",
    "classmethod",
    "staticmethod",
    "abstractmethod",
    "dataclass",
    "setter",
    "getter",
    "deleter",
    "wraps",
})


def _decorator_terminal_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _diff_touches_dunder_eq(raw_diff: str) -> bool:
    for line in raw_diff.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (
            line.startswith("-") and not line.startswith("---")
        ):
            if "__eq__" in line or "__ne__" in line or "__hash__" in line:
                return True
    return False


def _stmt_span(node: ast.AST) -> Tuple[int, int]:
    start = getattr(node, "lineno", 1) or 1
    decs = getattr(node, "decorator_list", None)
    if decs:
        for d in decs:
            dl = getattr(d, "lineno", None)
            if dl is not None and dl < start:
                start = dl
    end = getattr(node, "end_lineno", None) or start
    return start, end


def _stmt_sig(node: ast.stmt, lines: Optional[List[str]] = None) -> Tuple[str, Any]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ("fn", node.name)
    if isinstance(node, ast.ClassDef):
        return ("cls", node.name)
    if lines:
        s0, s1 = _stmt_span(node)
        if 1 <= s0 <= s1 <= len(lines):
            return ("raw", tuple(lines[s0 - 1 : s1]))
    return ("st", ast.dump(node, include_attributes=False))


def _node_same_fast(
    os_: ast.AST,
    ns_: ast.AST,
    old_lines: Optional[List[str]],
    new_lines: Optional[List[str]],
) -> bool:
    if type(os_) is not type(ns_):
        return False
    if old_lines and new_lines:
        o0, o1 = _stmt_span(os_)
        n0, n1 = _stmt_span(ns_)
        if (o1 - o0) == (n1 - n0) and 1 <= o0 <= o1 <= len(old_lines) and 1 <= n0 <= n1 <= len(new_lines):
            if old_lines[o0 - 1 : o1] == new_lines[n0 - 1 : n1]:
                return True
    return _ast_eq(os_, ns_)


def _fn_signature_changed(old_fn: ast.AST, new_fn: ast.AST) -> bool:
    """True if function kind, name, decorators, args, returns, or type_params changed."""
    if type(old_fn) is not type(new_fn):
        return True
    assert isinstance(old_fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    assert isinstance(new_fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    if old_fn.name != new_fn.name:
        return True
    if not _ast_eq(old_fn.args, new_fn.args):
        return True
    if not _ast_list_eq(old_fn.decorator_list, new_fn.decorator_list):
        return True
    if (old_fn.returns is None) != (new_fn.returns is None):
        return True
    if old_fn.returns is not None and not _ast_eq(old_fn.returns, new_fn.returns):
        return True
    if not _ast_list_eq(getattr(old_fn, "type_params", []), getattr(new_fn, "type_params", [])):
        return True
    return False


def _fn_scope_shape(fn_node: ast.AST) -> Tuple[frozenset[str], bool]:
    """Collect local variable bindings & generator yield presence in a function scope."""
    bound: Set[str] = set()
    has_yield = False
    stack: List[ast.AST] = list(getattr(fn_node, "body", ()))

    def _collect_target_names(tgt: ast.AST) -> None:
        if isinstance(tgt, ast.Name):
            bound.add(tgt.id)
        elif isinstance(tgt, (ast.Tuple, ast.List)):
            for elt in tgt.elts:
                _collect_target_names(elt)
        elif isinstance(tgt, ast.Starred):
            _collect_target_names(tgt.value)

    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(cur, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # Outer-most generator's first iter runs in enclosing scope; walrus targets bind in enclosing scope.
            if cur.generators:
                stack.append(cur.generators[0].iter)
            comp_stack: List[ast.AST] = []
            for gen in cur.generators[1:]:
                comp_stack.append(gen.iter)
            for gen in cur.generators:
                comp_stack.extend(gen.ifs)
            if isinstance(cur, ast.DictComp):
                comp_stack.extend((cur.key, cur.value))
            else:
                comp_stack.append(cur.elt)
            while comp_stack:
                cn = comp_stack.pop()
                if isinstance(cn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                if isinstance(cn, ast.NamedExpr):
                    bound.add(cn.target.id)
                comp_stack.extend(ast.iter_child_nodes(cn))
            continue
        if isinstance(cur, (ast.Yield, ast.YieldFrom)):
            has_yield = True
        elif isinstance(cur, ast.Name) and isinstance(cur.ctx, (ast.Store, ast.Del)):
            bound.add(cur.id)
        elif isinstance(cur, ast.ExceptHandler):
            if cur.name:
                bound.add(cur.name)
        elif isinstance(cur, (ast.Global, ast.Nonlocal)):
            bound.update(cur.names)
        elif isinstance(cur, (ast.Import, ast.ImportFrom)):
            for alias in cur.names:
                bound.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(cur, ast.NamedExpr):
            bound.add(cur.target.id)
        elif isinstance(cur, (ast.MatchAs, ast.MatchStar)):
            if cur.name:
                bound.add(cur.name)
        elif isinstance(cur, ast.MatchMapping):
            if cur.rest:
                bound.add(cur.rest)
        elif isinstance(cur, ast.AnnAssign) and cur.simple and isinstance(cur.target, ast.Name):
            bound.add(cur.target.id)
        elif isinstance(cur, ast.Delete):
            for t in cur.targets:
                _collect_target_names(t)
        stack.extend(ast.iter_child_nodes(cur))

    return frozenset(bound), has_yield


def _header_lines(stmt: ast.stmt) -> range:
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
    return range(start, end + 1)


def _find_inner_lambdas_only(old_expr: ast.AST, new_expr: ast.AST) -> Optional[List[ast.Lambda]]:
    """If old_expr -> new_expr differs ONLY inside nested Lambda bodies, return those old Lambdas."""
    if type(old_expr) is not type(new_expr):
        return None
    if isinstance(old_expr, ast.Lambda) and isinstance(new_expr, ast.Lambda):
        if _ast_eq(old_expr.args, new_expr.args):
            return [old_expr]
        return None
    if _ast_eq(old_expr, new_expr):
        return []
    if isinstance(old_expr, ast.Dict) and isinstance(new_expr, ast.Dict):
        if len(old_expr.keys) != len(new_expr.keys):
            return None
        out: List[ast.Lambda] = []
        for ok, ov, nk, nv in zip(old_expr.keys, old_expr.values, new_expr.keys, new_expr.values):
            if (ok is None) != (nk is None):
                return None
            if ok is not None and not _ast_eq(ok, nk):
                return None
            if not _ast_eq(ov, nv):
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
            if not _ast_eq(oe, ne):
                sub = _find_inner_lambdas_only(oe, ne)
                if sub is None:
                    return None
                out.extend(sub)
        return out
    if isinstance(old_expr, ast.Call) and isinstance(new_expr, ast.Call):
        if not _ast_eq(old_expr.func, new_expr.func):
            return None
        if len(old_expr.args) != len(new_expr.args) or len(old_expr.keywords) != len(new_expr.keywords):
            return None
        out = []
        for oa, na in zip(old_expr.args, new_expr.args):
            if not _ast_eq(oa, na):
                sub = _find_inner_lambdas_only(oa, na)
                if sub is None:
                    return None
                out.extend(sub)
        for okw, nkw in zip(old_expr.keywords, new_expr.keywords):
            if okw.arg != nkw.arg:
                return None
            if not _ast_eq(okw.value, nkw.value):
                sub = _find_inner_lambdas_only(okw.value, nkw.value)
                if sub is None:
                    return None
                out.extend(sub)
        return out
    return None


def _ast_canon_dict_key(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, str):
            return f"str:{v}"
        if isinstance(v, int) and not isinstance(v, bool):
            return f"int:{v}"
        return None
    if isinstance(node, ast.Tuple):
        parts: List[str] = []
        for elt in node.elts:
            ce = _ast_canon_dict_key(elt)
            if ce is None:
                return None
            parts.append(ce)
        return "tuple:(" + ",".join(parts) + ")"
    if isinstance(node, ast.Attribute):
        v = node.value
        if isinstance(v, ast.Name):
            if v.id == "exp":
                return f"cls:{node.attr}"
            if v.id in ("TokenType", "DType"):
                return f"enum:{v.id}.{node.attr}"
        elif isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name):
            if v.value.id == "exp" and v.attr == "DType":
                return f"enum:DType.{node.attr}"
    return None


def _diff_ast_dict(old_dict: ast.Dict, new_dict: ast.Dict) -> Optional[Tuple[List[str], List[str]]]:
    """Return (added_or_deleted_keys, modified_existing_keys) for static dict literals, or None if unsafe."""
    old_splats: List[ast.AST] = []
    new_splats: List[ast.AST] = []
    old_map: Dict[str, ast.AST] = {}
    new_map: Dict[str, ast.AST] = {}

    for k, val in zip(old_dict.keys, old_dict.values):
        if k is None:
            old_splats.append(val)
        else:
            ck = _ast_canon_dict_key(k)
            if ck is None or ck in old_map:
                return None
            old_map[ck] = val

    for k, val in zip(new_dict.keys, new_dict.values):
        if k is None:
            new_splats.append(val)
        else:
            ck = _ast_canon_dict_key(k)
            if ck is None or ck in new_map:
                return None
            new_map[ck] = val

    if not _ast_list_eq(old_splats, new_splats):
        return None

    added_or_deleted = sorted(old_map.keys() ^ new_map.keys())
    modified_existing = [k for k in sorted(old_map.keys() & new_map.keys()) if not _ast_eq(old_map[k], new_map[k])]
    return added_or_deleted, modified_existing


def _is_simple_scalar_ast(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert, ast.Not)):
        return isinstance(node.operand, ast.Constant)
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _is_simple_scalar_ast(node.value)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_simple_scalar_ast(e) for e in node.elts)
    return False


_BUILTIN_NAMES = frozenset(dir(builtins))
_BASE_ATTR_NAMES = frozenset().union(
    *(
        dir(t)
        for t in (
            object,
            int,
            str,
            bytes,
            dict,
            list,
            tuple,
            set,
            frozenset,
            type,
            Exception,
            enum.Enum,
            enum.IntEnum,
        )
    )
)
_SAFE_STDLIB = frozenset({
    "abc",
    "array",
    "ast",
    "base64",
    "binascii",
    "bisect",
    "calendar",
    "codecs",
    "collections",
    "contextlib",
    "copy",
    "csv",
    "dataclasses",
    "datetime",
    "decimal",
    "difflib",
    "enum",
    "fnmatch",
    "fractions",
    "functools",
    "glob",
    "gzip",
    "hashlib",
    "heapq",
    "inspect",
    "io",
    "itertools",
    "json",
    "keyword",
    "locale",
    "logging",
    "math",
    "numbers",
    "operator",
    "os",
    "pathlib",
    "pickle",
    "pprint",
    "queue",
    "random",
    "re",
    "shlex",
    "statistics",
    "string",
    "struct",
    "sys",
    "tempfile",
    "textwrap",
    "threading",
    "time",
    "traceback",
    "types",
    "typing",
    "unicodedata",
    "uuid",
    "warnings",
    "weakref",
    "zlib",
    "zoneinfo",
})
_DYN_MARKERS = (
    "globals()",
    "__getattr__",
    "getmembers(",
    "vars(",
    "__dict__",
    "sys.modules[__name__]",
    "__all__",
)
_WORD_RE = re.compile(r"\w+")


def _is_pure_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not, ast.Invert)):
        return _is_pure_literal(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_pure_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_pure_literal(k) for k in node.keys) and all(
            _is_pure_literal(v) for v in node.values
        )
    return False


def _deftime_safe(fn: ast.AST, future_ann: bool) -> bool:
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if fn.decorator_list or getattr(fn, "type_params", []):
        return False
    args = fn.args
    for d in args.defaults:
        if not _is_pure_literal(d):
            return False
    for d in args.kw_defaults:
        if d is not None and not _is_pure_literal(d):
            return False
    if not future_ann:
        for grp in (args.posonlyargs, args.args, args.kwonlyargs):
            for a in grp:
                if a.annotation is not None:
                    return False
        for a in (args.vararg, args.kwarg):
            if a is not None and a.annotation is not None:
                return False
        if fn.returns is not None:
            return False
    return True


def _plain_fstr_value(node: Any) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                return None
        return "".join(parts)
    return None


def _bound_import_names(st: ast.stmt) -> List[str]:
    out: List[str] = []
    if isinstance(st, ast.Import):
        for alias in st.names:
            out.append(alias.asname or alias.name.split(".", 1)[0])
    elif isinstance(st, ast.ImportFrom):
        for alias in st.names:
            out.append(alias.asname or alias.name)
    return out


def _extract_single_assign(stmt: ast.stmt) -> Optional[Tuple[str, ast.AST]]:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return stmt.target.id, stmt.value
    return None


def _count_target_assigns(stmts: Sequence[ast.stmt]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for st in stmts:
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    counts[t.id] += 1
        elif isinstance(st, (ast.AnnAssign, ast.AugAssign)) and isinstance(st.target, ast.Name):
            counts[st.target.id] += 1
    return counts


def _top_level_names(stmts: Sequence[ast.stmt]) -> Set[str]:
    names: Set[str] = set()

    def _collect_tgt(t: ast.AST) -> None:
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                _collect_tgt(e)
        elif isinstance(t, ast.Starred):
            _collect_tgt(t.value)

    for st in stmts:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(st.name)
        elif isinstance(st, ast.Assign):
            for t in st.targets:
                _collect_tgt(t)
        elif isinstance(st, ast.AnnAssign):
            _collect_tgt(st.target)
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            for alias in st.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name.split(".", 1)[0])
    return names


def _resolve_local_mod_path(
    mod_name: str,
    level: int,
    rel_path: str,
    src_root: Path,
    added_paths: Set[str],
    deleted_paths: Set[str],
) -> Optional[str]:
    if level > 0:
        parts = rel_path.removesuffix(".py").split("/")
        pkg_parts = parts[:-1]
        up = level - 1
        base = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else []
        full = ".".join(base + ([mod_name] if mod_name else []))
    else:
        full = mod_name
    if not (full == "sqlglot" or full.startswith("sqlglot.") or full == "tests" or full.startswith("tests.")):
        return None
    mparts = full.split(".")
    cand1 = "/".join(mparts) + ".py"
    cand2 = "/".join(mparts) + "/__init__.py"
    for cand in (cand1, cand2):
        if cand in added_paths:
            return cand
        if cand not in deleted_paths and (src_root / cand).is_file():
            return cand
    return "__MISSING__"


def _has_broken_import(
    old_stmts: Sequence[ast.stmt],
    new_stmts: Sequence[ast.stmt],
    rel_path: str,
    src_root: Path,
    added_paths: Set[str],
    deleted_paths: Set[str],
) -> bool:
    """Detect added/changed local imports pointing to missing modules."""
    old_imports = [st for st in old_stmts if isinstance(st, (ast.Import, ast.ImportFrom))]
    for st in new_stmts:
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            if any(_ast_eq(st, oi) for oi in old_imports):
                continue
            if isinstance(st, ast.Import):
                for alias in st.names:
                    res = _resolve_local_mod_path(alias.name, 0, rel_path, src_root, added_paths, deleted_paths)
                    if res == "__MISSING__":
                        return True
            else:
                res = _resolve_local_mod_path(st.module or "", st.level, rel_path, src_root, added_paths, deleted_paths)
                if res == "__MISSING__":
                    return True
    return False


def parse_git_diff(patch_text: str) -> List[Dict[str, Any]]:
    """Split unified git diff into per-file sections with hunks."""
    files: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    cur_hunk: Optional[Dict[str, Any]] = None
    cur_raw: List[str] = []

    lines = patch_text.splitlines(keepends=True)
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in lines:
        sline = line.rstrip("\r\n")
        if sline.startswith("diff --git "):
            if cur is not None:
                cur["raw_diff"] = "".join(cur_raw)
                files.append(cur)
            cur_raw = [line]
            parts = sline.split()
            a_path = parts[2][2:] if len(parts) >= 4 and parts[2].startswith("a/") else ""
            b_path = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            cur = {
                "a_path": a_path,
                "b_path": b_path,
                "rel_path": b_path or a_path,
                "status": "modified",
                "renamed": False,
                "binary": False,
                "hunks": [],
            }
            cur_hunk = None
            continue
        if cur is None:
            continue
        cur_raw.append(line)
        if cur_hunk is None:
            if sline.startswith("Binary files ") or sline == "GIT binary patch":
                cur["binary"] = True
                continue
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
                cur["renamed"] = True
                continue
            if sline.startswith("rename to "):
                cur["b_path"] = sline[10:].strip()
                cur["rel_path"] = cur["b_path"]
                cur["renamed"] = True
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
        cur["raw_diff"] = "".join(cur_raw)
        files.append(cur)
    return files


def apply_hunks_in_memory(old_text: str, hunks: List[Dict[str, Any]]) -> Tuple[str, bool]:
    """Apply parsed unified hunks to old_text in memory. Returns (new_text, ok)."""
    if not hunks:
        return old_text, True
    old_lines = old_text.splitlines(keepends=True)
    out: List[str] = []
    pos = 0

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
    """Precomputed-map + AST-diff test selector using bitmask set algebra."""

    def __init__(self, tests_json: Dict[str, Any], maps: Dict[str, Any], src_root: Path) -> None:
        self.src_root = src_root
        tests = tests_json["tests"]

        self.id_to_num: Dict[str, int] = {}
        self.num_to_id: List[str] = [""] * len(tests)
        self.num_to_parts: List[Optional[Tuple[str, str, str]]] = [None] * len(tests)
        self.num_to_ms: List[int] = [0] * len(tests)
        self.file_to_nums: Dict[str, List[int]] = defaultdict(list)
        self.class_to_nums: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        self.method_to_num: Dict[Tuple[str, str, str], int] = {}
        self.func_to_num: Dict[Tuple[str, str], int] = {}
        self.all_nums: Set[int] = set()

        self.file_to_livemask: Dict[str, int] = defaultdict(int)
        self.class_to_livemask: Dict[Tuple[str, str], int] = defaultdict(int)
        self.all_livemask: int = 0

        for t in tests:
            num = int(t["number"])
            tid = t["id"]
            tfile = t["file"]
            ms = math.floor(float(t["seconds"]) * 1000.0 + 0.5)
            bit = 1 << num
            self.id_to_num[tid] = num
            self.num_to_id[num] = tid
            self.num_to_ms[num] = ms
            self.file_to_nums[tfile].append(num)
            self.file_to_livemask[tfile] |= bit
            self.all_nums.add(num)
            self.all_livemask |= bit
            parts = tid.split("::")
            if len(parts) == 3:
                self.num_to_parts[num] = (tfile, parts[1], parts[2])
                self.class_to_nums[(tfile, parts[1])].append(num)
                self.class_to_livemask[(tfile, parts[1])] |= bit
                self.method_to_num[(tfile, parts[1], parts[2])] = num
            elif len(parts) == 2:
                self.func_to_num[(tfile, parts[1])] = num

        test_ids: List[str] = maps["test_ids"]
        self.idx_to_num: List[Optional[int]] = [self.id_to_num.get(tid) for tid in test_ids]
        self.id_to_base_idx: Dict[str, int] = {tid: i for i, tid in enumerate(test_ids)}

        self.file_to_basemask: Dict[str, int] = {}
        for tfile, nums in self.file_to_nums.items():
            bm = 0
            for t in tests:
                if t["file"] == tfile:
                    bidx = self.id_to_base_idx.get(t["id"])
                    if bidx is not None:
                        bm |= 1 << bidx
            self.file_to_basemask[tfile] = bm

        self.is_v2 = int(maps.get("version", 1)) >= 2
        self.raw_line_cov: Dict[str, Dict[Any, Any]] = maps.get("line_cov", {})
        self.raw_file_access: Dict[str, Any] = maps.get("file_access", {})
        self.raw_dir_access: Dict[str, Any] = maps.get("dir_access", {})
        self.raw_class_usage: Dict[str, Any] = maps.get("class_usage", {})
        self.raw_pkg_walkers: Dict[str, Any] = maps.get("pkg_walkers", {})
        self.raw_dict_key_usage: Dict[str, Dict[str, Dict[str, Any]]] = maps.get("dict_key_usage", {})
        self.tracked_dict_classes: Set[str] = set(maps.get("tracked_dict_classes", ()))
        self.rev_imports: Dict[str, List[str]] = maps.get("rev_imports", {})
        self.dialect_typing_links: Dict[str, List[str]] = maps.get("dialect_typing_links", {})
        self.attr_read_lines: Dict[str, Dict[str, List[int]]] = maps.get("attr_read_lines", {})
        self.attr_import_test_files: Dict[str, List[str]] = maps.get("attr_import_test_files", {})
        self.metaclass_attrs: Set[str] = set(maps.get("metaclass_attrs", ()))
        self.raw_once_lines: Dict[str, List[int]] = maps.get("once_lines", {})
        raw_col = maps.get("collection_lines", {})
        self.collection_lines: Dict[str, Dict[int, List[str]]] = {
            rp: {int(ln): list(tfs) for ln, tfs in lmap.items()} for rp, lmap in raw_col.items()
        }
        raw_trig = maps.get("import_trigger_lines", {})
        self.import_trigger_lines: Dict[str, Dict[int, List[str]]] = {
            rp: {int(ln): list(trigs) for ln, trigs in lmap.items()} for rp, lmap in raw_trig.items()
        }
        self.dict_value_import_reads: FrozenSet[str] = frozenset(maps.get("dict_value_import_reads", ()))
        self.str_const_idents: FrozenSet[str] = frozenset(maps.get("str_const_idents", ()))
        self.known_words: FrozenSet[str] = frozenset(maps.get("known_words", ()))
        self.dict_key_mismatch_attrs: FrozenSet[str] = frozenset(maps.get("dict_key_mismatch_attrs", ()))
        self.path_mentions: Dict[str, List[str]] = maps.get("path_mentions", {})
        self.lazy_test_importers: Dict[str, List[str]] = maps.get("lazy_test_importers", {})
        raw_eq_safe = maps.get("eq_safe_sites", {})
        self.eq_safe_sites: Dict[str, frozenset[Tuple[int, int, int, int]]] = {
            rp: (coords if isinstance(coords, frozenset) else frozenset(tuple(x) for x in coords))
            for rp, coords in raw_eq_safe.items()
        }
        self.eq_sites_stats: Dict[str, Any] = maps.get("eq_sites_stats", {})
        self.subprocess_test_ids: List[str] = list(maps.get("subprocess_test_ids", ()))
        self._subprocess_livemask: Optional[int] = None
        _mn_id = "tests/test_parser.py::TestParser::test_max_nodes"
        _tpcds_id = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
        _tpch_id = "tests/test_executor.py::TestExecutor::test_execute_tpch"
        _gen_mem_id = "tests/test_generator.py::TestGenerator::test_generator_memory"
        self.max_nodes_num: Optional[int] = self.id_to_num.get(_mn_id)
        self.tpcds_num: Optional[int] = self.id_to_num.get(_tpcds_id)
        self.tpch_num: Optional[int] = self.id_to_num.get(_tpch_id)
        self.gen_mem_num: Optional[int] = self.id_to_num.get(_gen_mem_id)
        _tpcds_bidx = self.id_to_base_idx.get(_tpcds_id)
        self._tpcds_base_bit: int = (1 << _tpcds_bidx) if _tpcds_bidx is not None else 0
        self.tpcds_exec_qids: Tuple[int, ...] = tuple(int(x) for x in maps.get("tpcds_exec_qids", ()))
        self.tpcds_digests: Dict[int, str] = {int(k): str(v) for k, v in maps.get("tpcds_digests", {}).items()}
        self.tpcds_parse_digests: Dict[int, str] = {
            int(k): str(v) for k, v in maps.get("tpcds_parse_digests", {}).items()
        }
        self.tpcds_opt_digests: Dict[int, str] = {
            int(k): str(v) for k, v in maps.get("tpcds_opt_digests", {}).items()
        }
        self.tpcds_query_cov: Dict[str, Dict[int, int]] = {
            rp: {int(ln): int(m) for ln, m in lm.items()} for rp, lm in maps.get("tpcds_query_cov", {}).items()
        }
        self.tpcds_parse_cov: Dict[str, FrozenSet[int]] = {
            rp: frozenset(int(x) for x in lns) for rp, lns in maps.get("tpcds_parse_cov", {}).items()
        }
        self.tpcds_s2_lines: Dict[str, FrozenSet[int]] = {
            rp: frozenset(int(x) for x in lns) for rp, lns in maps.get("tpcds_s2_lines", {}).items()
        }
        self.tpcds_s3_lines: Dict[str, FrozenSet[int]] = {
            rp: frozenset(int(x) for x in lns) for rp, lns in maps.get("tpcds_s3_lines", {}).items()
        }
        self.tpcds_s3_query_cov: Dict[str, Dict[int, int]] = {
            rp: {int(ln): int(m) for ln, m in lm.items()} for rp, lm in maps.get("tpcds_s3_query_cov", {}).items()
        }
        self.tpch_digests: Tuple[str, ...] = tuple(maps.get("tpch_digests", ()))
        self.max_nodes_files: FrozenSet[str] = frozenset(maps.get("max_nodes_files", ()))
        raw_opt_conn = maps.get("opt_conn_digests", {})
        self.opt_conn_digests_by_num: Dict[int, Tuple[str, ...]] = {
            self.id_to_num[tid]: tuple(digs) for tid, digs in raw_opt_conn.items() if tid in self.id_to_num
        }
        fast_unprobed: Set[int] = (
            {x for x in (self.max_nodes_num, self.tpcds_num, self.tpch_num, self.gen_mem_num) if x is not None}
            | {self.id_to_num[tid] for tid in self.subprocess_test_ids if tid in self.id_to_num}
            | {self.id_to_num[tid] for tid in maps.get("fast_unprobed_ids", ()) if tid in self.id_to_num}
            | {num for num in self.all_nums if self.num_to_parts[num] is None or self.num_to_ms[num] > 2500}
        )
        self._fast_unprobed_nums: FrozenSet[int] = frozenset(fast_unprobed)
        walker_bmask = 0
        for raw in self.raw_pkg_walkers.values():
            walker_bmask |= self._to_mask(raw)
        for raw in self.raw_dir_access.values():
            walker_bmask |= self._to_mask(raw)
        self._pkg_dir_walker_nums: FrozenSet[int] = frozenset(self._decode_masks(walker_bmask, 0))
        self.probe_cpu_budget_s: float = float(os.environ.get("BLAST_PROBE_BUDGET_S", "45.0"))
        self._fast_budget_env: Optional[float] = (
            float(os.environ["BLAST_FAST_BUDGET_S"]) if "BLAST_FAST_BUDGET_S" in os.environ else None
        )
        self._s3_budget_env: Optional[float] = (
            float(os.environ["BLAST_S3_BUDGET_S"]) if "BLAST_S3_BUDGET_S" in os.environ else None
        )
        self.max_s3_exec_qids: int = int(os.environ.get("BLAST_MAX_S3_QIDS", "20"))
        self.total_probe_cpu_s: float = 0.0
        self.fast_probe_cpu_s: float = 0.0
        self.s3_probe_cpu_s: float = 0.0
        self.last_probe_cpu_s: float = 0.0
        self._tpcds_schema_cache: Optional[Dict[str, Dict[str, str]]] = None
        self._raw_tpcds_tables: Optional[Dict[str, Tuple[Dict[str, str], List[tuple]]]] = None
        self._tpcds_table_cache: Dict[str, Tuple[Dict[str, str], List[tuple]]] = {}
        self._prewarm_mods: Optional[FrozenSet[str]] = None
        self._prewarm_dialects: Dict[str, Any] = {}
        self._code_cache: Dict[str, Any] = {}
        self._tpcds_sqls_cache: Optional[List[Tuple[bool, str]]] = None
        self._tpch_sqls_cache: Optional[List[str]] = None
        self._tpcds_cov_qmask: int = 0
        self._tpcds_parse_hit: bool = False
        self._tpcds_unmapped_cov_line: bool = False
        self._tpcds_non_line: bool = False
        self._tpcds_s2_hit: bool = False
        self._tpcds_s3_qmask: int = 0
        self._once_cache: Dict[Tuple[bool, str], frozenset] = {}
        self._pkg_lazy_cache: Dict[str, int] = {}
        self._mod_users_cache: Dict[str, Tuple[int, int]] = {}
        self.direct_imports: Dict[str, List[str]] = maps.get("imports", {})
        self._inv_imports: Optional[Dict[str, List[str]]] = None
        self._star_imported: Optional[FrozenSet[str]] = None
        self._src_text_cache: Dict[str, str] = {}
        self._marks: int = 0
        self._trusted_marks: int = 0
        self._other_added_text: str = ""

        self._trace: bool = TRACE
        self.reasons: List[str] = []
        self._reason_seen: Set[str] = set()
        self._empty_guard_pending: Dict[str, Tuple[int, int, str]] = {}
        self._last_num_sections: int = 0

        # Precompute module -> union of class_usage masks cleanly in O(N).
        self.mod_class_mask: Dict[str, int] = defaultdict(int)
        for ckey, val in self.raw_class_usage.items():
            mod = ckey.split("::", 1)[0]
            self.mod_class_mask[mod] |= self._to_mask(val)
        self._init_cls_usage_len = len(self.raw_class_usage)

        # Caches populated lazily.
        self._line_cache: Dict[str, Dict[int, int]] = {}
        self._mod_cov_cache: Dict[str, int] = {}
        self._file_acc_cache: Dict[str, int] = {}
        self._dir_acc_cache: Dict[str, int] = {}
        self._cls_use_cache: Dict[str, int] = {}
        self._attr_reader_cache: Dict[str, int] = {}
        self._ast_cache: Dict[str, Tuple[str, Optional[ast.Module]]] = {}
        self._lines_cache: Dict[str, Tuple[str, List[str]]] = {}

    @staticmethod
    def _to_mask(val: Any) -> int:
        if isinstance(val, int):
            return val
        m = 0
        if val:
            for i in val:
                m |= 1 << i
        return m

    def _decode_masks(self, base_mask: int, live_mask: int) -> Set[int]:
        out: Set[int] = set()
        idx_to_num = self.idx_to_num
        m = base_mask
        while m:
            lsb = m & -m
            m ^= lsb
            n = idx_to_num[lsb.bit_length() - 1]
            if n is not None:
                out.add(n)
        m = live_mask
        while m:
            lsb = m & -m
            m ^= lsb
            out.add(lsb.bit_length() - 1)
        return out

    def _file_access(self, rel_path: str) -> int:
        cached = self._file_acc_cache.get(rel_path)
        if cached is None:
            raw = self.raw_file_access.get(rel_path)
            cached = self._to_mask(raw) if raw else 0
            self._file_acc_cache[rel_path] = cached
        if cached & self._tpcds_base_bit:
            self._tpcds_non_line = True
        return cached

    def _dir_walker_tests(self, rel_path: str) -> int:
        parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else "."
        cached = self._dir_acc_cache.get(parent)
        if cached is not None:
            if cached & self._tpcds_base_bit:
                self._tpcds_non_line = True
            return cached
        mask = 0
        if self.raw_dir_access:
            mask |= self._to_mask(self.raw_dir_access.get(".", 0))
            if parent and parent != ".":
                parts = parent.split("/")
                cur = ""
                for part in parts:
                    cur = part if not cur else f"{cur}/{part}"
                    if cur in self.raw_dir_access:
                        mask |= self._to_mask(self.raw_dir_access[cur])
        self._dir_acc_cache[parent] = mask
        if mask & self._tpcds_base_bit:
            self._tpcds_non_line = True
        return mask

    def _pkg_walker_tests(self, rel_path: str) -> int:
        mask = 0
        parent = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
        for pkg, raw in self.raw_pkg_walkers.items():
            if parent == pkg or parent.startswith(pkg + "/"):
                mask |= self._to_mask(raw)
        if mask & self._tpcds_base_bit:
            self._tpcds_non_line = True
        return mask

    def _class_usage(self, cls_key: str) -> int:
        cached = self._cls_use_cache.get(cls_key)
        if cached is None:
            raw = self.raw_class_usage.get(cls_key)
            cached = self._to_mask(raw) if raw else 0
            self._cls_use_cache[cls_key] = cached
        if cached & self._tpcds_base_bit:
            self._tpcds_non_line = True
        return cached

    def _dict_key_mask(self, attr: str, cls_key: str, norm_key: str) -> int:
        by_attr = self.raw_dict_key_usage.get(attr)
        if not by_attr:
            return 0
        by_cls = by_attr.get(cls_key)
        if not by_cls:
            return 0
        raw = by_cls.get(norm_key)
        mask = self._to_mask(raw) if raw else 0
        if norm_key.startswith("enum:"):
            suffix = norm_key[5:]
            for pfx in ("int:", "str:"):
                alt = by_cls.get(pfx + suffix)
                if alt:
                    mask |= self._to_mask(alt)
        elif norm_key.startswith("tuple:") and "enum:" in norm_key:
            for pfx in ("int:", "str:"):
                alt_k = norm_key.replace("enum:", pfx)
                alt = by_cls.get(alt_k)
                if alt:
                    mask |= self._to_mask(alt)
        if mask & self._tpcds_base_bit:
            self._tpcds_non_line = True
        return mask

    def _attr_reader_mask(self, attr: str) -> int:
        cached = self._attr_reader_cache.get(attr)
        if cached is not None:
            if cached & self._tpcds_base_bit:
                self._tpcds_non_line = True
            return cached
        mask = 0
        rmap = self.attr_read_lines.get(attr)
        if rmap:
            for rel, lns in rmap.items():
                mask |= self._lines_cov(rel, lns)
        for tf in self.attr_import_test_files.get(attr, ()):
            bm = self.file_to_basemask.get(tf, 0)
            if bm & self._tpcds_base_bit:
                self._tpcds_non_line = True
            mask |= bm
        self._attr_reader_cache[attr] = mask
        return mask

    def _line_map(self, rel_path: str) -> Dict[int, int]:
        cached = self._line_cache.get(rel_path)
        if cached is None:
            raw = self.raw_line_cov.get(rel_path)
            cols = self.collection_lines.get(rel_path)
            if not raw and not cols:
                cached = {}
            else:
                if not raw:
                    cached = {}
                elif self.is_v2 and isinstance(next(iter(raw.keys())), int) and isinstance(next(iter(raw.values())), int):
                    cached = dict(raw) if cols else raw  # type: ignore[assignment]
                else:
                    cached = {}
                    for lns, idxs in raw.items():
                        m = self._to_mask(idxs)
                        if m:
                            cached[int(lns)] = m
                if cols:
                    for ln, tfs in cols.items():
                        cm = 0
                        for tf in tfs:
                            cm |= self.file_to_basemask.get(tf, 0)
                        if cm:
                            li = int(ln)
                            cached[li] = cached.get(li, 0) | cm
            self._line_cache[rel_path] = cached
        return cached

    def _module_cov(self, rel_path: str) -> int:
        cached = self._mod_cov_cache.get(rel_path)
        if cached is None:
            lmap = self._line_map(rel_path)
            m = 0
            for lm in lmap.values():
                m |= lm
            cached = m
            self._mod_cov_cache[rel_path] = cached
        if cached & self._tpcds_base_bit:
            self._tpcds_non_line = True
        return cached

    def _lines_cov(self, rel_path: str, lines: Iterable[int]) -> int:
        lmap = self._line_map(rel_path)
        if not lmap:
            return 0
        tbit = self._tpcds_base_bit
        qcov = self.tpcds_query_cov.get(rel_path)
        pcov = self.tpcds_parse_cov.get(rel_path)
        s2set = self.tpcds_s2_lines.get(rel_path)
        s3qmap = self.tpcds_s3_query_cov.get(rel_path)
        s3set = self.tpcds_s3_lines.get(rel_path)
        m = 0
        for ln in lines:
            v = lmap.get(ln)
            if v:
                m |= v
                if tbit and (v & tbit):
                    qm = qcov.get(ln, 0) if qcov else 0
                    if qm:
                        self._tpcds_cov_qmask |= qm
                        if s2set and ln in s2set:
                            self._tpcds_s2_hit = True
                        m3 = s3qmap.get(ln, 0) if s3qmap else 0
                        if m3:
                            self._tpcds_s3_qmask |= m3
                        elif s3set and ln in s3set:
                            self._tpcds_s3_qmask |= qm
                    elif pcov and ln in pcov:
                        self._tpcds_parse_hit = True
                    else:
                        self._tpcds_unmapped_cov_line = True
        return m

    def _baseline_src_ast_lines(self, rel_path: str) -> Tuple[str, Optional[ast.Module], List[str]]:
        cached = self._ast_cache.get(rel_path)
        if cached is None:
            p = self.src_root / rel_path
            if not p.is_file():
                cached = ("", None)
                self._ast_cache[rel_path] = cached
                self._lines_cache[rel_path] = ("", [])
                return "", None, []
            src = p.read_text("utf-8", errors="replace")
            try:
                tree: Optional[ast.Module] = ast.parse(src)
            except SyntaxError:
                tree = None
            cached = (src, tree)
            self._ast_cache[rel_path] = cached
            lines = src.splitlines()
            self._lines_cache[rel_path] = (src, lines)
            return src, tree, lines
        src, tree = cached
        lc = self._lines_cache.get(rel_path)
        if lc is None or lc[0] is not src:
            lines = src.splitlines()
            self._lines_cache[rel_path] = (src, lines)
        else:
            lines = lc[1]
        return src, tree, lines

    def _importer_test_livemask(self, rel_path: str) -> int:
        m = 0
        for rev in self.rev_imports.get(rel_path, ()):
            m |= self.file_to_livemask.get(rev, 0)
        return m

    def _mod_classes_usage_mask(self, rel_path: str, old_mod: Optional[ast.Module]) -> int:
        if old_mod is None:
            return 0
        if len(self.raw_class_usage) == self._init_cls_usage_len:
            m = self.mod_class_mask.get(rel_path, 0)
        else:
            # Re-scan when raw_class_usage was mutated dynamically in unit tests.
            prefix = f"{rel_path}::"
            m = 0
            for ckey in self.raw_class_usage:
                if ckey.startswith(prefix):
                    m |= self._class_usage(ckey)
        if m & self._tpcds_base_bit:
            self._tpcds_non_line = True
        return m

    def _rule_enabled(self, name: str) -> bool:
        cur_dis = os.environ.get("BLAST_DISABLE", "")
        cur_ena = os.environ.get("BLAST_ENABLE", "")
        if cur_dis != _INIT_DISABLE_ENV or cur_ena != _INIT_ENABLE_ENV:
            ena = {x.strip() for x in cur_ena.split(",") if x.strip()}
            dis = (DEFAULT_DISABLED - ena) | {x.strip() for x in cur_dis.split(",") if x.strip()}
            return name not in dis
        return name not in DISABLED

    def _note(self, tag: str) -> None:
        if self._trace and tag not in self._reason_seen:
            self._reason_seen.add(tag)
            self.reasons.append(tag)

    def _lazy_test_livemask(self, rel_path: str) -> int:
        m = 0
        for tf in self.lazy_test_importers.get(rel_path, ()):
            m |= self.file_to_livemask.get(tf, 0)
        return m

    def _pkg_sibling_lazy_livemask(self, pkg: str) -> int:
        if not pkg:
            return 0
        cached = self._pkg_lazy_cache.get(pkg)
        if cached is not None:
            return cached
        m = self._lazy_test_livemask(f"{pkg}/__init__.py")
        prefix = pkg + "/"
        target_slashes = pkg.count("/") + 1
        for k, tfs in self.lazy_test_importers.items():
            if k.startswith(prefix) and k.count("/") == target_slashes:
                for tf in tfs:
                    m |= self.file_to_livemask.get(tf, 0)
        self._pkg_lazy_cache[pkg] = m
        return m

    def _module_wide_fallback(
        self,
        rel_path: str,
        old_mod: Optional[ast.Module],
        include_importers: bool = False,
    ) -> Tuple[int, int]:
        base = 0
        live = 0
        base |= self._file_access(rel_path)
        base |= self._dir_walker_tests(rel_path)
        base |= self._pkg_walker_tests(rel_path)
        base |= self._module_cov(rel_path)
        for link_cls in self.dialect_typing_links.get(rel_path, ()):
            base |= self._class_usage(link_cls)
        base |= self._mod_classes_usage_mask(rel_path, old_mod)

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
        added_imp = False
        if include_importers or not is_dialect_mod or base == 0:
            live |= self._importer_test_livemask(rel_path)
            added_imp = True
        if rel_path == "sqlglot/__init__.py":
            live |= self.all_livemask
        if self._trace:
            self._note(f"module_wide:{rel_path}{'+imp' if added_imp else ''}")
        return base, live

    def _cls_key(self, rel_path: str, cls_stack: List[str]) -> str:
        return f"{rel_path}::{'.'.join(cls_stack)}"

    def _once_escalation(self, rel_path: str, old_mod: Optional[ast.Module]) -> Tuple[int, int]:
        """Module-wide users + static importers + tests reaching the module through lazy imports."""
        b, l = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
        l |= self._lazy_test_livemask(rel_path)
        if self._trace:
            self._note(f"once:{rel_path}")
        return b, l

    def _touches_once_lines(self, rel_path: str, lines: Sequence[int], fn_root: Optional[ast.AST]) -> bool:
        """True if any changed line inside fn_root's *body* only ever runs once per process."""
        it_on = self._rule_enabled("import_trigger")
        cache_k = (it_on, rel_path)
        once = self._once_cache.get(cache_k)
        if once is None:
            s = set(self.raw_once_lines.get(rel_path, ()))
            if not it_on:
                tmap = self.import_trigger_lines.get(rel_path)
                if tmap:
                    s.update(tmap.keys())
            once = frozenset(s)
            self._once_cache[cache_k] = once
        if not once:
            return False
        lo = 0
        if isinstance(fn_root, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn_root.body:
            first = fn_root.body[0]
            if _is_docstring_stmt(first) and len(fn_root.body) > 1:
                first = fn_root.body[1]
            lo = first.lineno  # def / decorator / default-arg lines are import-time by nature: ignore
        return any(ln >= lo and ln in once for ln in lines)

    def _module_users_cheap(self, rel_path: str) -> Tuple[int, int]:
        cached = self._mod_users_cache.get(rel_path)
        if cached is not None:
            return cached
        base = (
            self._file_access(rel_path)
            | self._dir_walker_tests(rel_path)
            | self._pkg_walker_tests(rel_path)
            | self._module_cov(rel_path)
            | self._mod_classes_usage_mask(rel_path, None)
        )
        for link_cls in self.dialect_typing_links.get(rel_path, ()):
            base |= self._class_usage(link_cls)
        live = self._importer_test_livemask(rel_path) | self._lazy_test_livemask(rel_path)
        if rel_path == "sqlglot/__init__.py":
            live |= self.all_livemask
        cached = (base, live)
        self._mod_users_cache[rel_path] = cached
        return cached

    def _import_trigger_mask(self, rel_path: str, lines: Iterable[int], fn_root: Optional[ast.AST]) -> Tuple[int, int]:
        tmap = self.import_trigger_lines.get(rel_path)
        if not tmap:
            return 0, 0
        lo = 0
        if isinstance(fn_root, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn_root.body:
            first = fn_root.body[0]
            if _is_docstring_stmt(first) and len(fn_root.body) > 1:
                first = fn_root.body[1]
            lo = first.lineno
        trigs_hit: Set[str] = set()
        for ln in lines:
            if ln >= lo:
                trigs = tmap.get(ln)
                if trigs:
                    trigs_hit.update(trigs)
        if not trigs_hit:
            return 0, 0
        tb, tl = 0, 0
        for trig in sorted(trigs_hit):
            mb, ml = self._module_users_cheap(trig)
            tb |= mb
            tl |= ml
        if self._trace and (tb | tl):
            self._note(f"import_trigger:{rel_path}")
        return tb, tl

    def _lines_with_fallback(
        self,
        rel_path: str,
        lines: Iterable[int],
        cls_stack: List[str],
        old_fn_root: ast.stmt,
        old_mod: Optional[ast.Module],
    ) -> Tuple[int, int]:
        lines = list(lines)
        itb, itl = self._import_trigger_mask(rel_path, lines, old_fn_root) if self._rule_enabled("import_trigger") else (0, 0)
        if self._touches_once_lines(rel_path, lines, old_fn_root):
            # Once-per-process code (metaclass / __init_subclass__ hooks, factory outer bodies, memoized
            # builders): per-test coverage only credits the first test that triggered it.
            b, l = self._once_escalation(rel_path, old_mod)
            return b | self._lines_cov(rel_path, lines) | itb, l | itl
        m = self._lines_cov(rel_path, lines)
        if m:
            if self._trace:
                self._note(f"cov:{rel_path}")
            return m | itb, itl
        fn0, fn1 = _stmt_span(old_fn_root)
        m = self._lines_cov(rel_path, range(fn0, fn1 + 1))
        if m:
            if self._trace:
                self._note(f"fn_fallback:{rel_path}")
            return m | itb, itl
        if cls_stack:
            ck = self._cls_key(rel_path, cls_stack)
            m = self._class_usage(ck)
            if m:
                if self._trace:
                    self._note(f"cls_usage:{ck}")
                return m | itb, itl
        fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
        return fb | itb, fl | itl

    def _diff_fn_bodies(
        self,
        old_stmts: List[ast.stmt],
        new_stmts: List[ast.stmt],
        rel_path: str,
        cls_stack: List[str],
        old_fn_root: ast.stmt,
        old_mod: Optional[ast.Module],
        old_lines: List[str],
        new_lines: List[str],
        use_fallback: bool = True,
    ) -> Tuple[int, int]:
        """Recursively diff two function-body statement lists, returning (base_mask, live_mask)."""
        if _ast_list_eq(old_stmts, new_stmts):
            return 0, 0
        base = 0
        live = 0

        def _add_lines(lines: Iterable[int]) -> None:
            nonlocal base, live
            if use_fallback:
                b, l = self._lines_with_fallback(rel_path, lines, cls_stack, old_fn_root, old_mod)
                base |= b
                live |= l
            else:
                base |= self._lines_cov(rel_path, lines)

        osigs = [_stmt_sig(s, old_lines) for s in old_stmts]
        nsigs = [_stmt_sig(s, new_lines) for s in new_stmts]
        sm = difflib.SequenceMatcher(None, osigs, nsigs, autojunk=False)

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for oi, nj in zip(range(i1, i2), range(j1, j2)):
                    os_ = old_stmts[oi]
                    ns_ = new_stmts[nj]
                    if _node_same_fast(os_, ns_, old_lines, new_lines):
                        continue
                    if isinstance(os_, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                        ns_, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        if _fn_signature_changed(os_, ns_) or _fn_scope_shape(os_) != _fn_scope_shape(ns_):
                            s0, s1 = _stmt_span(os_)
                            _add_lines(range(s0, s1 + 1))
                        else:
                            b, l = self._diff_fn_bodies(
                                os_.body, ns_.body, rel_path, cls_stack, os_, old_mod, old_lines, new_lines, use_fallback
                            )
                            base |= b
                            live |= l
                    else:
                        s0, s1 = _stmt_span(os_)
                        _add_lines(range(s0, s1 + 1))
                continue

            if tag == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1:
                os_ = old_stmts[i1]
                ns_ = new_stmts[j1]
                if _node_same_fast(os_, ns_, old_lines, new_lines):
                    continue
                if type(os_) is type(ns_) and isinstance(os_, ast.If) and isinstance(ns_, ast.If):
                    if not _ast_eq(os_.test, ns_.test):
                        _add_lines(_header_lines(os_))
                    if not _ast_list_eq(os_.body, ns_.body):
                        b, l = self._diff_fn_bodies(
                            os_.body, ns_.body, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                        )
                        base |= b
                        live |= l
                    if not _ast_list_eq(os_.orelse, ns_.orelse):
                        b, l = self._diff_fn_bodies(
                            os_.orelse, ns_.orelse, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                        )
                        base |= b
                        live |= l
                    continue
                if type(os_) is type(ns_) and isinstance(os_, (ast.For, ast.AsyncFor)) and isinstance(ns_, (ast.For, ast.AsyncFor)):
                    if not _ast_eq(os_.target, ns_.target) or not _ast_eq(os_.iter, ns_.iter):
                        _add_lines(_header_lines(os_))
                    else:
                        b1, l1 = self._diff_fn_bodies(
                            os_.body, ns_.body, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                        )
                        b2, l2 = self._diff_fn_bodies(
                            os_.orelse, ns_.orelse, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                        )
                        base |= b1 | b2
                        live |= l1 | l2
                    continue
                if type(os_) is type(ns_) and isinstance(os_, ast.While) and isinstance(ns_, ast.While):
                    if not _ast_eq(os_.test, ns_.test):
                        _add_lines(_header_lines(os_))
                    b1, l1 = self._diff_fn_bodies(
                        os_.body, ns_.body, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                    )
                    b2, l2 = self._diff_fn_bodies(
                        os_.orelse, ns_.orelse, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                    )
                    base |= b1 | b2
                    live |= l1 | l2
                    continue
                if type(os_) is type(ns_) and isinstance(os_, (ast.With, ast.AsyncWith)) and isinstance(ns_, (ast.With, ast.AsyncWith)):
                    if not _ast_list_eq(os_.items, ns_.items):
                        _add_lines(_header_lines(os_))
                    else:
                        b, l = self._diff_fn_bodies(
                            os_.body, ns_.body, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                        )
                        base |= b
                        live |= l
                    continue
                if type(os_) is type(ns_) and isinstance(os_, ast.Try) and isinstance(ns_, ast.Try):
                    b1, l1 = self._diff_fn_bodies(
                        os_.body, ns_.body, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                    )
                    b2, l2 = self._diff_fn_bodies(
                        os_.orelse, ns_.orelse, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                    )
                    b3, l3 = self._diff_fn_bodies(
                        os_.finalbody, ns_.finalbody, rel_path, cls_stack, old_fn_root, old_mod, old_lines, new_lines, use_fallback
                    )
                    base |= b1 | b2 | b3
                    live |= l1 | l2 | l3
                    if len(os_.handlers) != len(ns_.handlers):
                        s0, s1 = _stmt_span(os_)
                        _add_lines(range(s0, s1 + 1))
                    else:
                        for oh, nh in zip(os_.handlers, ns_.handlers):
                            header_changed = (
                                (oh.type is None) != (nh.type is None)
                                or (oh.type is not None and not _ast_eq(oh.type, nh.type))
                                or oh.name != nh.name
                            )
                            if header_changed:
                                # Handler type/binding change affects anyone entering the try body.
                                try_lines = range(os_.lineno, (os_.end_lineno or os_.lineno) + 1)
                                _add_lines(try_lines)
                            elif not _ast_list_eq(oh.body, nh.body):
                                bh, lh = self._diff_fn_bodies(
                                    oh.body, nh.body, rel_path, cls_stack, oh, old_mod, old_lines, new_lines, use_fallback
                                )
                                base |= bh
                                live |= lh
                    continue

            if tag == "replace" and (i2 - i1) == (j2 - j1):
                # Filter out statements whose AST didn't change (e.g. comments/formatting).
                any_real_diff = False
                for oi, nj in zip(range(i1, i2), range(j1, j2)):
                    os_ = old_stmts[oi]
                    ns_ = new_stmts[nj]
                    if _node_same_fast(os_, ns_, old_lines, new_lines):
                        continue
                    any_real_diff = True
                    if _is_docstring_stmt(os_):
                        fn0, fn1 = _stmt_span(old_fn_root)
                        _add_lines(range(fn0, fn1 + 1))
                    else:
                        s0, s1 = _stmt_span(os_)
                        _add_lines(range(s0, s1 + 1))
                if not any_real_diff:
                    continue
                continue

            if tag in ("replace", "delete"):
                for oi in range(i1, i2):
                    os_ = old_stmts[oi]
                    if _is_docstring_stmt(os_):
                        fn0, fn1 = _stmt_span(old_fn_root)
                        _add_lines(range(fn0, fn1 + 1))
                    else:
                        s0, s1 = _stmt_span(os_)
                        _add_lines(range(s0, s1 + 1))

            if tag == "insert":
                cov_lines: List[int] = []
                for idx in range(i1 - 1, -1, -1):
                    cand = old_stmts[idx]
                    if not _is_docstring_stmt(cand):
                        c0, c1 = _stmt_span(cand)
                        cov_lines.extend(range(c0, c1 + 1))
                        break
                for idx in range(i1, len(old_stmts)):
                    cand = old_stmts[idx]
                    if not _is_docstring_stmt(cand):
                        c0, c1 = _stmt_span(cand)
                        cov_lines.extend(range(c0, c1 + 1))
                        break
                if not cov_lines:
                    cov_lines.extend(_header_lines(old_fn_root))
                _add_lines(cov_lines)

        return base, live

    def _try_narrow_decorator_removal(
        self,
        os_: ast.AST,
        ns_: ast.AST,
        rel_path: str,
    ) -> Optional[Tuple[int, int]]:
        if not self._rule_enabled("decorator_removal_narrow"):
            return None
        if not isinstance(os_, (ast.FunctionDef, ast.AsyncFunctionDef)) or type(os_) is not type(ns_):
            return None
        if os_.name != ns_.name:
            return None
        old_decs = os_.decorator_list
        new_decs = ns_.decorator_list
        if len(new_decs) >= len(old_decs):
            return None
        j = 0
        removed: List[ast.expr] = []
        for d in old_decs:
            if j < len(new_decs) and _ast_eq(d, new_decs[j]):
                j += 1
            else:
                removed.append(d)
        if j != len(new_decs) or not removed:
            return None
        for d in removed:
            tname = _decorator_terminal_name(d)
            if tname is None or tname in _DANGEROUS_REMOVED_DECORATORS:
                return None
        if not (
            _ast_eq(os_.args, ns_.args)
            and _ast_eq(os_.returns, ns_.returns)
            and _ast_list_eq(getattr(os_, "type_params", []), getattr(ns_, "type_params", []))
            and getattr(os_, "type_comment", None) == getattr(ns_, "type_comment", None)
            and _ast_list_eq(os_.body, ns_.body)
        ):
            return None
        s0, s1 = _stmt_span(os_)
        fn_lines = range(s0, s1 + 1)
        cov = self._lines_cov(rel_path, fn_lines)
        if not cov:
            return None
        itb, itl = self._import_trigger_mask(rel_path, fn_lines, os_) if self._rule_enabled("import_trigger") else (0, 0)
        if self._trace:
            self._note(f"decorator_narrow:{rel_path}")
        return cov | itb, itl

    def _get_subprocess_livemask(self) -> int:
        if self._subprocess_livemask is None:
            m = 0
            for tid in self.subprocess_test_ids:
                num = self.id_to_num.get(tid)
                if num is not None:
                    m |= 1 << num
            self._subprocess_livemask = m
        return self._subprocess_livemask

    def _read_src(self, rel_path: str) -> str:
        cached = self._src_text_cache.get(rel_path)
        if cached is not None:
            return cached
        if rel_path in self._ast_cache:
            src = self._ast_cache[rel_path][0]
            self._src_text_cache[rel_path] = src
            return src
        p = self.src_root / rel_path
        try:
            src = p.read_text("utf-8", errors="replace") if p.is_file() else ""
        except OSError:
            src = ""
        self._src_text_cache[rel_path] = src
        return src

    def _star_imported_paths(self) -> FrozenSet[str]:
        if self._star_imported is not None:
            return self._star_imported
        star_targets: Set[str] = set()
        star_edges: Dict[str, Set[str]] = defaultdict(set)
        sqlglot_dir = self.src_root / "sqlglot"
        if sqlglot_dir.is_dir():
            for p in sorted(sqlglot_dir.rglob("*.py")):
                if "__pycache__" in p.parts:
                    continue
                rel = p.relative_to(self.src_root).as_posix()
                src = self._read_src(rel)
                if "import *" not in src:
                    continue
                try:
                    mod = ast.parse(src)
                except SyntaxError:
                    continue
                for st in ast.walk(mod):
                    if isinstance(st, ast.ImportFrom) and any(a.name == "*" for a in st.names):
                        res = _resolve_local_mod_path(
                            st.module or "", st.level, rel, self.src_root, set(), set()
                        )
                        if res is not None and res != "__MISSING__":
                            star_targets.add(res)
                            star_edges[rel].add(res)
        work = list(star_targets)
        while work:
            cur = work.pop()
            for nxt in star_edges.get(cur, ()):
                if nxt not in star_targets:
                    star_targets.add(nxt)
                    work.append(nxt)
        self._star_imported = frozenset(star_targets)
        return self._star_imported

    def _importers_of(self, rel_path: str) -> List[str]:
        if self._inv_imports is None:
            inv: Dict[str, Set[str]] = defaultdict(set)
            for imp_by, targets in self.direct_imports.items():
                for tgt in targets:
                    inv[tgt].add(imp_by)
            self._inv_imports = {k: sorted(v) for k, v in inv.items()}
        return self._inv_imports.get(rel_path, [])

    def _name_in_direct_importers(self, rel_path: str, name: str) -> bool:
        pat = re.compile(rf"\b{re.escape(name)}\b")
        for f in self._importers_of(rel_path):
            if pat.search(self._read_src(f)):
                return True
        return False

    def _neutral_filter(
        self,
        rel_path: str,
        old_src: str,
        old_mod: ast.Module,
        new_mod: ast.Module,
        old_lines: Optional[List[str]] = None,
        new_lines: Optional[List[str]] = None,
    ) -> Tuple[List[ast.stmt], Set[str]]:
        if not rel_path.startswith("sqlglot/"):
            return list(new_mod.body), set()

        en_fn = self._rule_enabled("module_new_fn")
        en_imp = self._rule_enabled("noop_stdlib_import")
        en_cls = self._rule_enabled("class_new_method")
        if not (en_fn or en_imp or en_cls):
            return list(new_mod.body), set()

        _old_tok_cache: Optional[Set[str]] = None

        def _get_old_tokens() -> Set[str]:
            nonlocal _old_tok_cache
            if _old_tok_cache is None:
                _old_tok_cache = set(_WORD_RE.findall(old_src))
            return _old_tok_cache

        future: Optional[bool] = None

        def _is_future() -> bool:
            nonlocal future
            if future is None:
                future = _has_future_annotations(old_mod) and _has_future_annotations(new_mod)
            return future

        module_ok = (not rel_path.endswith("__init__.py") and not any(m in old_src for m in _DYN_MARKERS)) if (en_fn or en_imp) else False
        body: List[ast.stmt] = list(new_mod.body)
        kinds: Set[str] = set()

        # (a) Fresh undecorated top-level helper functions
        if en_fn and module_ok:
            old_top = _top_level_names(old_mod.body)
            has_cand_fn = any(
                isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name not in old_top
                for st in body
            )
            if has_cand_fn:
                old_tokens = _get_old_tokens()
                new_bind_counts: Dict[str, int] = Counter()
                for st in body:
                    for bn in _top_level_names([st]):
                        new_bind_counts[bn] += 1
                filtered_a: List[ast.stmt] = []
                for st in body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        n = st.name
                        if (
                            n not in old_top
                            and n not in old_tokens
                            and n not in _BUILTIN_NAMES
                            and not (n.startswith("__") and n.endswith("__"))
                            and _deftime_safe(st, _is_future())
                            and new_bind_counts.get(n, 0) == 1
                            and ((n not in self.known_words) or (n.startswith("_") and not self._name_in_direct_importers(rel_path, n)))
                            and (n.startswith("_") or rel_path not in self._star_imported_paths())
                        ):
                            kinds.add("module_new_fn")
                            if self._trace:
                                self._note(f"module_new_fn:{rel_path}:{n}")
                            continue
                    filtered_a.append(st)
                body = filtered_a

        # (b) Pure stdlib import additions and import reorders
        if en_imp and module_ok:
            def _is_nonfuture_imp(st: ast.stmt) -> bool:
                return isinstance(st, ast.Import) or (isinstance(st, ast.ImportFrom) and st.module != "__future__")

            old_imps = [st for st in old_mod.body if _is_nonfuture_imp(st)]
            new_imps = [st for st in body if _is_nonfuture_imp(st)]
            imps_unchanged = (
                old_lines is not None
                and new_lines is not None
                and len(old_imps) == len(new_imps)
                and all(
                    old_lines[a.lineno - 1 : (a.end_lineno or a.lineno)]
                    == new_lines[b.lineno - 1 : (b.end_lineno or b.lineno)]
                    for a, b in zip(old_imps, new_imps)
                )
            )
            if not imps_unchanged:
                def _is_safe_stdlib_mod(st: ast.stmt) -> bool:
                    if isinstance(st, ast.Import):
                        return all(a.name != "*" and a.name.split(".", 1)[0] in _SAFE_STDLIB for a in st.names)
                    if isinstance(st, ast.ImportFrom):
                        return (
                            st.level == 0
                            and bool(st.module)
                            and (st.module or "").split(".", 1)[0] in _SAFE_STDLIB
                            and all(a.name != "*" for a in st.names)
                        )
                    return False

                old_tokens = _get_old_tokens()
                old_imp_rem = Counter(
                    ast.dump(st, include_attributes=False) for st in old_imps
                )
                added_idxs: List[int] = []
                all_added_safe = True
                for idx, st in enumerate(body):
                    if not _is_nonfuture_imp(st):
                        continue
                    d = ast.dump(st, include_attributes=False)
                    if old_imp_rem[d] > 0:
                        old_imp_rem[d] -= 1
                    else:
                        added_idxs.append(idx)
                        if not _is_safe_stdlib_mod(st):
                            all_added_safe = False
                            continue
                        for bn in _bound_import_names(st):
                            if (
                                bn in old_tokens
                                or bn in _BUILTIN_NAMES
                                or (bn.startswith("__") and bn.endswith("__"))
                                or (not bn.startswith("_") and rel_path in self._star_imported_paths())
                            ):
                                all_added_safe = False
                                break
                if added_idxs and all_added_safe:
                    drop_set = set(added_idxs)
                    body = [st for idx, st in enumerate(body) if idx not in drop_set]
                    kinds.add("stdlib_import_add")

                def _split_runs(stmts: Sequence[ast.stmt]) -> Tuple[List[List[ast.stmt]], List[Tuple[bool, List[ast.stmt]]]]:
                    runs: List[List[ast.stmt]] = []
                    segs: List[Tuple[bool, List[ast.stmt]]] = []
                    cur_i: List[ast.stmt] = []
                    cur_o: List[ast.stmt] = []
                    for s in stmts:
                        if _is_nonfuture_imp(s):
                            if cur_o:
                                segs.append((False, cur_o))
                                cur_o = []
                            cur_i.append(s)
                        else:
                            if cur_i:
                                runs.append(cur_i)
                                segs.append((True, cur_i))
                                cur_i = []
                            cur_o.append(s)
                    if cur_i:
                        runs.append(cur_i)
                        segs.append((True, cur_i))
                    if cur_o:
                        segs.append((False, cur_o))
                    return runs, segs

                old_runs, _ = _split_runs(old_mod.body)
                new_runs, new_segs = _split_runs(body)
                if len(old_runs) == len(new_runs) and old_runs:
                    old_dumps_list = [[ast.dump(s, include_attributes=False) for s in r] for r in old_runs]
                    new_dumps_list = [[ast.dump(s, include_attributes=False) for s in r] for r in new_runs]
                    if all(Counter(od) == Counter(nd) for od, nd in zip(old_dumps_list, new_dumps_list)):
                        diff_idxs = {k for k, (od, nd) in enumerate(zip(old_dumps_list, new_dumps_list)) if od != nd}
                        if diff_idxs and all(_is_safe_stdlib_mod(s) for k in diff_idxs for s in new_runs[k]):
                            from collections import deque
                            rebuilt: List[ast.stmt] = []
                            r_idx = 0
                            for is_imp, seg in new_segs:
                                if not is_imp:
                                    rebuilt.extend(seg)
                                    continue
                                if r_idx in diff_idxs:
                                    by_dump: Dict[str, deque] = defaultdict(deque)
                                    for s, sd in zip(seg, new_dumps_list[r_idx]):
                                        by_dump[sd].append(s)
                                    for od in old_dumps_list[r_idx]:
                                        rebuilt.append(by_dump[od].popleft())
                                else:
                                    rebuilt.extend(seg)
                                r_idx += 1
                            body = rebuilt
                            kinds.add("import_reorder")

        # (c) Fresh non-overriding undecorated class methods
        if en_cls:
            def _cls_bound_targets(stmts: Sequence[ast.stmt]) -> Set[str]:
                out: Set[str] = set()
                def _collect_tgt(t: ast.AST) -> None:
                    if isinstance(t, ast.Name):
                        out.add(t.id)
                    elif isinstance(t, (ast.Tuple, ast.List)):
                        for e in t.elts:
                            _collect_tgt(e)
                    elif isinstance(t, ast.Starred):
                        _collect_tgt(t.value)
                for s in stmts:
                    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        out.add(s.name)
                    elif isinstance(s, ast.Assign):
                        for t in s.targets:
                            _collect_tgt(t)
                    elif isinstance(s, ast.AnnAssign):
                        _collect_tgt(s.target)
                return out

            def _cls_bind_counts(stmts: Sequence[ast.stmt]) -> Dict[str, int]:
                counts: Dict[str, int] = Counter()
                for s in stmts:
                    for bn in _cls_bound_targets([s]):
                        counts[bn] += 1
                return counts

            def _filter_cls_body(
                cur_old: Sequence[ast.stmt],
                cur_new: Sequence[ast.stmt],
                cls_stack: List[str],
            ) -> Tuple[List[ast.stmt], bool]:
                old_by_name: Dict[str, List[ast.ClassDef]] = defaultdict(list)
                new_by_name: Dict[str, List[ast.ClassDef]] = defaultdict(list)
                for s in cur_old:
                    if isinstance(s, ast.ClassDef):
                        old_by_name[s.name].append(s)
                for s in cur_new:
                    if isinstance(s, ast.ClassDef):
                        new_by_name[s.name].append(s)

                out_stmts: List[ast.stmt] = []
                changed = False
                for s in cur_new:
                    if isinstance(s, ast.ClassDef):
                        oc_list = old_by_name.get(s.name, ())
                        nc_list = new_by_name.get(s.name, ())
                        if len(oc_list) == 1 and len(nc_list) == 1:
                            oc = oc_list[0]
                            if (
                                old_lines is not None
                                and new_lines is not None
                                and ((oc.end_lineno or oc.lineno) - oc.lineno) == ((s.end_lineno or s.lineno) - s.lineno)
                                and old_lines[oc.lineno - 1 : (oc.end_lineno or oc.lineno)]
                                == new_lines[s.lineno - 1 : (s.end_lineno or s.lineno)]
                            ):
                                out_stmts.append(s)
                                continue
                            if (
                                _ast_list_eq(oc.bases, s.bases)
                                and _ast_list_eq(oc.keywords, s.keywords)
                                and _ast_list_eq(oc.decorator_list, s.decorator_list)
                            ):
                                sub_stk = cls_stack + [s.name]
                                sub_body, sub_changed = _filter_cls_body(oc.body, s.body, sub_stk)
                                ob = _cls_bound_targets(oc.body)
                                has_cand_m = any(
                                    isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                                    and m.name not in ob
                                    and not (m.name.startswith("__") and m.name.endswith("__"))
                                    and m.name not in self.known_words
                                    and m.name not in _BASE_ATTR_NAMES
                                    and not m.name.endswith(("_sql", "_parser", "_parsers"))
                                    and not m.name.startswith("test")
                                    for m in sub_body
                                )
                                if has_cand_m:
                                    old_tokens = _get_old_tokens()
                                    nb_counts = _cls_bind_counts(s.body)
                                    fut = _is_future()
                                    filtered_cls: List[ast.stmt] = []
                                    dropped = False
                                    for m in sub_body:
                                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                            mn = m.name
                                            if (
                                                mn not in ob
                                                and not (mn.startswith("__") and mn.endswith("__"))
                                                and _deftime_safe(m, fut)
                                                and mn not in self.known_words
                                                and mn not in old_tokens
                                                and mn not in _BASE_ATTR_NAMES
                                                and not mn.endswith(("_sql", "_parser", "_parsers"))
                                                and not mn.startswith("test")
                                                and nb_counts.get(mn, 0) == 1
                                            ):
                                                dropped = True
                                                kinds.add("class_new_method")
                                                if self._trace:
                                                    self._note(f"class_new_method:{rel_path}::{'.'.join(sub_stk)}.{mn}")
                                                continue
                                        filtered_cls.append(m)
                                else:
                                    filtered_cls = sub_body
                                    dropped = False
                                if dropped or sub_changed:
                                    c2 = copy.copy(s)
                                    c2.body = filtered_cls
                                    out_stmts.append(c2)
                                    changed = True
                                    continue
                    out_stmts.append(s)
                return out_stmts, changed

            body, _ = _filter_cls_body(old_mod.body, body, [])

        return body, kinds

    def _module_const_readers(
        self,
        rel_path: str,
        name: str,
        old_mod: ast.Module,
        old_src: str,
        assign_lineno: int,
    ) -> Optional[Tuple[int, int]]:
        if not self._rule_enabled("module_const_narrow"):
            return None
        if not rel_path.startswith("sqlglot/") or rel_path.endswith("__init__.py"):
            return None
        if any(m in old_src for m in _DYN_MARKERS):
            return None
        if name.startswith("__") and name.endswith("__"):
            return None
        quoted_re = re.compile(rf"['\"]{re.escape(name)}['\"]")
        if quoted_re.search(old_src) or f"global {name}" in old_src:
            return None
        if not name.startswith("_") and rel_path in self._star_imported_paths():
            return None

        def _ref_sites(
            root: ast.AST,
            ignore_imports: bool,
        ) -> Optional[List[Tuple[Any, bool, Optional[ast.stmt], ast.AST]]]:
            sites: List[Tuple[Any, bool, Optional[ast.stmt], ast.AST]] = []
            stmt_stack: List[ast.stmt] = []

            def _visit(node: Any, in_fn: bool) -> bool:
                if node is None:
                    return True
                if isinstance(node, list):
                    for it in node:
                        if not _visit(it, in_fn):
                            return False
                    return True
                if not isinstance(node, ast.AST):
                    return True
                if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
                    return False
                if ignore_imports and isinstance(node, (ast.Import, ast.ImportFrom)):
                    return True
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    stmt_stack.append(node)
                    try:
                        if not _visit(node.decorator_list, in_fn):
                            return False
                        for grp in (node.args.posonlyargs, node.args.args, node.args.kwonlyargs):
                            for a in grp:
                                if not _visit(a.annotation, in_fn):
                                    return False
                        for a in (node.args.vararg, node.args.kwarg):
                            if a is not None and not _visit(a.annotation, in_fn):
                                return False
                        if not _visit(node.args.defaults, in_fn):
                            return False
                        if not _visit(node.args.kw_defaults, in_fn):
                            return False
                        if not _visit(node.returns, in_fn):
                            return False
                        if not _visit(getattr(node, "type_params", None), in_fn):
                            return False
                        if not _visit(node.body, True):
                            return False
                    finally:
                        stmt_stack.pop()
                    return True
                if isinstance(node, ast.Lambda):
                    if not _visit(node.args.defaults, in_fn):
                        return False
                    if not _visit(node.args.kw_defaults, in_fn):
                        return False
                    return _visit(node.body, True)
                if isinstance(node, ast.ClassDef):
                    stmt_stack.append(node)
                    try:
                        if not _visit(node.decorator_list, in_fn):
                            return False
                        if not _visit(node.bases, in_fn):
                            return False
                        if not _visit(node.keywords, in_fn):
                            return False
                        if not _visit(getattr(node, "type_params", None), in_fn):
                            return False
                        if not _visit(node.body, in_fn):
                            return False
                    finally:
                        stmt_stack.pop()
                    return True
                if isinstance(node, ast.Name):
                    if node.id == name:
                        sites.append((node.ctx, in_fn, stmt_stack[-1] if stmt_stack else None, node))
                    return True
                if isinstance(node, ast.Attribute):
                    if node.attr == name:
                        sites.append((node.ctx, in_fn, stmt_stack[-1] if stmt_stack else None, node))
                    return _visit(node.value, in_fn)
                pushed = isinstance(node, ast.stmt)
                if pushed:
                    stmt_stack.append(node)
                try:
                    for ch in ast.iter_child_nodes(node):
                        if not _visit(ch, in_fn):
                            return False
                finally:
                    if pushed:
                        stmt_stack.pop()
                return True

            if not _visit(root, False):
                return None
            return sites

        sites = _ref_sites(old_mod, ignore_imports=False)
        if sites is None:
            return None
        mod_lines: Set[int] = set()
        for ctx, in_fn, enc_stmt, ref_node in sites:
            if (
                isinstance(ctx, ast.Store)
                and enc_stmt is not None
                and enc_stmt.lineno == assign_lineno
                and isinstance(ref_node, ast.Name)
            ):
                continue
            if isinstance(ctx, (ast.Store, ast.Del)):
                return None
            if not in_fn or enc_stmt is None:
                return None
            s0, s1 = _stmt_span(enc_stmt)
            mod_lines.update(range(s0, s1 + 1))

        once_set = set(self.raw_once_lines.get(rel_path, ())) | set(self.import_trigger_lines.get(rel_path, {}).keys())
        if mod_lines & once_set:
            return None

        base = self._lines_cov(rel_path, mod_lines) if mod_lines else 0
        live = 0
        name_word_re = re.compile(rf"\b{re.escape(name)}\b")
        for f in self._importers_of(rel_path):
            f_src = self._read_src(f)
            if not f_src or not name_word_re.search(f_src):
                continue
            if f in self.file_to_livemask:
                live |= self.file_to_livemask[f]
                continue
            if f.endswith("__init__.py") or any(m in f_src for m in _DYN_MARKERS) or quoted_re.search(f_src):
                return None
            try:
                f_mod = ast.parse(f_src)
            except SyntaxError:
                return None
            for sub in ast.walk(f_mod):
                if isinstance(sub, ast.ImportFrom):
                    if any(a.name == name and a.asname and a.asname != name for a in sub.names):
                        return None
            f_sites = _ref_sites(f_mod, ignore_imports=True)
            if f_sites is None:
                return None
            f_lines: Set[int] = set()
            for ctx, in_fn, enc_stmt, _ in f_sites:
                if isinstance(ctx, (ast.Store, ast.Del)):
                    return None
                if not in_fn or enc_stmt is None:
                    return None
                s0, s1 = _stmt_span(enc_stmt)
                f_lines.update(range(s0, s1 + 1))
            f_once = set(self.raw_once_lines.get(f, ())) | set(self.import_trigger_lines.get(f, {}).keys())
            if f_lines & f_once:
                return None
            if f_lines:
                base |= self._lines_cov(f, f_lines)

        return base, live

    def _diff_scope_stmts(
        self,
        old_stmts: List[ast.stmt],
        new_stmts: List[ast.stmt],
        rel_path: str,
        cls_stack: List[str],
        old_mod: Optional[ast.Module],
        old_lines: List[str],
        new_lines: List[str],
        changed_classes: Optional[Set[str]] = None,
        mod_change_flag: Optional[List[bool]] = None,
    ) -> Tuple[int, int]:
        """Diff Module or ClassDef body statement list, returning (base_mask, live_mask)."""
        base = 0
        live = 0
        osigs = [_stmt_sig(s, old_lines) for s in old_stmts]
        nsigs = [_stmt_sig(s, new_lines) for s in new_stmts]
        sm = difflib.SequenceMatcher(None, osigs, nsigs, autojunk=False)
        old_counts = _count_target_assigns(old_stmts)
        new_counts = _count_target_assigns(new_stmts)
        dec_enabled = self._rule_enabled("decorator_import_time")

        def _mark_scope() -> None:
            self._marks += 1
            if cls_stack:
                if changed_classes is not None:
                    changed_classes.add(self._cls_key(rel_path, cls_stack))
            elif mod_change_flag is not None:
                mod_change_flag[0] = True

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for oi, nj in zip(range(i1, i2), range(j1, j2)):
                    os_ = old_stmts[oi]
                    ns_ = new_stmts[nj]
                    if _node_same_fast(os_, ns_, old_lines, new_lines):
                        continue
                    if isinstance(os_, ast.ClassDef) and isinstance(ns_, ast.ClassDef):
                        dec_diff = not _ast_list_eq(os_.decorator_list, ns_.decorator_list)
                        header_diff = (
                            not _ast_list_eq(os_.bases, ns_.bases)
                            or not _ast_list_eq(os_.keywords, ns_.keywords)
                            or dec_diff
                        )
                        cls_stack.append(os_.name)
                        cls_key = self._cls_key(rel_path, cls_stack)
                        if header_diff:
                            if changed_classes is not None:
                                changed_classes.add(cls_key)
                            if dec_diff and dec_enabled:
                                fb, fl = self._once_escalation(rel_path, old_mod)
                                base |= self._class_usage(cls_key) | fb
                                live |= fl
                                if self._trace:
                                    self._note(f"decorator:{rel_path}")
                            else:
                                cm = self._class_usage(cls_key)
                                if cm:
                                    base |= cm
                                    if self._trace:
                                        self._note(f"cls_usage:{cls_key}")
                                else:
                                    fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
                                    base |= fb
                                    live |= fl
                        sb, sl = self._diff_scope_stmts(
                            os_.body, ns_.body, rel_path, cls_stack, old_mod, old_lines, new_lines,
                            changed_classes, mod_change_flag,
                        )
                        base |= sb
                        live |= sl
                        cls_stack.pop()
                    elif isinstance(os_, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                        ns_, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        if not _ast_eq(os_, ns_):
                            _mark_scope()
                        s0, s1 = _stmt_span(os_)
                        fn_lines = range(s0, s1 + 1)
                        dec_diff = not _ast_list_eq(os_.decorator_list, ns_.decorator_list)
                        if dec_diff and dec_enabled:
                            narrow = self._try_narrow_decorator_removal(os_, ns_, rel_path)
                            if narrow is not None:
                                base |= narrow[0]
                                live |= narrow[1]
                            else:
                                cov = self._lines_cov(rel_path, fn_lines)
                                fb, fl = self._once_escalation(rel_path, old_mod)
                                base |= cov | fb
                                live |= fl
                                if self._trace:
                                    self._note(f"decorator:{rel_path}")
                        elif _fn_signature_changed(os_, ns_) or _fn_scope_shape(os_) != _fn_scope_shape(ns_):
                            itb, itl = (
                                self._import_trigger_mask(rel_path, fn_lines, os_)
                                if self._rule_enabled("import_trigger")
                                else (0, 0)
                            )
                            cov = self._lines_cov(rel_path, fn_lines)
                            if cov and self._touches_once_lines(rel_path, fn_lines, os_):
                                fb, fl = self._once_escalation(rel_path, old_mod)
                                base |= cov | fb | itb
                                live |= fl | itl
                            elif cov:
                                base |= cov | itb
                                live |= itl
                                if self._trace:
                                    self._note(f"cov:{rel_path}")
                            elif cls_stack:
                                ck = self._cls_key(rel_path, cls_stack)
                                cm = self._class_usage(ck)
                                if cm:
                                    base |= cm | itb
                                    live |= itl
                                    if self._trace:
                                        self._note(f"cls_usage:{ck}")
                                else:
                                    fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
                                    base |= fb | itb
                                    live |= fl | itl
                            else:
                                fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
                                base |= fb | itb
                                live |= fl | itl
                        else:
                            b, l = self._diff_fn_bodies(
                                os_.body, ns_.body, rel_path, cls_stack, os_, old_mod, old_lines, new_lines
                            )
                            base |= b
                            live |= l
                continue

            if tag == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1:
                os_ = old_stmts[i1]
                ns_ = new_stmts[j1]
                if _node_same_fast(os_, ns_, old_lines, new_lines):
                    continue
                _mark_scope()
                if (
                    dec_enabled
                    and isinstance(os_, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and isinstance(ns_, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and not _ast_list_eq(os_.decorator_list, ns_.decorator_list)
                ):
                    narrow = self._try_narrow_decorator_removal(os_, ns_, rel_path)
                    if narrow is not None:
                        base |= narrow[0]
                        live |= narrow[1]
                        continue
                    fb, fl = self._once_escalation(rel_path, old_mod)
                    base |= fb
                    live |= fl
                    if self._trace:
                        self._note(f"decorator:{rel_path}")

                assigned_old = _extract_single_assign(os_)
                assigned_new = _extract_single_assign(ns_)
                if assigned_old is not None and assigned_new is not None and assigned_old[0] == assigned_new[0]:
                    attr, ov = assigned_old
                    _, nv = assigned_new

                    if cls_stack:
                        cls_key = self._cls_key(rel_path, cls_stack)
                        # 1) Narrowest path: inner Lambda bodies inside a class dict/structure.
                        lambdas = _find_inner_lambdas_only(ov, nv)
                        if lambdas is not None:
                            lam_mask = 0
                            for lam in lambdas:
                                lam_mask |= self._lines_cov(rel_path, range(lam.lineno, (lam.end_lineno or lam.lineno) + 1))
                            if lam_mask:
                                base |= lam_mask
                                if self._trace:
                                    self._note(f"cov:{rel_path}")
                                continue

                        # 2) Tracked dict-key precision (when attr appears once in class and dict is tracked).
                        if (
                            attr in self.raw_dict_key_usage
                            and cls_key in self.tracked_dict_classes
                            and old_counts.get(attr, 0) == 1
                            and new_counts.get(attr, 0) == 1
                            and isinstance(ov, ast.Dict)
                            and isinstance(nv, ast.Dict)
                        ):
                            dk_diff = _diff_ast_dict(ov, nv)
                            if dk_diff is not None:
                                added_or_deleted, modified_existing = dk_diff
                                if added_or_deleted:
                                    cm = self._class_usage(cls_key)
                                    if cm:
                                        base |= cm
                                        if self._trace:
                                            self._note(f"cls_usage:{cls_key}")
                                    else:
                                        fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
                                        base |= fb
                                        live |= fl
                                elif modified_existing:
                                    dk_mask = self._dict_key_mask(attr, cls_key, "__all__")
                                    for kn in modified_existing:
                                        dk_mask |= self._dict_key_mask(attr, cls_key, kn)
                                    base |= dk_mask
                                    if (
                                        dk_mask == 0
                                        and self._rule_enabled("dict_zero_trust")
                                        and attr not in self.dict_value_import_reads
                                        and attr not in self.str_const_idents
                                        and attr not in self.dict_key_mismatch_attrs
                                        and all(
                                            isinstance(kn, str)
                                            and kn.startswith(("str:", "int:", "enum:", "tuple:", "cls:"))
                                            for kn in modified_existing
                                        )
                                    ):
                                        self._trusted_marks += 1
                                        if self._trace:
                                            self._note(f"dict_zero_trusted:{attr}")
                                    if self._trace:
                                        n_dk = dk_mask.bit_count()
                                        self._note(f"dict_key_zero:{attr}" if n_dk == 0 else f"dict_key:{attr}:{n_dk}")
                                    # Confirmed tracked dict with potentially 0 readers: do not inflate!
                                    continue
                                continue

                        # 3) Scalar class-attribute intersection via runtime reader coverage.
                        if (
                            attr.isupper()
                            and attr not in self.metaclass_attrs
                            and old_counts.get(attr, 0) == 1
                            and new_counts.get(attr, 0) == 1
                            and _is_simple_scalar_ast(ov)
                            and _is_simple_scalar_ast(nv)
                            and attr in self.attr_read_lines
                        ):
                            cu = self._class_usage(cls_key)
                            readers = self._attr_reader_mask(attr)
                            if cu and readers:
                                inter = cu & readers
                                base |= inter
                                if self._trace:
                                    self._note(f"scalar_attr:{attr}:{inter.bit_count()}")
                                continue
                    else:
                        # Top-level single assignment (Module scope):
                        lambdas = _find_inner_lambdas_only(ov, nv)
                        if lambdas is not None:
                            lam_mask = 0
                            for lam in lambdas:
                                lam_mask |= self._lines_cov(rel_path, range(lam.lineno, (lam.end_lineno or lam.lineno) + 1))
                            if lam_mask:
                                base |= lam_mask
                                if self._trace:
                                    self._note(f"cov:{rel_path}")
                                continue

                        if (
                            rel_path.startswith("sqlglot/typing/")
                            and attr == "EXPRESSION_METADATA"
                            and old_counts.get(attr, 0) == 1
                            and new_counts.get(attr, 0) == 1
                            and isinstance(ov, ast.Dict)
                            and isinstance(nv, ast.Dict)
                            and rel_path in self.dialect_typing_links
                        ):
                            dk_diff = _diff_ast_dict(ov, nv)
                            if dk_diff is not None:
                                added_or_deleted, modified_existing = dk_diff
                                link_clss = self.dialect_typing_links[rel_path]
                                if not added_or_deleted and modified_existing and link_clss and all(
                                    lc in self.tracked_dict_classes for lc in link_clss
                                ):
                                    dk_mask = 0
                                    for lc in link_clss:
                                        for kn in modified_existing:
                                            dk_mask |= self._dict_key_mask("EXPRESSION_METADATA", lc, kn)
                                    base |= dk_mask
                                    if self._trace:
                                        n_dk = dk_mask.bit_count()
                                        self._note("dict_key_zero:EXPRESSION_METADATA" if n_dk == 0 else f"dict_key:EXPRESSION_METADATA:{n_dk}")
                                    continue

                        if (
                            old_counts.get(attr, 0) == 1
                            and new_counts.get(attr, 0) == 1
                            and _is_pure_literal(ov)
                            and _is_pure_literal(nv)
                        ):
                            old_src_txt = "\n".join(old_lines)
                            res = self._module_const_readers(rel_path, attr, old_mod, old_src_txt, os_.lineno)
                            if res is not None:
                                base |= res[0]
                                live |= res[1]
                                self._trusted_marks += 1
                                if self._trace:
                                    self._note(f"module_const:{rel_path}:{attr}:{(res[0] | res[1]).bit_count()}")
                                continue

            # Filter pairwise AST-equal replacements before fallback!
            if tag == "replace" and (i2 - i1) == (j2 - j1):
                if all(_node_same_fast(old_stmts[oi], new_stmts[nj], old_lines, new_lines) for oi, nj in zip(range(i1, i2), range(j1, j2))):
                    continue

            _mark_scope()
            if cls_stack:
                cls_key = self._cls_key(rel_path, cls_stack)
                cm = self._class_usage(cls_key)
                lm = 0
                for oi in range(i1, i2):
                    s0, s1 = _stmt_span(old_stmts[oi])
                    lm |= self._lines_cov(rel_path, range(s0, s1 + 1))
                comb = cm | lm
                if comb:
                    base |= comb
                    if self._trace:
                        if cm:
                            self._note(f"cls_usage:{cls_key}")
                        if lm:
                            self._note(f"cov:{rel_path}")
                else:
                    fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
                    base |= fb
                    live |= fl
            else:
                has_import_edit = any(
                    isinstance(old_stmts[oi], (ast.Import, ast.ImportFrom)) for oi in range(i1, i2)
                ) or any(isinstance(new_stmts[nj], (ast.Import, ast.ImportFrom)) for nj in range(j1, j2))
                fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=has_import_edit)
                base |= fb
                live |= fl
                return base, live

        return base, live

    def _analyze_test_file(
        self,
        rel_path: str,
        status: str,
        hunks: List[Dict[str, Any]],
    ) -> Tuple[int, int]:
        file_live = self.file_to_livemask.get(rel_path, 0)
        imp_live = self._importer_test_livemask(rel_path)

        if status == "deleted":
            if self._trace:
                self._note(f"deleted:{rel_path}")
                self._note(f"test_file:{rel_path}")
            return 0, file_live | imp_live

        old_src, old_mod, old_lines = self._baseline_src_ast_lines(rel_path)
        new_src, ok = apply_hunks_in_memory(old_src, hunks)
        if not ok or old_mod is None:
            if self._trace:
                self._note(f"apply_fail:{rel_path}")
                self._note(f"test_file:{rel_path}")
            return 0, file_live | imp_live
        try:
            new_mod = ast.parse(new_src)
        except SyntaxError:
            if self._trace:
                self._note(f"syntax:{rel_path}")
                self._note(f"test_file:{rel_path}")
            return 0, file_live | imp_live
        if _encoding_cookie(old_src) != _encoding_cookie(new_src):
            if self._trace:
                self._note(f"syntax:{rel_path}")
                self._note(f"test_file:{rel_path}")
            return 0, file_live | imp_live
        if _ast_eq(old_mod, new_mod):
            fa = self._file_access(rel_path)
            if self._trace:
                self._note(f"fixture:{rel_path}" if fa else f"inert:{rel_path}")
            return fa, 0

        if self._trace:
            self._note(f"test_file:{rel_path}")

        new_lines = new_src.splitlines()
        base = self._file_access(rel_path)
        live = 0
        osigs = [_stmt_sig(s, old_lines) for s in old_mod.body]
        nsigs = [_stmt_sig(s, new_lines) for s in new_mod.body]
        sm = difflib.SequenceMatcher(None, osigs, nsigs, autojunk=False)

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "replace" and (i2 - i1) == (j2 - j1) and all(
                _node_same_fast(old_mod.body[oi], new_mod.body[nj], old_lines, new_lines)
                for oi, nj in zip(range(i1, i2), range(j1, j2))
            ):
                continue
            if tag != "equal":
                return base, live | file_live | imp_live

            for oi, nj in zip(range(i1, i2), range(j1, j2)):
                os_ = old_mod.body[oi]
                ns_ = new_mod.body[nj]
                if _node_same_fast(os_, ns_, old_lines, new_lines):
                    continue
                if isinstance(os_, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                    ns_, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    num = self.func_to_num.get((rel_path, os_.name))
                    if num is not None:
                        live |= 1 << num
                    else:
                        # Shared helper function inside a test module -> check line_cov across all callers.
                        if _fn_signature_changed(os_, ns_) or _fn_scope_shape(os_) != _fn_scope_shape(ns_):
                            s0, s1 = _stmt_span(os_)
                            bm = self._lines_cov(rel_path, range(s0, s1 + 1))
                        else:
                            bm, _ = self._diff_fn_bodies(
                                os_.body, ns_.body, rel_path, [], os_, old_mod, old_lines, new_lines, use_fallback=False
                            )
                        if bm:
                            base |= bm
                        else:
                            live |= file_live | imp_live
                elif isinstance(os_, ast.ClassDef) and isinstance(ns_, ast.ClassDef):
                    cls_live = self.class_to_livemask.get((rel_path, os_.name), 0)
                    header_diff = (
                        not _ast_list_eq(os_.bases, ns_.bases)
                        or not _ast_list_eq(os_.keywords, ns_.keywords)
                        or not _ast_list_eq(os_.decorator_list, ns_.decorator_list)
                    )
                    if header_diff:
                        live |= cls_live or (file_live | imp_live)
                        if imp_live and cls_live == 0:
                            live |= imp_live
                    csigs_o = [_stmt_sig(s, old_lines) for s in os_.body]
                    csigs_n = [_stmt_sig(s, new_lines) for s in ns_.body]
                    csm = difflib.SequenceMatcher(None, csigs_o, csigs_n, autojunk=False)
                    for ctag, ci1, ci2, cj1, cj2 in csm.get_opcodes():
                        if ctag == "equal":
                            for coi, cnj in zip(range(ci1, ci2), range(cj1, cj2)):
                                cos = os_.body[coi]
                                cns = ns_.body[cnj]
                                if _node_same_fast(cos, cns, old_lines, new_lines):
                                    continue
                                if isinstance(cos, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
                                    cns, (ast.FunctionDef, ast.AsyncFunctionDef)
                                ):
                                    mnum = self.method_to_num.get((rel_path, os_.name, cos.name))
                                    if mnum is not None:
                                        live |= 1 << mnum
                                    else:
                                        # Helper/setup method (e.g. Validator.*, setUp) -> cover via line_cov + fallback.
                                        if _fn_signature_changed(cos, cns) or _fn_scope_shape(cos) != _fn_scope_shape(cns):
                                            s0, s1 = _stmt_span(cos)
                                            bm = self._lines_cov(rel_path, range(s0, s1 + 1))
                                        else:
                                            bm, _ = self._diff_fn_bodies(
                                                cos.body, cns.body, rel_path, [os_.name], cos, old_mod, old_lines, new_lines, use_fallback=False
                                            )
                                        if bm:
                                            base |= bm
                                        elif cls_live:
                                            live |= cls_live
                                        else:
                                            live |= file_live | imp_live
                                else:
                                    live |= cls_live or (file_live | imp_live)
                        else:
                            if ctag == "replace" and (ci2 - ci1) == (cj2 - cj1) and all(
                                _node_same_fast(os_.body[coi], ns_.body[cnj], old_lines, new_lines)
                                for coi, cnj in zip(range(ci1, ci2), range(cj1, cj2))
                            ):
                                continue
                            for coi in range(ci1, ci2):
                                cos = os_.body[coi]
                                if isinstance(cos, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    mnum = self.method_to_num.get((rel_path, os_.name, cos.name))
                                    if mnum is not None:
                                        live |= 1 << mnum
                                        continue
                                    s0, s1 = _stmt_span(cos)
                                    bm = self._lines_cov(rel_path, range(s0, s1 + 1))
                                    if bm:
                                        base |= bm
                                        continue
                                live |= cls_live or (file_live | imp_live)
                            if ctag == "insert":
                                live |= cls_live or (file_live | imp_live)
                else:
                    return base, live | file_live | imp_live

        return base, live

    def _analyze_py_source(
        self,
        rel_path: str,
        status: str,
        hunks: List[Dict[str, Any]],
        added_paths: Set[str],
        deleted_paths: Set[str],
        raw_diff: str = "",
    ) -> Tuple[int, int]:
        self._last_ast_eq = False
        self._marks = 0
        self._trusted_marks = 0
        unc_enabled = self._rule_enabled("uncertainty_escalation")

        if status == "added":
            bm = self._dir_walker_tests(rel_path) | self._pkg_walker_tests(rel_path)
            lm = 0
            if (
                self._rule_enabled("added_module_narrow")
                and rel_path.startswith("sqlglot/")
                and not rel_path.endswith("/__init__.py")
                and rel_path != "sqlglot/__init__.py"
            ):
                stem = rel_path.rsplit("/", 1)[-1][:-3] if rel_path.endswith(".py") else rel_path.rsplit("/", 1)[-1]
                pkg = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
                same_pkg_other = any(
                    p != rel_path and (p.rsplit("/", 1)[0] if "/" in p else "") == pkg for p in added_paths
                )
                if (
                    stem not in self.known_words
                    and not same_pkg_other
                    and not (self._other_added_text and re.search(rf"\b{re.escape(stem)}\b", self._other_added_text))
                ):
                    if self._trace:
                        self._note(f"added_narrow:{rel_path}")
                    return bm | self._file_access(rel_path), 0
            if unc_enabled and rel_path.startswith("sqlglot/"):
                pkg = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
                lm |= self._pkg_sibling_lazy_livemask(pkg)
                if self._trace:
                    self._note(f"uncertainty:{rel_path}")
            if self._trace:
                self._note(f"added:{rel_path}")
            return bm, lm

        old_src, old_mod, old_lines = self._baseline_src_ast_lines(rel_path)
        if status == "deleted" or old_mod is None:
            fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
            if unc_enabled:
                if rel_path.startswith("sqlglot/"):
                    fl |= self._lazy_test_livemask(rel_path)
                if (fb | fl) == 0 and rel_path.startswith("sqlglot/"):
                    fl |= self.all_livemask
                if self._trace:
                    self._note(f"uncertainty:{rel_path}")
            if self._trace:
                self._note(f"deleted:{rel_path}" if status == "deleted" else f"uncertainty:{rel_path}")
            return fb, fl

        new_src, ok = apply_hunks_in_memory(old_src, hunks)
        if not ok:
            if self._trace:
                self._note(f"apply_fail:{rel_path}")
            fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
            if unc_enabled:
                fl |= self._lazy_test_livemask(rel_path)
                if (fb | fl) == 0:
                    fl |= self.all_livemask
                if self._trace:
                    self._note(f"uncertainty:{rel_path}")
            return fb, fl

        try:
            new_mod = ast.parse(new_src)
        except SyntaxError:
            if self._trace:
                self._note(f"syntax:{rel_path}")
            fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
            if unc_enabled:
                fl |= self._lazy_test_livemask(rel_path)
                if (fb | fl) == 0:
                    fl |= self.all_livemask
                if self._trace:
                    self._note(f"uncertainty:{rel_path}")
            return fb, fl

        if _encoding_cookie(old_src) != _encoding_cookie(new_src):
            if self._trace:
                self._note(f"syntax:{rel_path}")
            fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
            if unc_enabled:
                fl |= self._lazy_test_livemask(rel_path)
                if (fb | fl) == 0:
                    fl |= self.all_livemask
                if self._trace:
                    self._note(f"uncertainty:{rel_path}")
            return fb, fl

        new_lines = new_src.splitlines()
        neutral_kinds: Set[str] = set()
        if rel_path.startswith("sqlglot/"):
            nb, neutral_kinds = self._neutral_filter(
                rel_path, old_src, old_mod, new_mod, old_lines=old_lines, new_lines=new_lines
            )
            if neutral_kinds:
                new_mod = ast.Module(body=nb, type_ignores=getattr(new_mod, "type_ignores", []))

        base = self._file_access(rel_path)
        if len(old_mod.body) == len(new_mod.body) and all(
            _node_same_fast(os_, ns_, old_lines, new_lines) for os_, ns_ in zip(old_mod.body, new_mod.body)
        ):
            self._last_ast_eq = True
            if neutral_kinds:
                if self._trace:
                    self._note(f"neutral:{rel_path}:{'+'.join(sorted(neutral_kinds))}")
                return base, (
                    self._get_subprocess_livemask()
                    if neutral_kinds & {"stdlib_import_add", "import_reorder"}
                    else 0
                )
            if self._trace:
                self._note(f"fixture:{rel_path}" if base else f"inert:{rel_path}")
            return base, 0

        if rel_path.startswith("sqlglot/") and self._rule_enabled("semantic_noop"):
            allow_opswap = (
                self._rule_enabled("noop_opswap")
                and self._last_num_sections == 1
                and not _diff_touches_dunder_eq(raw_diff)
            )
            tc_old, al_old = _typing_guard_info(old_mod)
            tc_new, al_new = _typing_guard_info(new_mod)
            sem_ctx = _SemEqCtx(
                safe_sites=self.eq_safe_sites.get(rel_path, frozenset()),
                allow_opswap=allow_opswap,
                allow_alpha=self._rule_enabled("noop_alpha"),
                allow_ann=self._rule_enabled("noop_annotation"),
                allow_notop=self._rule_enabled("noop_notop"),
                allow_tc=self._rule_enabled("noop_type_checking"),
                allow_fstr=self._rule_enabled("noop_fstring"),
                future_ann=_has_future_annotations(old_mod) and _has_future_annotations(new_mod),
                has_from_tc=tc_old and tc_new,
                typing_aliases=frozenset(al_old & al_new),
                old_lines=old_lines,
                new_lines=new_lines,
            )
            if _sem_eq(old_mod, new_mod, sem_ctx) and sem_ctx.kinds:
                self._last_ast_eq = True
                extra_live = 0
                if "opswap" in sem_ctx.kinds and self.eq_sites_stats and not self.eq_sites_stats.get("child_records"):
                    extra_live = self._get_subprocess_livemask()
                if self._trace:
                    self._note(f"semantic_noop:{rel_path}:{'+'.join(sorted(sem_ctx.kinds))}")
                return base, extra_live

        live = 0
        # Always check deleted top-level symbols or broken local imports.
        if (_top_level_names(old_mod.body) - _top_level_names(new_mod.body)) or _has_broken_import(
            old_mod.body, new_mod.body, rel_path, self.src_root, added_paths, deleted_paths
        ):
            fb, fl = self._module_wide_fallback(rel_path, old_mod, include_importers=True)
            base |= fb
            live |= fl

        changed_classes: Set[str] = set()
        mod_change_flag = [False]
        db, dl = self._diff_scope_stmts(
            old_mod.body, new_mod.body, rel_path, [], old_mod, old_lines, new_lines,
            changed_classes=changed_classes, mod_change_flag=mod_change_flag,
        )
        tot_b = base | db
        tot_l = live | dl

        if rel_path.startswith("sqlglot/") and (tot_b | tot_l) == 0:
            if not (self._marks > 0 and self._marks == self._trusted_marks):
                eb, el = 0, 0
                if mod_change_flag[0] or not changed_classes:
                    eb, el = self._module_wide_fallback(rel_path, old_mod, include_importers=False)
                else:
                    for ck in sorted(changed_classes):
                        eb |= self._class_usage(ck)
                    if (eb | el) == 0:
                        eb, el = self._module_wide_fallback(rel_path, old_mod, include_importers=False)
                if eb or el:
                    self._empty_guard_pending[rel_path] = (eb, el, raw_diff)

        return tot_b, tot_l

    def _packed_cost_ms(self, sel: Set[int]) -> int:
        if not sel:
            return 0
        bins = pack_workers(sel, self.num_to_ms)
        if not bins:
            return 0
        return max(sum(self.num_to_ms[n] for n in b) for b in bins) + WORKER_COST_MS * len(bins)

    def _load_tpcds_schema(self) -> Dict[str, Dict[str, str]]:
        if self._tpcds_schema_cache is None:
            helpers_src = self._read_src("tests/helpers.py")
            tree = ast.parse(helpers_src)
            schema: Optional[Dict[str, Dict[str, str]]] = None
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "TPCDS_SCHEMA"
                ):
                    schema = ast.literal_eval(node.value)
                    break
            if not isinstance(schema, dict):
                raise RuntimeError("TPCDS_SCHEMA not found")
            self._tpcds_schema_cache = schema
        return self._tpcds_schema_cache

    @staticmethod
    def _parse_sql_fixture_pairs(path: Path) -> List[Tuple[bool, str]]:
        text = path.read_text("utf-8", errors="replace")
        filtered = "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))
        stmts = filtered.split(";")
        out: List[Tuple[bool, str]] = []
        for i in range(0, len(stmts), 2):
            if i + 1 < len(stmts):
                sql_lines = stmts[i].strip().split("\n")
                meta: Dict[str, str] = {}
                j = 0
                while j < len(sql_lines) and sql_lines[j].startswith("#"):
                    k, v = sql_lines[j].split(":", 1)
                    meta[k.lstrip("#").strip()] = v.strip()
                    j += 1
                sql = "\n".join(sql_lines[j:])
                ev = meta.get("execute", "")
                is_exec = bool(ev) and ev.strip().lower() in ("true", "1")
                out.append((is_exec, sql))
        return out

    def _get_tpcds_sqls(self) -> List[Tuple[bool, str]]:
        if self._tpcds_sqls_cache is None:
            self._tpcds_sqls_cache = self._parse_sql_fixture_pairs(
                self.src_root / "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
            )
        return self._tpcds_sqls_cache

    def _get_tpch_sqls(self) -> List[str]:
        if self._tpch_sqls_cache is None:
            self._tpch_sqls_cache = [
                sql
                for _, sql in self._parse_sql_fixture_pairs(
                    self.src_root / "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
                )
            ]
        return self._tpch_sqls_cache

    def _read_tpcds_table_csv(self, table: str, columns: Dict[str, str]) -> Tuple[Dict[str, str], List[tuple]]:
        dir_tpcds = self.src_root / "tests/fixtures/optimizer/tpc-ds"
        fn = dir_tpcds / f"{table}.csv.gz"
        with open(fn, "rb") as fb:
            is_gz = fb.read(2) == b"\x1f\x8b"
        opener = gzip.open(fn, "rt", newline="") if is_gz else open(fn, encoding="utf-8", newline="")
        with opener as f:
            reader = csv.reader(f, delimiter="|")
            rows: List[tuple] = []
            ctypes: List[Any] = []
            next(reader)
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(
                    tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row))
                )
            return columns, rows

    def _ensure_raw_tpcds_tables(self) -> Dict[str, Tuple[Dict[str, str], List[tuple]]]:
        if self._raw_tpcds_tables is None:
            schema = self._load_tpcds_schema()
            raw_tables: Dict[str, Tuple[Dict[str, str], List[tuple]]] = {}
            for table, columns in schema.items():
                cached = self._tpcds_table_cache.get(table)
                if cached is None:
                    cached = self._read_tpcds_table_csv(table, columns)
                    self._tpcds_table_cache[table] = cached
                raw_tables[table] = cached
            self._raw_tpcds_tables = raw_tables
        return self._raw_tpcds_tables

    def _load_single_tpcds_table(self, table: str) -> Tuple[Dict[str, str], List[tuple]]:
        cached = self._tpcds_table_cache.get(table)
        if cached is not None:
            return cached
        if self._raw_tpcds_tables is not None and table in self._raw_tpcds_tables:
            res = self._raw_tpcds_tables[table]
            self._tpcds_table_cache[table] = res
            return res
        schema = self._load_tpcds_schema()
        res = self._read_tpcds_table_csv(table, schema[table])
        self._tpcds_table_cache[table] = res
        return res

    def _preload_tpcds_tables_for_qids(
        self, qids: Sequence[int]
    ) -> Dict[str, Tuple[Dict[str, str], List[tuple]]]:
        schema = self._load_tpcds_schema()
        tpcds_sqls = self._get_tpcds_sqls()
        sql_blob = "\n".join(tpcds_sqls[qi][1] for qi in qids if 0 <= qi < len(tpcds_sqls)).lower()
        for tname in schema:
            if tname not in self._tpcds_table_cache and re.search(rf"\b{re.escape(tname.lower())}\b", sql_blob):
                self._load_single_tpcds_table(tname)
        return dict(self._tpcds_table_cache)

    def _tpcds_is_line_mode(self) -> bool:
        return not (
            self._tpcds_non_line
            or self._tpcds_unmapped_cov_line
            or (self._tpcds_cov_qmask == 0 and not self._tpcds_parse_hit)
        ) and bool(self.tpcds_s2_lines or self.tpcds_s3_lines or self.tpcds_s3_query_cov)

    def _classify_tpcds_stages(self, touched_sqlglot: Set[str]) -> Tuple[bool, bool]:
        """Return (touches_opt, touches_exec) using exact line-level stage coverage when available."""
        if not self.tpcds_parse_digests or not self.tpcds_opt_digests:
            return True, True
        force_exec = any(
            p.startswith("sqlglot/executor/") or p in _TPCDS_FORCE_EXEC_FILES for p in touched_sqlglot
        )
        if self._tpcds_is_line_mode():
            touches_exec = force_exec or (self._tpcds_s3_qmask != 0)
            touches_opt = touches_exec or self._tpcds_s2_hit
            return touches_opt, touches_exec
        touches_exec = force_exec or any(
            p.startswith("sqlglot/expressions/") or p in _TPCDS_EXEC_CORE_FILES for p in touched_sqlglot
        )
        touches_opt = touches_exec or any(
            p.startswith(("sqlglot/optimizer/", "sqlglot/typing/")) or p in _TPCDS_OPT_CORE_FILES
            for p in touched_sqlglot
        )
        return touches_opt, touches_exec

    def _probe_gen_memory(self, touched_sqlglot: Set[str]) -> bool:
        """Run MRO-filtered tracemalloc dialect.generator() leak probe inside forked child."""
        import tracemalloc
        import tests.test_generator  # noqa: F401
        from sqlglot.dialects import Dialect, Dialects

        loaded = []
        for name in Dialects:
            d = Dialect.get_or_raise(name)
            d.generator()
            loaded.append((name, d))

        touched_mods = {p[:-3].replace("/", ".") for p in touched_sqlglot if p.endswith(".py")}
        active_mods = touched_mods & set(sys.modules.keys())

        if touched_sqlglot and (not active_mods or all(_is_leaf_gen_mod(m) for m in active_mods)):
            target_dialects = [
                (name, d)
                for name, d in loaded
                if name == ""
                or any(
                    c.__module__ in active_mods
                    for root_cls in (type(d), d.generator_class, d.parser_class, d.tokenizer_class)
                    for c in root_cls.__mro__
                )
            ]
        else:
            target_dialects = loaded

        for _, dialect in target_dialects:
            for _ in range(100):
                dialect.generator()
            gc.collect()
            tracemalloc.start()
            try:
                before = tracemalloc.get_traced_memory()[0]
                for _ in range(1000):
                    dialect.generator()
                gc.collect()
                after = tracemalloc.get_traced_memory()[0]
            finally:
                tracemalloc.stop()
            if not (after - before < 1024):
                return False
        return True

    def _probe_tpcds(
        self,
        tpcds_sqls: List[Tuple[bool, str]],
        b_exec_qids: List[int],
        touches_opt: bool,
        touches_exec: bool,
        tpcds_schema: Dict[str, Dict[str, str]],
        raw_tables: Dict[str, Tuple[Dict[str, str], List[tuple]]],
        s3_exec_qids: Optional[Sequence[int]] = None,
    ) -> Tuple[bool, str]:
        """3-stage streaming deterministic probe for test_execute_tpcds inside forked child."""
        from sqlglot import exp, find_tables, parse_one, transpile
        from sqlglot.executor import execute
        from sqlglot.executor.python import Python
        from sqlglot.executor.table import Table
        from sqlglot.optimizer import optimize
        from sqlglot.schema import ensure_schema

        if Python().generator(identify=True, comments=False).generate(exp.var("scope")) != "scope":
            return False, "guard"

        built_tables: Dict[str, Any] = {}
        rt = dict(raw_tables)

        def _exec_one(qi: int, ast_q: Any, dk_sql: str) -> bool:
            tabs: Dict[str, Any] = {}
            for t in find_tables(ast_q):
                tname = t.name
                if tname not in built_tables:
                    pair = rt.get(tname)
                    if pair is None:
                        pair = self._load_single_tpcds_table(tname)
                        rt[tname] = pair
                    built_tables[tname] = Table(columns=pair[0], rows=pair[1])
                tabs[tname] = built_tables[tname]
            tbl = execute(ast_q, schema=tpcds_schema, tables=tabs)
            c_cols = [str(c) for c in tbl.columns]
            c_rows = [[_canon_cell(x) for x in r] for r in tbl.rows]
            return _canon_digest([dk_sql, c_cols, c_rows]) == self.tpcds_digests.get(qi)

        if not touches_opt:
            exec_set = set(self.tpcds_exec_qids)
            schema_obj: Any = None
            used_s2 = False
            used_s3 = False
            s3_ran = 0
            try:
                for qi, (_, sql) in enumerate(tpcds_sqls):
                    ast_q = pickle.loads(pickle.dumps(parse_one(sql)))
                    if not isinstance(ast_q, exp.Expr):
                        return False, "s1"
                    if qi in exec_set:
                        dk_sql = transpile(sql, write="duckdb")[0]
                        if _canon_digest([repr(ast_q), dk_sql]) == self.tpcds_parse_digests.get(qi):
                            continue
                        used_s2 = True
                        if schema_obj is None:
                            schema_obj = ensure_schema(tpcds_schema, dialect=None)
                        opt_q = optimize(ast_q.copy(), schema_obj, leave_tables_isolated=True, dialect=None)
                        if _canon_digest([repr(opt_q), dk_sql]) == self.tpcds_opt_digests.get(qi):
                            continue
                        used_s3 = True
                        s3_ran += 1
                        if s3_ran > self.max_s3_exec_qids:
                            return False, "s1->s3"
                        if not _exec_one(qi, ast_q, dk_sql):
                            return False, "s1->s3"
                return True, ("s1->s3" if used_s3 else ("s1->s2" if used_s2 else "s1"))
            except BaseException:
                return False, ("s1->s3" if used_s3 else ("s1->s2" if used_s2 else "s1"))

        if not touches_exec:
            used_s3 = False
            s3_ran = 0
            try:
                schema_obj = ensure_schema(tpcds_schema, dialect=None)
                for qi in b_exec_qids:
                    _, sql = tpcds_sqls[qi]
                    ast_q = pickle.loads(pickle.dumps(parse_one(sql)))
                    if not isinstance(ast_q, exp.Expr):
                        return False, "s2"
                    dk_sql = transpile(sql, write="duckdb")[0]
                    opt_q = optimize(ast_q.copy(), schema_obj, leave_tables_isolated=True, dialect=None)
                    if _canon_digest([repr(opt_q), dk_sql]) == self.tpcds_opt_digests.get(qi):
                        continue
                    used_s3 = True
                    s3_ran += 1
                    if s3_ran > self.max_s3_exec_qids:
                        return False, "s2->s3"
                    if not _exec_one(qi, ast_q, dk_sql):
                        return False, "s2->s3"
                exec_set = set(b_exec_qids)
                for qi, (_, sql) in enumerate(tpcds_sqls):
                    if qi in exec_set:
                        continue
                    ast_q = pickle.loads(pickle.dumps(parse_one(sql)))
                    if not isinstance(ast_q, exp.Expr):
                        return False, ("s2->s3" if used_s3 else "s2")
                return True, ("s2->s3" if used_s3 else "s2")
            except BaseException:
                return False, ("s2->s3" if used_s3 else "s2")

        s3_set = set(b_exec_qids if s3_exec_qids is None else s3_exec_qids)
        schema_obj = None
        used_s2 = False
        s3_ran = 0
        fail_stage = f"s3({len(s3_set)})" if len(s3_set) < len(b_exec_qids) else "s3"
        try:
            for qi in b_exec_qids:
                _, sql = tpcds_sqls[qi]
                ast_q = pickle.loads(pickle.dumps(parse_one(sql)))
                if not isinstance(ast_q, exp.Expr):
                    return False, fail_stage
                dk_sql = transpile(sql, write="duckdb")[0]
                if qi in s3_set:
                    s3_ran += 1
                    if s3_ran > self.max_s3_exec_qids:
                        return False, ("s2+s3" if used_s2 else fail_stage)
                    if not _exec_one(qi, ast_q, dk_sql):
                        return False, ("s2+s3" if used_s2 else fail_stage)
                else:
                    if not self._tpcds_s2_hit and _canon_digest([repr(ast_q), dk_sql]) == self.tpcds_parse_digests.get(qi):
                        continue
                    used_s2 = True
                    if schema_obj is None:
                        schema_obj = ensure_schema(tpcds_schema, dialect=None)
                    opt_q = optimize(ast_q.copy(), schema_obj, leave_tables_isolated=True, dialect=None)
                    if _canon_digest([repr(opt_q), dk_sql]) == self.tpcds_opt_digests.get(qi):
                        continue
                    s3_ran += 1
                    if s3_ran > self.max_s3_exec_qids:
                        return False, "s2+s3"
                    if not _exec_one(qi, ast_q, dk_sql):
                        return False, "s2+s3"
            exec_set = set(b_exec_qids)
            for qi, (_, sql) in enumerate(tpcds_sqls):
                if qi in exec_set:
                    continue
                ast_q = pickle.loads(pickle.dumps(parse_one(sql)))
                if not isinstance(ast_q, exp.Expr):
                    return False, ("s2+s3" if used_s2 else fail_stage)
            return True, fail_stage
        except BaseException:
            return False, ("s2+s3" if used_s2 else fail_stage)

    def _probe_fast_tests(
        self,
        fast_cands: Sequence[int],
        touched_sqlglot: Set[str],
        worker_already_alive: bool = False,
    ) -> List[int]:
        """Run fast unittest methods in-process inside forked child; return strictly passing test numbers."""
        touched_stems = {p[:-3].rsplit("/", 1)[-1] for p in touched_sqlglot if p.endswith(".py")}
        by_cls: Dict[Tuple[str, str], List[Tuple[int, str]]] = defaultdict(list)
        for num in fast_cands:
            parts = self.num_to_parts[num]
            if parts is None:
                continue
            by_cls[(parts[0], parts[1])].append((num, parts[2]))

        sorted_groups: List[Tuple[Tuple[str, str], List[Tuple[int, str]]]] = []
        for cls_key, items in by_cls.items():
            items.sort(key=lambda it: (self.num_to_ms[it[0]], it[1]))
            sorted_groups.append((cls_key, items))
        sorted_groups.sort(
            key=lambda kv: (
                0 if kv[0][0][:-3].rsplit("/", 1)[-1].removeprefix("test_") in touched_stems else 1,
                sum(self.num_to_ms[n] for n, _ in kv[1]),
                kv[0][0],
                kv[0][1],
            )
        )

        any_kept = worker_already_alive
        passed: List[int] = []
        for (tfile, cls_name), items in sorted_groups:
            if any_kept and all(self.num_to_ms[n] < 400 for n, _ in items):
                continue
            try:
                mod = importlib.import_module(tfile[:-3].replace("/", "."))
                cls = getattr(mod, cls_name)
            except BaseException:
                any_kept = True
                continue
            if getattr(cls, "__unittest_skip__", False):
                any_kept = True
                continue

            is_opt = tfile == "tests/test_optimizer.py" and cls_name == "TestOptimizer"
            is_exec = tfile == "tests/test_executor.py" and cls_name == "TestExecutor"
            orig_schema: Any = None
            orig_afe: Any = None
            try:
                if is_opt:
                    import sqlglot
                    from sqlglot.schema import MappingSchema

                    orig_schema = getattr(sqlglot, "schema", None)
                    sqlglot.schema = MappingSchema()
                    cls.pool = _SyncPoolExecutor()
                    orig_afe = getattr(mod, "assert_frame_equal", None)
                elif is_exec:
                    cls.tpch_conn = _MissingStubObj("TestExecutor.tpch_conn")
                    cls.tpcds_conn = _MissingStubObj("TestExecutor.tpcds_conn")
                    cls.tpch_tables = _MissingStubObj("TestExecutor.tpch_tables")
                    cls.tpcds_tables = _MissingStubObj("TestExecutor.tpcds_tables")
                else:
                    cls.setUpClass()
            except BaseException:
                any_kept = True
                continue

            try:
                for num, meth_name in items:
                    if any_kept and self.num_to_ms[num] < 400:
                        continue
                    try:
                        tc = cls(meth_name)
                        if getattr(tc, "__unittest_skip__", False) or getattr(
                            getattr(cls, meth_name, None), "__unittest_skip__", False
                        ):
                            any_kept = True
                            continue
                        ver: Optional[_ConnVerifier] = None
                        if is_opt:
                            exp_digs = self.opt_conn_digests_by_num.get(num)
                            if exp_digs is not None:
                                ver = _ConnVerifier(exp_digs)
                                cls.conn = ver
                                mod.assert_frame_equal = ver.verify_pair
                            else:
                                cls.conn = _MissingStubObj("TestOptimizer.conn")
                                mod.assert_frame_equal = _MissingStubObj("assert_frame_equal")
                        res = _StrictTestResult()
                        tc.run(res)
                        ok = (
                            res.testsRun == 1
                            and not res.any_bad
                            and not res.failures
                            and not res.errors
                            and not res.skipped
                            and res.wasSuccessful()
                        )
                        if ok and ver is not None:
                            ok = (
                                ver.matched == ver.total_expected
                                and len(ver.calls) == 2 * ver.total_expected
                            )
                        if ok:
                            passed.append(num)
                        else:
                            any_kept = True
                    except BaseException:
                        any_kept = True
                        continue
            finally:
                if is_opt:
                    try:
                        import sqlglot

                        sqlglot.schema = orig_schema
                        if orig_afe is not None:
                            mod.assert_frame_equal = orig_afe
                    except BaseException:
                        pass
                elif not is_exec:
                    try:
                        cls.tearDownClass()
                    except BaseException:
                        pass
                    try:
                        cls.doClassCleanups()
                    except BaseException:
                        pass
        return passed

    def _ensure_compiled_rel(self, rel: str) -> None:
        if rel not in self._code_cache and (self.src_root / rel).is_file():
            txt = self._read_src(rel)
            self._code_cache[rel] = compile(txt, str(self.src_root / rel), "exec")

    def _ensure_prewarmed_sqlglot(self) -> None:
        if self._prewarm_mods is not None:
            return
        for m in [
            k
            for k in list(sys.modules)
            if k == "sqlglot" or k == "tests" or k.startswith(("sqlglot.", "tests."))
        ]:
            del sys.modules[m]
        src_str = str(self.src_root)
        if not sys.path or sys.path[0] != src_str:
            if src_str in sys.path:
                sys.path.remove(src_str)
            sys.path.insert(0, src_str)
        lg = logging.getLogger("sqlglot")
        prev_d = lg.disabled
        lg.disabled = True
        try:
            import sqlglot  # noqa: F401
            import tests.dialects.test_dialect  # noqa: F401
            from sqlglot.dialects.dialect import Dialect
        finally:
            lg.disabled = prev_d
        self._prewarm_mods = frozenset(
            m for m in sys.modules if m == "sqlglot" or m == "tests" or m.startswith(("sqlglot.", "tests."))
        )
        self._prewarm_dialects = dict(Dialect._classes)

    def _maybe_run_dynamic_probes(self, sel: Set[int], sections: List[Dict[str, Any]]) -> Set[int]:
        if not (self.src_root / "sqlglot/__init__.py").is_file():
            return sel
        patched: Dict[str, bytes] = {}
        deleted: Set[str] = set()
        touched_sqlglot: Set[str] = set()
        has_non_sqlglot_change = False
        has_struct_sqlglot_change = False

        for sec in sections:
            if sec.get("binary"):
                return sel
            rp = sec.get("rel_path", "")
            ap = sec.get("a_path", "")
            status = sec.get("status", "modified")
            is_rename = bool(sec.get("renamed") or (ap and rp and ap != rp))
            if not rp:
                continue
            fname = rp.rsplit("/", 1)[-1]
            ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
            is_inert = ext in _INERT_EXTS or fname in _INERT_NAMES or rp.startswith(("docs/", ".github/"))
            if is_inert and not (is_rename and ap.startswith("sqlglot/")):
                continue

            if is_rename and ap != rp:
                if ap.startswith("sqlglot/") and ap.endswith(".py"):
                    deleted.add(ap)
                    touched_sqlglot.add(ap)
                    has_struct_sqlglot_change = True
                elif not (ap.startswith("tests/fixtures/") and "tpc-ds" not in ap and "tpc-h" not in ap):
                    af = ap.rsplit("/", 1)[-1]
                    ae = ("." + af.rsplit(".", 1)[-1].lower()) if "." in af else ""
                    if ae not in _INERT_EXTS and af not in _INERT_NAMES and not ap.startswith(("docs/", ".github/")):
                        return sel
                else:
                    has_non_sqlglot_change = True
                if rp.startswith("sqlglot/") and rp.endswith(".py"):
                    old_txt = self._read_src(ap)
                    if not old_txt and not (self.src_root / ap).is_file():
                        return sel
                    ns, ok = apply_hunks_in_memory(old_txt, sec["hunks"])
                    if not ok:
                        return sel
                    patched[rp] = ns.encode("utf-8")
                    touched_sqlglot.add(rp)
                    has_struct_sqlglot_change = True
                elif not (is_inert or (rp.startswith("tests/fixtures/") and "tpc-ds" not in rp and "tpc-h" not in rp)):
                    return sel
                elif not is_inert:
                    has_non_sqlglot_change = True
                continue

            if rp.startswith("sqlglot/") and rp.endswith(".py"):
                touched_sqlglot.add(rp)
                if status == "deleted":
                    deleted.add(rp)
                    has_struct_sqlglot_change = True
                elif status == "added":
                    ns, ok = apply_hunks_in_memory("", sec["hunks"])
                    if not ok:
                        return sel
                    patched[rp] = ns.encode("utf-8")
                    has_struct_sqlglot_change = True
                else:
                    old_txt = self._read_src(rp)
                    if not old_txt and not (self.src_root / rp).is_file():
                        return sel
                    ns, ok = apply_hunks_in_memory(old_txt, sec["hunks"])
                    if not ok:
                        return sel
                    patched[rp] = ns.encode("utf-8")
            elif rp.startswith("tests/fixtures/") and "tpc-ds" not in rp and "tpc-h" not in rp:
                has_non_sqlglot_change = True
                continue
            else:
                return sel

        if deleted:
            has_struct_sqlglot_change = True

        sim_cur = set(sel)
        eff_fast_b = (
            min(self.probe_cpu_budget_s, self._fast_budget_env)
            if self._fast_budget_env is not None
            else self.probe_cpu_budget_s
        )
        eff_s3_b = min(
            self.probe_cpu_budget_s,
            self._s3_budget_env if self._s3_budget_env is not None else self.probe_cpu_budget_s * (15.0 / 45.0),
        )
        rem_total = max(0.0, self.probe_cpu_budget_s - self.total_probe_cpu_s)
        rem_p1 = min(rem_total, max(0.0, eff_fast_b - self.fast_probe_cpu_s))
        rem_s3 = min(rem_total, max(0.0, eff_s3_b - self.s3_probe_cpu_s))

        want_a = False
        want_b = False
        want_c = False
        want_d = False
        want_e = False
        require_a_for_b = False
        require_a_for_d = False
        require_b_for_d = False
        require_prev_for_e = False
        worker_alive_after_e = False
        b_exec_qids: List[int] = []
        s3_exec_qids: List[int] = []
        fast_cands: List[int] = []
        touches_opt = True
        touches_exec = True
        est_b = 0.0
        est_d = 0.0
        p1_est = 0.0
        s3_est = 0.0

        if (
            self.max_nodes_num is not None
            and self.max_nodes_num in sim_cur
            and self.max_nodes_files
            and not (touched_sqlglot & self.max_nodes_files)
            and rem_p1 >= 0.25
        ):
            sav_a = self._packed_cost_ms(sim_cur) - self._packed_cost_ms(sim_cur - {self.max_nodes_num})
            if sav_a > 3000:
                want_a = True
                p1_est += 0.20
                sim_cur.remove(self.max_nodes_num)

        if self.tpcds_num is not None and self.tpcds_num in sim_cur and self.tpcds_digests:
            is_line_mode = self._tpcds_is_line_mode()
            if not is_line_mode:
                b_exec_qids = list(self.tpcds_exec_qids)
            else:
                b_exec_qids = [q for q in self.tpcds_exec_qids if (self._tpcds_cov_qmask >> q) & 1]
            touches_opt, touches_exec = self._classify_tpcds_stages(touched_sqlglot)
            sav_b = (self._packed_cost_ms(sim_cur) - self._packed_cost_ms(sim_cur - {self.tpcds_num})) / 1000.0
            if not touches_opt or not touches_exec:
                if not touches_opt:
                    est_b = (0.0 if want_a else 0.20) + 0.65
                else:
                    est_b = (0.0 if want_a else 0.20) + 0.25 + 0.052 * len(b_exec_qids)
                thresh_b = max(3.0, est_b + 1.5)
                if sav_b > thresh_b and rem_p1 >= p1_est + est_b and rem_total >= p1_est + est_b:
                    want_b = True
                    p1_est += est_b
                    if want_a:
                        sav_b_no_a = (self._packed_cost_ms(sel) - self._packed_cost_ms(sel - {self.tpcds_num})) / 1000.0
                        require_a_for_b = sav_b_no_a <= thresh_b
                    sim_cur.remove(self.tpcds_num)
            else:
                force_exec = any(
                    p.startswith("sqlglot/executor/") or p in _TPCDS_FORCE_EXEC_FILES for p in touched_sqlglot
                )
                if is_line_mode and not force_exec and self._tpcds_s3_qmask != 0:
                    s3_exec_qids = [q for q in b_exec_qids if (self._tpcds_s3_qmask >> q) & 1]
                else:
                    s3_exec_qids = list(b_exec_qids)
                n_s3 = len(s3_exec_qids)
                n_s2_rem = max(0, len(b_exec_qids) - n_s3)
                if 0 < n_s3 <= self.max_s3_exec_qids:
                    est_b = (0.0 if want_a else 0.20) + 0.18 + 0.205 * n_s3 + 0.052 * n_s2_rem + 0.08
                    thresh_b = max(4.0, est_b + 3.0)
                    if sav_b > thresh_b and rem_s3 >= est_b and rem_total >= p1_est + est_b:
                        want_b = True
                        s3_est = est_b
                        if want_a:
                            sav_b_no_a = (self._packed_cost_ms(sel) - self._packed_cost_ms(sel - {self.tpcds_num})) / 1000.0
                            require_a_for_b = sav_b_no_a <= thresh_b
                        sim_cur.remove(self.tpcds_num)

        if self.gen_mem_num is not None and self.gen_mem_num in sim_cur:
            is_core_gm = not touched_sqlglot or not all(
                p.startswith(("sqlglot/optimizer/", "sqlglot/executor/"))
                or _is_leaf_gen_mod(p[:-3].replace("/", "."))
                for p in touched_sqlglot
            )
            est_d = (0.0 if (want_a or want_b) else 0.10) + (1.25 if is_core_gm else 0.30)
            sav_d = (self._packed_cost_ms(sim_cur) - self._packed_cost_ms(sim_cur - {self.gen_mem_num})) / 1000.0
            thresh_d = max(3.0, est_d + 1.0)
            if sav_d > thresh_d and rem_p1 >= p1_est + est_d and rem_total >= p1_est + s3_est + est_d:
                want_d = True
                p1_est += est_d
                if want_a:
                    sav_d_no_a = (self._packed_cost_ms(sel) - self._packed_cost_ms(sel - {self.gen_mem_num})) / 1000.0
                    require_a_for_d = sav_d_no_a <= thresh_d
                if want_b:
                    sel_with_b = set(sel)
                    if want_a and self.max_nodes_num is not None:
                        sel_with_b.discard(self.max_nodes_num)
                    sav_d_no_b = (
                        self._packed_cost_ms(sel_with_b) - self._packed_cost_ms(sel_with_b - {self.gen_mem_num})
                    ) / 1000.0
                    require_b_for_d = sav_d_no_b <= thresh_d
                sim_cur.remove(self.gen_mem_num)

        if self.tpch_num is not None and self.tpch_num in sim_cur and self.tpch_digests:
            sav_c = self._packed_cost_ms(sim_cur) - self._packed_cost_ms(sim_cur - {self.tpch_num})
            if sav_c > 2500 and rem_p1 >= p1_est + 0.20 and rem_total >= p1_est + s3_est + 0.20:
                want_c = True
                p1_est += 0.20
                sim_cur.remove(self.tpch_num)

        if self._rule_enabled("fast_test_probe") and not has_non_sqlglot_change:
            excl = self._fast_unprobed_nums | (
                self._pkg_dir_walker_nums if has_struct_sqlglot_change else frozenset()
            )
            fast_cands = sorted(n for n in sim_cur if n not in excl)
            sum_dur_s = sum(self.num_to_ms[n] for n in fast_cands) / 1000.0
            if 1 <= len(fast_cands) <= 65 and sum_dur_s <= 3.0:
                n_files = len({self.num_to_parts[n][0] for n in fast_cands if self.num_to_parts[n] is not None})
                fork_ovh = 0.0 if (want_a or want_b or want_c or want_d) else 0.09
                est_e = fork_ovh + 0.015 * n_files + 1.15 * sum_dur_s
                sav_e = (self._packed_cost_ms(sim_cur) - self._packed_cost_ms(sim_cur - set(fast_cands))) / 1000.0
                if (
                    sav_e >= 1.8
                    and est_e <= 0.48 * sav_e
                    and rem_p1 >= p1_est + est_e
                    and rem_total >= p1_est + s3_est + est_e
                ):
                    want_e = True
                    p1_est += est_e
                    worker_alive_after_e = bool(sim_cur - set(fast_cands))
                    sav_e_alone = (self._packed_cost_ms(sel) - self._packed_cost_ms(sel - set(fast_cands))) / 1000.0
                    require_prev_for_e = (want_a or want_b or want_c or want_d) and (
                        sav_e_alone < 1.8 or est_e > 0.48 * sav_e_alone
                    )

        if not (want_a or want_b or want_c or want_d or want_e):
            return sel

        touched_mod_names = {
            (p[:-12] if p.endswith("/__init__.py") else p[:-3]).replace("/", ".")
            for p in touched_sqlglot
            if p.endswith(".py")
        }
        cpu0 = _total_cpu_seconds()
        try:
            self._ensure_prewarmed_sqlglot()
            can_reuse_prewarm = bool(
                self._prewarm_mods
                and not has_struct_sqlglot_change
                and not (touched_mod_names & self._prewarm_mods)
            )
            if want_e:
                if not can_reuse_prewarm:
                    self._ensure_compiled_rel("tests/__init__.py")
                    self._ensure_compiled_rel("tests/helpers.py")
                    self._ensure_compiled_rel("tests/dialects/__init__.py")
                    self._ensure_compiled_rel("tests/dialects/test_dialect.py")
                for tfile in {self.num_to_parts[n][0] for n in fast_cands if self.num_to_parts[n] is not None}:
                    self._ensure_compiled_rel(tfile)
            tpcds_sqls = self._get_tpcds_sqls() if want_b else []
            tpcds_schema = self._load_tpcds_schema() if want_b else {}
            raw_tables = (
                self._preload_tpcds_tables_for_qids(s3_exec_qids)
                if (want_b and touches_exec and s3_exec_qids)
                else {}
            )
            tpch_sqls = self._get_tpch_sqls() if want_c else []
        except Exception:
            dt = max(0.0, _total_cpu_seconds() - cpu0)
            self.last_probe_cpu_s = dt
            self.total_probe_cpu_s += dt
            self.fast_probe_cpu_s += dt
            return sel

        sys.stdout.flush()
        sys.stderr.flush()
        r_fd, w_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r_fd)
            res: Dict[str, Any] = {}
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                if devnull > 2:
                    os.close(devnull)
                sys.dont_write_bytecode = True
                os.environ["SKIP_INTEGRATION"] = "0"
                signal.alarm(30)

                if can_reuse_prewarm and self._prewarm_mods:
                    for mod_name in [
                        m
                        for m in list(sys.modules)
                        if (m == "sqlglot" or m.startswith("sqlglot.") or m == "tests" or m.startswith("tests."))
                        and m not in self._prewarm_mods
                    ]:
                        del sys.modules[mod_name]
                    from sqlglot.dialects.dialect import Dialect

                    Dialect._classes.clear()
                    Dialect._classes.update(self._prewarm_dialects)
                else:
                    for mod_name in [
                        m
                        for m in list(sys.modules)
                        if m == "sqlglot" or m.startswith("sqlglot.") or m == "tests" or m.startswith("tests.")
                    ]:
                        del sys.modules[mod_name]
                gc.freeze()

                src_str = str(self.src_root)
                if src_str not in sys.path:
                    sys.path.insert(0, src_str)
                sys.meta_path.insert(0, PatchOverlayFinder(self.src_root, patched, deleted, self._code_cache))

                if want_a:
                    try:
                        import tests.test_parser  # noqa: F401
                        from sqlglot import exp, parse_one
                        from sqlglot.errors import ParseError

                        assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
                        cols500 = [f"col{i}" for i in range(500)]
                        assert isinstance(
                            parse_one(f"SELECT {','.join(cols500)} FROM t", max_nodes=1_000_000),
                            exp.Select,
                        )
                        expr = "x" + "=x" * 10
                        big_sql = f"SELECT {','.join([expr] * 10)}"
                        try:
                            parse_one(big_sql, max_nodes=50)
                            raise AssertionError("expected ParseError for max_nodes=50")
                        except ParseError as exc:
                            assert "(50)" in str(exc) and "Maximum number of AST nodes" in str(exc)
                        try:
                            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
                            raise AssertionError("expected ParseError for max_nodes=5")
                        except ParseError:
                            pass
                        assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)
                        res["a"] = True
                    except BaseException:
                        res["a"] = False

                if want_d and not require_b_for_d and not (require_a_for_d and res.get("a") is not True):
                    try:
                        res["d"] = self._probe_gen_memory(touched_sqlglot)
                    except BaseException:
                        res["d"] = False

                if want_b and not (require_a_for_b and res.get("a") is not True):
                    try:
                        b_ok, b_stage = self._probe_tpcds(
                            tpcds_sqls,
                            b_exec_qids,
                            touches_opt,
                            touches_exec,
                            tpcds_schema,
                            raw_tables,
                            s3_exec_qids=s3_exec_qids if touches_exec else None,
                        )
                        res["b"] = b_ok
                        res["b_stage"] = b_stage
                    except BaseException:
                        res["b"] = False
                        res["b_stage"] = "err"

                if want_d and require_b_for_d and res.get("b") is True and not (require_a_for_d and res.get("a") is not True):
                    try:
                        gc.freeze()
                        res["d"] = self._probe_gen_memory(touched_sqlglot)
                    except BaseException:
                        res["d"] = False

                if want_c:
                    try:
                        from sqlglot import parse_one

                        c_ok = True
                        for qi, sql in enumerate(tpch_sqls):
                            ast_q = pickle.loads(pickle.dumps(parse_one(sql)))
                            if _canon_digest(ast_q.sql()) != self.tpch_digests[qi]:
                                c_ok = False
                                break
                        res["c"] = c_ok
                    except BaseException:
                        res["c"] = False

                if want_e:
                    prev_ok = (
                        (not want_a or res.get("a") is True)
                        and (not want_b or res.get("b") is True)
                        and (not want_c or res.get("c") is True)
                        and (not want_d or res.get("d") is True)
                    )
                    if not require_prev_for_e or prev_ok:
                        try:
                            gc.freeze()
                            _install_fast_probe_sandbox()
                            alive_now = bool(
                                worker_alive_after_e
                                or (want_a and res.get("a") is not True)
                                or (want_b and res.get("b") is not True)
                                or (want_c and res.get("c") is not True)
                                or (want_d and res.get("d") is not True)
                            )
                            res["e_ran"] = True
                            res["e_pass"] = self._probe_fast_tests(fast_cands, touched_sqlglot, alive_now)
                        except BaseException:
                            res["e_ran"] = False
                            res["e_pass"] = []
            except BaseException:
                res = {}

            try:
                os.write(w_fd, json.dumps(res).encode("ascii"))
            except BaseException:
                pass
            try:
                os.close(w_fd)
            except BaseException:
                pass
            os._exit(0)

        os.close(w_fd)
        chunks: List[bytes] = []
        deadline = time.monotonic() + 31.0
        timed_out = False
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0.0:
                timed_out = True
                break
            ready, _, _ = select.select([r_fd], [], [], rem)
            if not ready:
                timed_out = True
                break
            b = os.read(r_fd, 65536)
            if not b:
                break
            chunks.append(b)
        if timed_out:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.close(r_fd)
        os.waitpid(pid, 0)

        dt_cpu = max(0.0, _total_cpu_seconds() - cpu0)
        self.last_probe_cpu_s = dt_cpu
        self.total_probe_cpu_s += dt_cpu
        probe_res: Dict[str, Any] = {}
        if not timed_out and chunks:
            try:
                probe_res = json.loads(b"".join(chunks).decode("ascii"))
            except Exception:
                probe_res = {}

        if s3_est > 0.0 and not (require_a_for_b and probe_res.get("a") is not True):
            s3_share = dt_cpu * (s3_est / max(1e-6, p1_est + s3_est))
            self.s3_probe_cpu_s += s3_share
            self.fast_probe_cpu_s += max(0.0, dt_cpu - s3_share)
        else:
            self.fast_probe_cpu_s += dt_cpu

        out = set(sel)
        if want_a:
            if probe_res.get("a") is True and self.max_nodes_num is not None:
                out.discard(self.max_nodes_num)
                if self._trace:
                    self._note("probe_drop:max_nodes")
            elif self._trace:
                self._note("probe_keep:max_nodes")
        if want_b and not (require_a_for_b and probe_res.get("a") is not True):
            b_stage = str(probe_res.get("b_stage") or "s3")
            if probe_res.get("b") is True and self.tpcds_num is not None:
                out.discard(self.tpcds_num)
                if self._trace:
                    self._note(f"probe_drop:tpcds:{b_stage}:{len(b_exec_qids)}")
            elif self._trace:
                self._note(f"probe_keep:tpcds:{b_stage}:{len(b_exec_qids)}")
        if want_c:
            if probe_res.get("c") is True and self.tpch_num is not None:
                out.discard(self.tpch_num)
                if self._trace:
                    self._note("probe_drop:tpch")
            elif self._trace:
                self._note("probe_keep:tpch")
        if (
            want_d
            and not (require_a_for_d and probe_res.get("a") is not True)
            and not (require_b_for_d and probe_res.get("b") is not True)
        ):
            if probe_res.get("d") is True and self.gen_mem_num is not None:
                out.discard(self.gen_mem_num)
                if self._trace:
                    self._note("probe_drop:gen_mem")
            elif self._trace:
                self._note("probe_keep:gen_mem")
        if want_e and probe_res.get("e_ran") is True:
            cset = set(fast_cands)
            e_pass = [int(x) for x in probe_res.get("e_pass", ()) if int(x) in cset]
            for n in e_pass:
                out.discard(n)
            if self._trace:
                if e_pass:
                    self._note(f"probe_drop:fast:{len(e_pass)}/{len(fast_cands)}")
                else:
                    self._note(f"probe_keep:fast:0/{len(fast_cands)}")

        return out

    def select_commit(self, patch_text: str, apply_empty_guard: bool = True) -> Set[int]:
        """Select all potentially impacted test numbers for a single commit patch."""
        self._trace = TRACE or (os.environ.get("BLAST_TRACE") == "1")
        self.reasons = []
        self._reason_seen = set()
        self._empty_guard_pending = {}
        self.last_probe_cpu_s = 0.0
        self._tpcds_cov_qmask = 0
        self._tpcds_parse_hit = False
        self._tpcds_unmapped_cov_line = False
        self._tpcds_non_line = False
        self._tpcds_s2_hit = False
        self._tpcds_s3_qmask = 0

        base_mask = 0
        live_mask = 0
        sections = parse_git_diff(patch_text)
        self._last_num_sections = len(sections)
        unc_enabled = self._rule_enabled("uncertainty_escalation")

        if patch_text.strip() and not sections:
            if unc_enabled:
                live_mask |= self.all_livemask
                if self._trace:
                    self._note("uncertainty:unparseable_patch")
            return self._decode_masks(base_mask, live_mask)

        added_paths: Set[str] = set()
        deleted_paths: Set[str] = set()
        for sec in sections:
            st = sec["status"]
            rp = sec["rel_path"]
            ap = sec["a_path"]
            if st == "added" and rp:
                added_paths.add(rp)
            elif st == "deleted" and rp:
                deleted_paths.add(rp)
            elif sec.get("renamed") or (ap and rp and ap != rp):
                if ap:
                    deleted_paths.add(ap)
                if rp:
                    added_paths.add(rp)

        sec_added_texts = [
            "\n".join(
                l[1:] for l in sec.get("raw_diff", "").splitlines() if l.startswith("+") and not l.startswith("+++")
            )
            for sec in sections
        ]

        for sec_idx, sec in enumerate(sections):
            self._other_added_text = "\n".join(t for j, t in enumerate(sec_added_texts) if j != sec_idx and t)
            rel_path = sec["rel_path"]
            a_path = sec["a_path"]
            b_path = sec["b_path"]
            status = sec["status"]
            hunks = sec["hunks"]
            raw_diff = sec.get("raw_diff", "")
            is_rename = bool(sec.get("renamed") or (a_path and b_path and a_path != b_path))

            if not rel_path:
                continue

            fname = rel_path.rsplit("/", 1)[-1]
            ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
            is_inert_path = (
                ext in _INERT_EXTS or fname in _INERT_NAMES or rel_path.startswith(("docs/", ".github/"))
            )
            in_measured_tree = rel_path.startswith(("sqlglot/", "tests/"))

            pm_hit = False
            if not in_measured_tree and ext not in _INERT_EXTS:
                # Repo files outside the measured trees (sqlglotc/setup.py, setup.py, ...): tests that name them.
                for pth in {rel_path, a_path}:
                    if not pth:
                        continue
                    for tf in self.path_mentions.get(pth.split("/", 1)[0], ()):
                        m = self.file_to_livemask.get(tf, 0)
                        if m:
                            live_mask |= m
                            pm_hit = True
                if pm_hit and self._trace:
                    self._note(f"path_mention:{rel_path}")

            if sec.get("binary"):
                if is_inert_path:
                    if self._trace:
                        self._note(f"inert:{rel_path}")
                    continue
                if unc_enabled:
                    if rel_path.endswith(".py"):
                        if rel_path in self.file_to_livemask:
                            live_mask |= self.file_to_livemask[rel_path] | self._importer_test_livemask(rel_path)
                        else:
                            _, old_mod, _ = self._baseline_src_ast_lines(rel_path)
                            fb, fl = self._once_escalation(rel_path, old_mod)
                            if (fb | fl) == 0:
                                fl |= self.all_livemask
                            base_mask |= fb
                            live_mask |= fl
                    elif not pm_hit:
                        live_mask |= self.all_livemask
                    if self._trace:
                        self._note(f"uncertainty:{rel_path}")
                continue

            if is_rename and a_path and rel_path != a_path:
                if self._trace:
                    self._note("rename")
                # Handle old path as deleted and new path as added.
                if a_path.endswith(".py"):
                    if a_path in self.file_to_livemask:
                        live_mask |= self.file_to_livemask[a_path] | self._importer_test_livemask(a_path)
                    else:
                        _, old_a_mod, _ = self._baseline_src_ast_lines(a_path)
                        fb, fl = self._module_wide_fallback(a_path, old_a_mod, include_importers=True)
                        if unc_enabled and a_path.startswith("sqlglot/"):
                            fl |= self._lazy_test_livemask(a_path)
                            if (fb | fl) == 0:
                                fl |= self.all_livemask
                            if self._trace:
                                self._note(f"uncertainty:{a_path}")
                        base_mask |= fb
                        live_mask |= fl
                else:
                    base_mask |= self._file_access(a_path) | self._dir_walker_tests(a_path) | self._pkg_walker_tests(a_path)

                if rel_path.endswith(".py"):
                    base_mask |= self._dir_walker_tests(rel_path) | self._pkg_walker_tests(rel_path)
                    if rel_path in self.file_to_livemask:
                        live_mask |= self.file_to_livemask[rel_path]
                    elif unc_enabled and rel_path.startswith("sqlglot/"):
                        pkg = rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
                        live_mask |= self._pkg_sibling_lazy_livemask(pkg)
                        if self._trace:
                            self._note(f"uncertainty:{rel_path}")
                else:
                    base_mask |= self._file_access(rel_path) | self._dir_walker_tests(rel_path) | self._pkg_walker_tests(rel_path)
                continue

            if rel_path.endswith(".py"):
                if rel_path in self.file_to_livemask:
                    bm, lm = self._analyze_test_file(rel_path, status, hunks)
                else:
                    bm, lm = self._analyze_py_source(rel_path, status, hunks, added_paths, deleted_paths, raw_diff)
                base_mask |= bm
                live_mask |= lm
                if (
                    not in_measured_tree
                    and not is_inert_path
                    and not pm_hit
                    and not self._last_ast_eq
                    and (bm | lm) == 0
                    and unc_enabled
                ):
                    live_mask |= self.all_livemask
                    if self._trace:
                        self._note(f"uncertainty:{rel_path}")
            else:
                acc = (
                    self._file_access(rel_path)
                    | (self._file_access(a_path) if a_path and a_path != rel_path else 0)
                    | self._dir_walker_tests(rel_path)
                    | self._pkg_walker_tests(rel_path)
                )
                if acc:
                    base_mask |= acc
                    if self._trace:
                        self._note(f"fixture:{rel_path}")
                    continue

                if rel_path.startswith("tests/"):
                    parent = rel_path.rsplit("/", 1)[0] + "/"
                    dir_hits = 0
                    for k, raw in self.raw_file_access.items():
                        if k.startswith(parent):
                            dir_hits |= self._to_mask(raw)
                    if dir_hits:
                        base_mask |= dir_hits
                        if self._trace:
                            self._note(f"fixture:{rel_path}")
                    else:
                        live_mask |= self.all_livemask
                        if self._trace:
                            self._note(f"uncertainty:{rel_path}")
                else:
                    if is_inert_path:
                        if self._trace:
                            self._note(f"inert:{rel_path}")
                        continue
                    if rel_path.startswith("sqlglot/"):
                        live_mask |= self.all_livemask
                        if self._trace:
                            self._note(f"uncertainty:{rel_path}")
                    elif not pm_hit and unc_enabled:
                        live_mask |= self.all_livemask
                        if self._trace:
                            self._note(f"uncertainty:{rel_path}")

        if apply_empty_guard and self._empty_guard_pending and self._rule_enabled("empty_guard"):
            for rp, (eb, el, _) in self._empty_guard_pending.items():
                if eb or el:
                    base_mask |= eb
                    live_mask |= el
                    if self._trace:
                        self._note(f"empty_guard:{rp}")

        sel = self._decode_masks(base_mask, live_mask)
        if self.tpcds_num is not None and ((live_mask >> self.tpcds_num) & 1):
            self._tpcds_non_line = True
        if apply_empty_guard and sel and self._rule_enabled("dynamic_probe"):
            sel = self._maybe_run_dynamic_probes(sel, sections)
        return sel


def pack_workers(selected_nums: Set[int], num_to_ms: List[int]) -> List[List[int]]:
    """Optimal LPT + 1-move/2-swap local refinement over k = 1..8 minimizing max(load) + 2000*k."""
    if not selected_nums:
        return []

    ordered = sorted(selected_nums, key=lambda n: num_to_ms[n], reverse=True)
    n = len(ordered)
    max_k = min(MAX_WORKERS, n)

    total_ms = sum(num_to_ms[x] for x in ordered)
    best_cost = total_ms + WORKER_COST_MS
    best_bins: List[List[int]] = [ordered]
    slowest_ms = num_to_ms[ordered[0]]

    for k in range(2, max_k + 1):
        lb = max(slowest_ms, (total_ms + k - 1) // k) + WORKER_COST_MS * k
        if lb >= best_cost:
            continue

        bins: List[List[int]] = [[] for _ in range(k)]
        loads = [0] * k
        for tnum in ordered:
            dur = num_to_ms[tnum]
            min_i = 0
            min_l = loads[0]
            for i in range(1, k):
                li = loads[i]
                if li < min_l:
                    min_l = li
                    min_i = i
            bins[min_i].append(tnum)
            loads[min_i] = min_l + dur

        for _ in range(64):
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
            if hi == lo or not bins[hi]:
                break

            improved = False
            if len(bins[hi]) > 1:
                gap = hi_l - lo_l
                best_idx = -1
                best_new_max = hi_l
                for pos, tnum in enumerate(bins[hi]):
                    w = num_to_ms[tnum]
                    if 0 < w < gap:
                        new_max = hi_l - w if hi_l - w >= lo_l + w else lo_l + w
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
            if improved:
                continue

            if len(bins[hi]) <= 150:
                best_swap: Optional[Tuple[int, int, int, int]] = None
                best_pair_max = hi_l
                bin_hi = bins[hi]
                for j in range(k):
                    if j == hi:
                        continue
                    jl = loads[j]
                    if jl >= hi_l:
                        continue
                    bin_j = bins[j]
                    for pi, ti in enumerate(bin_hi):
                        wi = num_to_ms[ti]
                        for pj, tj in enumerate(bin_j):
                            wj = num_to_ms[tj]
                            delta = wi - wj
                            if delta <= 0:
                                continue
                            n_hi = hi_l - delta
                            n_j = jl + delta
                            pair_max = n_hi if n_hi >= n_j else n_j
                            if pair_max < best_pair_max:
                                best_pair_max = pair_max
                                best_swap = (j, pi, pj, delta)
                if best_swap is not None:
                    j, pi, pj, delta = best_swap
                    ti = bins[hi][pi]
                    tj = bins[j][pj]
                    bins[hi][pi] = tj
                    bins[j][pj] = ti
                    loads[hi] -= delta
                    loads[j] += delta
                    improved = True
            if not improved:
                break

        cost = max(loads) + WORKER_COST_MS * k
        if cost < best_cost:
            best_cost = cost
            best_bins = [b for b in bins if b]

    return best_bins


def load_maps(maps_path: Path) -> Dict[str, Any]:
    name = maps_path.name.lower()
    if name.endswith(".zlib"):
        return pickle.loads(zlib.decompress(maps_path.read_bytes()))
    if name.endswith(".pkl"):
        return pickle.loads(maps_path.read_bytes())
    if name.endswith(".gz"):
        with gzip.open(maps_path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    return json.loads(maps_path.read_text("utf-8"))


def select_commit_v4(
    engine: SelectorEngine,
    _unused: Optional[Any],
    patch_text: str,
) -> Set[int]:
    """Compatibility wrapper forwarding to SelectorEngine.select_commit."""
    return engine.select_commit(patch_text, apply_empty_guard=True)


def main() -> None:
    trace_on = TRACE or (os.environ.get("BLAST_TRACE") == "1")
    tests_json = json.loads((DATA_DIR / "tests.json").read_text("utf-8"))
    maps = load_maps(MAPS_PATH)

    engine = SelectorEngine(tests_json, maps, SQLGLOT_SRC)
    commits_dir = DATA_DIR / "commits"
    patch_files = sorted(commits_dir.glob("*.patch"))

    total_selected = 0
    for patch_path in patch_files:
        patch_text = patch_path.read_text("utf-8", errors="replace")
        sel = engine.select_commit(patch_text)
        workers = pack_workers(sel, engine.num_to_ms)
        total_selected += len(sel)
        if trace_on:
            occ = [b for b in workers if b]
            cost_ms = (max(sum(engine.num_to_ms[n] for n in b) for b in occ) + WORKER_COST_MS * len(occ)) if occ else 0
            sys.stderr.write(
                json.dumps(
                    {"commit": patch_path.stem, "reasons": engine.reasons, "n": len(sel), "cost_ms": cost_ms},
                    allow_nan=False,
                )
                + "\n"
            )
        sys.stdout.write(json.dumps({"commit": patch_path.stem, "workers": workers}, allow_nan=False) + "\n")
    sys.stdout.flush()
    print(f"selected {total_selected} tests across {len(patch_files)} commits", file=sys.stderr)


if __name__ == "__main__":
    main()
