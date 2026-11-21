#!/usr/bin/env python3
"""Build precomputed static + dynamic baseline maps for sqlglot at pinned commit eb11aec.

Outputs RWX_track/maps/blast_maps.pkl.zlib containing ONLY baseline commit facts:
  - version: 2
  - commit: pinned baseline commit hash
  - test_ids: list[str] (canonical pytest nodeids sorted deterministically)
  - line_cov: {rel_path: {lineno_int: test_bitmask}}
  - file_access: {rel_path: test_bitmask}
  - dir_access: {dir_rel_path: test_bitmask}
  - class_usage: {f"{rel_path}::{qualname}": test_bitmask}
  - pkg_walkers: {pkg_prefix: test_bitmask}
  - dict_key_usage: {attr_name: {cls_key: {norm_key: test_bitmask}}}
  - tracked_dict_classes: list[str]
  - imports: {rel_path: [imported_rel_path, ...]}
  - rev_imports: {rel_path: [importing_rel_path, ...]} (transitive)
  - dialect_typing_links: {typing_rel_path: [class_key, ...]}
  - attr_read_lines: {attr_name: {rel_path: [lineno_int, ...]}}
  - attr_import_test_files: {attr_name: [test_rel_path, ...]}
  - metaclass_attrs: list[str]
"""
from __future__ import annotations

import ast
import builtins
import concurrent.futures
import importlib
import io
import json
import os
import pathlib
import pickle
import pkgutil
import sys
import time
import unittest
import zlib
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


def _rel_path(path_obj: Any, repo: pathlib.Path) -> Optional[str]:
    try:
        p = pathlib.Path(path_obj)
        if not p.is_absolute():
            p = (repo / p).resolve()
        else:
            p = p.resolve()
        rel = p.relative_to(repo)
        return rel.as_posix()
    except Exception:
        return None


def _class_key(cls: type, repo: pathlib.Path) -> Optional[str]:
    mod_name = getattr(cls, "__module__", "")
    if not (mod_name == "sqlglot" or mod_name.startswith("sqlglot.") or mod_name.startswith("tests.")):
        return None
    mod = sys.modules.get(mod_name)
    mfile = getattr(mod, "__file__", None) if mod is not None else None
    if not mfile:
        return None
    rel = _rel_path(mfile, repo)
    if not rel:
        return None
    qn = getattr(cls, "__qualname__", cls.__name__)
    return f"{rel}::{qn}"


def _install_sync_executor() -> None:
    """Run ProcessPoolExecutor work synchronously in-process so coverage/hooks trace child tasks."""
    from concurrent.futures import Future

    class SyncExecutor:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
            if initializer is not None:
                initializer(*initargs)

        def submit(self, fn, *args, **kwargs):
            fut: Future = Future()
            try:
                p_fn, p_args, p_kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
                res = p_fn(*p_args, **p_kwargs)
                fut.set_result(pickle.loads(pickle.dumps(res)))
            except BaseException as exc:
                fut.set_exception(exc)
            return fut

        def map(self, fn, *iterables, timeout=None, chunksize=1):
            for args in zip(*iterables):
                p_fn, p_args = pickle.loads(pickle.dumps((fn, args)))
                res = p_fn(*p_args)
                yield pickle.loads(pickle.dumps(res))

        def shutdown(self, wait=True, *, cancel_futures=False):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.shutdown()
            return False

    concurrent.futures.ProcessPoolExecutor = SyncExecutor  # type: ignore[misc]


def _wrap_test_class_lifecycle(
    items: Any,
    cls_to_nodeids: Dict[type, List[str]],
    enter_ctx: Callable[[str], str],
    exit_ctx: Callable[[str], None],
) -> Tuple[Dict[type, str], List[Tuple[type, str, Any]]]:
    """Wrap setUpClass and tearDownClass across the MRO of collected test classes."""
    cls_to_tag: Dict[type, str] = {}
    seen_cls: Set[type] = set()
    wrapped: Set[Tuple[type, str]] = set()
    originals: List[Tuple[type, str, Any]] = []

    def _make_wrapped(fn: Callable[..., Any]) -> classmethod:
        @classmethod  # type: ignore[misc]
        def _wrapped(c: type) -> Any:
            tag = cls_to_tag.get(c, "")
            if not tag:
                return fn(c)
            prev = enter_ctx(tag)
            try:
                return fn(c)
            finally:
                exit_ctx(prev)

        return _wrapped

    for item in items:
        cls = getattr(item, "cls", None)
        if cls is None:
            continue
        cls_to_nodeids[cls].append(item.nodeid)
        if cls in seen_cls:
            continue
        seen_cls.add(cls)
        cls_to_tag[cls] = f"__CLS__::{cls.__module__}.{cls.__qualname__}"
        for owner in cls.__mro__:
            if owner is object or owner is unittest.TestCase:
                continue
            for attr_name in ("setUpClass", "tearDownClass"):
                key = (owner, attr_name)
                if key in wrapped or attr_name not in owner.__dict__:
                    continue
                wrapped.add(key)
                orig_desc = owner.__dict__[attr_name]
                originals.append((owner, attr_name, orig_desc))
                raw_fn = orig_desc.__func__ if isinstance(orig_desc, classmethod) else orig_desc
                setattr(owner, attr_name, _make_wrapped(raw_fn))

    return cls_to_tag, originals


def run_pass1_coverage(repo: pathlib.Path, out_cov_file: pathlib.Path) -> Tuple[List[str], Dict[int, int]] | Tuple[List[str], Dict[str, Dict[int, int]]]:
    """Run pytest with coverage.py dynamic contexts and MRO setUpClass/tearDownClass attribution."""
    import coverage
    import pytest

    if out_cov_file.exists():
        out_cov_file.unlink()

    t0 = time.perf_counter()
    print("[Pass 1] Starting coverage collection across all sqlglot tests...", file=sys.stderr, flush=True)

    cov = coverage.Coverage(
        data_file=str(out_cov_file),
        source=[str(repo / "sqlglot"), str(repo / "tests")],
        branch=False,
    )
    cov.start()

    _install_sync_executor()

    cls_to_nodeids: Dict[type, List[str]] = defaultdict(list)
    collected_nodeids: List[str] = []
    cur_ctx_stack: List[str] = [""]

    def _enter_ctx(tag: str) -> str:
        prev = cur_ctx_stack[-1]
        if tag != prev:
            cur_ctx_stack.append(tag)
            cov.switch_context(tag)
        return prev

    def _exit_ctx(prev: str) -> None:
        if cur_ctx_stack[-1] != prev and len(cur_ctx_stack) > 1:
            cur_ctx_stack.pop()
        cov.switch_context(prev)

    last_test_path: List[Any] = [None]

    class CovPlugin:
        def pytest_collection_modifyitems(self, items):
            for item in items:
                collected_nodeids.append(item.nodeid)
            _wrap_test_class_lifecycle(items, cls_to_nodeids, _enter_ctx, _exit_ctx)

        def pytest_runtest_protocol(self, item, nextitem):
            cur_path = getattr(item, "path", None)
            if cur_path != last_test_path[0]:
                last_test_path[0] = cur_path
                gen_mod = sys.modules.get("sqlglot.generator")
                if gen_mod is not None:
                    dc = getattr(gen_mod, "_DISPATCH_CACHE", None)
                    if isinstance(dc, dict):
                        dc.clear()
            cur_ctx_stack.append(item.nodeid)
            cov.switch_context(item.nodeid)
            return None

        def pytest_runtest_teardown(self, item, nextitem):
            # Ensure teardown itself runs under item context, then clear on report.
            return None

        def pytest_runtest_logfinish(self, nodeid, location):
            if len(cur_ctx_stack) > 1:
                cur_ctx_stack.pop()
            cov.switch_context(cur_ctx_stack[-1])

    rc = pytest.main(["-q", "--no-header", "tests"], plugins=[CovPlugin()])
    cov.stop()
    cov.save()
    dt = time.perf_counter() - t0
    print(f"[Pass 1] Pytest finished rc={rc}, collected={len(collected_nodeids)} in {dt:.1f}s", file=sys.stderr, flush=True)
    if rc != 0:
        raise RuntimeError(f"Pass 1 pytest failed with exit code {rc}")

    # Canonical index map over collected tests.
    test_ids = sorted(set(collected_nodeids))
    id_to_idx = {nid: i for i, nid in enumerate(test_ids)}

    cls_tag_to_mask: Dict[str, int] = {}
    for cls, nids in cls_to_nodeids.items():
        tag = f"__CLS__::{cls.__module__}.{cls.__qualname__}"
        m = 0
        for n in nids:
            idx = id_to_idx.get(n)
            if idx is not None:
                m |= 1 << idx
        if m:
            cls_tag_to_mask[tag] = m

    data = cov.get_data()
    line_cov: Dict[str, Dict[int, int]] = {}
    for fname in sorted(data.measured_files()):
        rel = _rel_path(fname, repo)
        if not rel or not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            continue
        by_ln = data.contexts_by_lineno(fname)
        out_lines: Dict[int, int] = {}
        for ln, ctxs in by_ln.items():
            mask = 0
            for c in ctxs:
                if not c:
                    continue
                idx = id_to_idx.get(c)
                if idx is not None:
                    mask |= 1 << idx
                else:
                    cmask = cls_tag_to_mask.get(c)
                    if cmask is not None:
                        mask |= cmask
            if mask:
                out_lines[int(ln)] = mask
        if out_lines:
            line_cov[rel] = out_lines

    print(f"[Pass 1] Extracted line_cov for {len(line_cov)} files", file=sys.stderr, flush=True)
    return test_ids, line_cov


_MISSING = object()

_PARSER_TRACKED_DICTS = (
    "FUNCTIONS",
    "FUNCTION_PARSERS",
    "NO_PAREN_FUNCTIONS",
    "NO_PAREN_FUNCTION_PARSERS",
    "STATEMENT_PARSERS",
    "EXPRESSION_PARSERS",
    "PROPERTY_PARSERS",
    "CONSTRAINT_PARSERS",
    "ALTER_PARSERS",
    "ALTER_ALTER_PARSERS",
    "SHOW_PARSERS",
    "SET_PARSERS",
    "RANGE_PARSERS",
    "PLACEHOLDER_PARSERS",
    "PRIMARY_PARSERS",
    "UNARY_PARSERS",
    "QUERY_MODIFIER_PARSERS",
    "COLUMN_OPERATORS",
    "TYPE_CONVERTERS",
    "TYPE_LITERAL_PARSERS",
    "BITWISE",
    "TERM",
    "FACTOR",
    "COMPARISON",
    "EQUALITY",
    "DISJUNCTION",
    "CONJUNCTION",
    "ASSIGNMENT",
    "EXPONENT",
    "LAMBDAS",
    "CAST_ACTIONS",
    "PIPE_SYNTAX_TRANSFORM_PARSERS",
)

_GENERATOR_TRACKED_DICTS = (
    "TYPE_MAPPING",
    "PROPERTIES_LOCATION",
    "TOKEN_MAPPING",
    "AFTER_HAVING_MODIFIER_TRANSFORMS",
    "TIME_PART_SINGULARS",
    "TYPE_PARAM_SETTINGS",
)

_TOKENIZER_TRACKED_DICTS = (
    "KEYWORDS",
    "SINGLE_TOKENS",
    "NUMERIC_LITERALS",
    "NUMERIC_ESCAPES",
)

_DIALECT_TRACKED_DICTS = (
    "EXPRESSION_METADATA",
    "DEFAULT_FUNCTIONS_COLUMN_NAMES",
    "COERCES_TO",
    "COLUMN_ALIAS_PRECEDENCE",
    "SET_OP_DISTINCT_BY_DEFAULT",
)

_ANNOTATOR_TRACKED_DICTS = (
    "COERCES_TO",
    "BINARY_COERCIONS",
)


def _norm_dict_key(k: Any) -> Optional[str]:
    if isinstance(k, str):
        return f"str:{k}"
    if isinstance(k, int) and not isinstance(k, bool):
        return f"int:{k}"
    if isinstance(k, tuple):
        parts: List[str] = []
        for item in k:
            ni = _norm_dict_key(item)
            if ni is None:
                return None
            parts.append(ni)
        return "tuple:(" + ",".join(parts) + ")"
    if isinstance(k, type):
        return f"cls:{k.__name__}"
    name = getattr(k, "name", None)
    if isinstance(name, str) and hasattr(type(k), "__members__"):
        return f"enum:{type(k).__name__}.{name}"
    return None


def run_pass2_dynamic_hooks(
    repo: pathlib.Path,
    test_ids: List[str],
    pytest_targets: Optional[List[str]] = None,
) -> Tuple[
    Dict[str, int],
    Dict[str, int],
    Dict[str, int],
    Dict[str, int],
    Dict[str, Dict[str, Dict[str, int]]],
    List[str],
]:
    """Run pytest with file_access, dir_access, class_usage, pkg_walkers, and dict_key_usage hooks."""
    import pytest
    import sqlglot.generator as gen_mod
    import sqlglot.tokenizer_core as tc_mod
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect, _Dialect
    from sqlglot.errors import SqlglotError
    from sqlglot.executor.context import Context
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import Table
    from sqlglot.generator import Generator
    from sqlglot.optimizer.annotate_types import TypeAnnotator
    from sqlglot.optimizer.resolver import Resolver
    from sqlglot.optimizer.scope import Scope
    from sqlglot.parser import Parser
    from sqlglot.planner import Plan, Step
    from sqlglot.schema import AbstractMappingSchema
    from sqlglot.tokens import Tokenizer

    diff_mod = importlib.import_module("sqlglot.diff")
    ChangeDistiller = diff_mod.ChangeDistiller
    TokenizerCore = tc_mod.TokenizerCore
    Token = tc_mod.Token

    t0 = time.perf_counter()
    print("[Pass 2] Running dynamic instrumentation (file/dir/class/pkg/dict-key)...", file=sys.stderr, flush=True)

    id_to_idx = {nid: i for i, nid in enumerate(test_ids)}
    cls_to_nodeids: Dict[type, List[str]] = defaultdict(list)

    state: Dict[str, Any] = {
        "ctx": "",
        "seen_files": set(),
        "seen_dirs": set(),
        "seen_clss": set(),
        "seen_pkgs": set(),
        "seen_dict_keys": set(),
    }

    raw_file_access: Dict[str, Set[str]] = defaultdict(set)
    raw_dir_access: Dict[str, Set[str]] = defaultdict(set)
    raw_class_usage: Dict[str, Set[str]] = defaultdict(set)
    raw_pkg_walkers: Dict[str, Set[str]] = defaultdict(set)
    raw_dict_usage: Dict[str, Dict[Tuple[str, str], Set[str]]] = defaultdict(lambda: defaultdict(set))
    tracked_dict_classes: Set[str] = set()

    cls_key_cache: Dict[type, Tuple[str, ...]] = {}
    wrapped_cls_attrs: Set[Tuple[type, str]] = set()
    wrapped_dict_originals: List[Tuple[type, str, dict]] = []
    wrapped_dispatch_cls: Set[type] = set()
    wrapped_dispatch_originals: Dict[type, dict] = {}
    next_td_id = [0]

    def _switch(new_ctx: str) -> None:
        state["ctx"] = new_ctx
        state["seen_files"].clear()
        state["seen_dirs"].clear()
        state["seen_clss"].clear()
        state["seen_pkgs"].clear()
        state["seen_dict_keys"].clear()

    def _record_file(path_arg: Any) -> None:
        ctx = state["ctx"]
        if not ctx:
            return
        rel = _rel_path(path_arg, repo)
        if not rel or rel in state["seen_files"]:
            return
        state["seen_files"].add(rel)
        raw_file_access[rel].add(ctx)

    def _record_dir(path_arg: Any) -> None:
        ctx = state["ctx"]
        if not ctx:
            return
        if path_arg is None or path_arg == "":
            path_arg = "."
        rel = _rel_path(path_arg, repo)
        if not rel or rel in state["seen_dirs"]:
            return
        state["seen_dirs"].add(rel)
        raw_dir_access[rel].add(ctx)

    def _mro_keys(cls: type) -> Tuple[str, ...]:
        cached = cls_key_cache.get(cls)
        if cached is not None:
            return cached
        keys: List[str] = []
        for base in cls.__mro__:
            k = _class_key(base, repo)
            if k:
                keys.append(k)
        res = tuple(keys)
        cls_key_cache[cls] = res
        return res

    def _record_cls(cls: type) -> None:
        ctx = state["ctx"]
        if not ctx or not isinstance(cls, type):
            return
        if cls in state["seen_clss"]:
            return
        state["seen_clss"].add(cls)
        for k in _mro_keys(cls):
            raw_class_usage[k].add(ctx)

    def _record_pkg(prefix: str) -> None:
        ctx = state["ctx"]
        if not ctx or not prefix or prefix in state["seen_pkgs"]:
            return
        state["seen_pkgs"].add(prefix)
        raw_pkg_walkers[prefix].add(ctx)

    def _log_dict_hit(td_id: int, mro_keys: Tuple[str, ...], attr: str, k: Any) -> None:
        ctx = state["ctx"]
        if not ctx:
            return
        cache_k = (td_id, k)
        seen = state["seen_dict_keys"]
        if cache_k in seen:
            return
        seen.add(cache_k)
        kn = _norm_dict_key(k)
        if kn is None:
            return
        by_attr = raw_dict_usage[attr]
        for ckey in mro_keys:
            by_attr[(ckey, kn)].add(ctx)

    class _TrackedDict(dict):
        __slots__ = ("_mro", "_attr", "_td_id")

        def __init__(self, base: dict, mro: Tuple[str, ...], attr: str) -> None:
            super().__init__(base)
            self._mro = mro
            self._attr = attr
            next_td_id[0] += 1
            self._td_id = next_td_id[0]

        def __getitem__(self, k: Any) -> Any:
            val = dict.__getitem__(self, k)
            if state["ctx"]:
                _log_dict_hit(self._td_id, self._mro, self._attr, k)
            return val

        def get(self, k: Any, default: Any = None) -> Any:
            val = dict.get(self, k, _MISSING)
            if val is _MISSING:
                return default
            if state["ctx"]:
                _log_dict_hit(self._td_id, self._mro, self._attr, k)
            return val

        def __contains__(self, k: object) -> bool:
            found = dict.__contains__(self, k)
            if found and state["ctx"]:
                _log_dict_hit(self._td_id, self._mro, self._attr, k)
            return found

        def copy(self) -> dict:
            return dict(self)

        def __reduce__(self):
            return (dict, (dict(self),))

    def _wrap_cls_dicts(cls: type, attrs: Tuple[str, ...]) -> None:
        for attr in attrs:
            for owner in cls.__mro__:
                if owner is object:
                    continue
                if attr not in owner.__dict__:
                    continue
                if (owner, attr) in wrapped_cls_attrs:
                    break
                cur = owner.__dict__[attr]
                if type(cur) is dict:
                    wrapped_cls_attrs.add((owner, attr))
                    wrapped_dict_originals.append((owner, attr, cur))
                    mro = _mro_keys(owner)
                    if mro:
                        tracked_dict_classes.update(mro)
                        setattr(owner, attr, _TrackedDict(cur, mro, attr))
                break

    # Hook file & directory access (read mode).
    orig_open = io.open
    orig_builtin_open = open
    orig_path_open = pathlib.Path.open
    orig_path_read_text = pathlib.Path.read_text
    orig_path_read_bytes = pathlib.Path.read_bytes
    orig_listdir = os.listdir
    orig_scandir = os.scandir
    orig_walk = os.walk
    orig_path_iterdir = pathlib.Path.iterdir
    orig_path_glob = pathlib.Path.glob
    orig_path_rglob = pathlib.Path.rglob

    def hooked_open(file, mode="r", *args, **kwargs):
        if isinstance(mode, str) and ("r" in mode or "+" in mode or mode == ""):
            _record_file(file)
        return orig_builtin_open(file, mode, *args, **kwargs)

    def hooked_path_open(self, mode="r", *args, **kwargs):
        if "r" in mode or "+" in mode:
            _record_file(self)
        return orig_path_open(self, mode, *args, **kwargs)

    def hooked_read_text(self, *args, **kwargs):
        _record_file(self)
        return orig_path_read_text(self, *args, **kwargs)

    def hooked_read_bytes(self, *args, **kwargs):
        _record_file(self)
        return orig_path_read_bytes(self, *args, **kwargs)

    def hooked_listdir(path="."):
        _record_dir(path)
        return orig_listdir(path)

    def hooked_scandir(path="."):
        _record_dir(path)
        return orig_scandir(path)

    def hooked_walk(top, *args, **kwargs):
        _record_dir(top)
        return orig_walk(top, *args, **kwargs)

    def hooked_iterdir(self):
        _record_dir(self)
        return orig_path_iterdir(self)

    def hooked_glob(self, pattern, *args, **kwargs):
        _record_dir(self)
        return orig_path_glob(self, pattern, *args, **kwargs)

    def hooked_rglob(self, pattern, *args, **kwargs):
        _record_dir(self)
        return orig_path_rglob(self, pattern, *args, **kwargs)

    # Hook Dialect / Tokenizer / TokenizerCore / Parser / Generator / TypeAnnotator / Optimizer / Executor / Expr.
    orig_gor_desc = Dialect.__dict__["get_or_raise"]
    orig_get_or_raise = orig_gor_desc.__func__
    orig_dget_desc = _Dialect.__dict__["get"]
    orig_dialect_get = orig_dget_desc.__func__
    orig_dgetitem_desc = _Dialect.__dict__["__getitem__"]
    orig_dialect_getitem = orig_dgetitem_desc.__func__
    orig_dialect_init = Dialect.__init__
    orig_tok_init = Tokenizer.__init__
    orig_tokcore_init = TokenizerCore.__init__
    orig_parser_init = Parser.__init__
    orig_gen_init = Generator.__init__
    orig_annot_init = TypeAnnotator.__init__
    orig_expr_init = exp.Expr.__init__
    orig_schema_init = AbstractMappingSchema.__init__
    orig_scope_init = Scope.__init__
    orig_resolver_init = Resolver.__init__
    orig_ctx_init = Context.__init__
    orig_tbl_init = Table.__init__
    orig_pyexec_init = PythonExecutor.__init__
    orig_step_init = Step.__init__
    orig_plan_init = Plan.__init__
    orig_diff_init = ChangeDistiller.__init__
    orig_token_init = Token.__init__
    orig_err_init = SqlglotError.__init__
    orig_iter_modules = pkgutil.iter_modules
    orig_walk_packages = pkgutil.walk_packages

    def _wrap_dialect_family(d_cls: Any) -> None:
        if not isinstance(d_cls, type):
            return
        _wrap_cls_dicts(d_cls, _DIALECT_TRACKED_DICTS)
        tok_cls = getattr(d_cls, "tokenizer_class", None)
        if isinstance(tok_cls, type):
            _wrap_cls_dicts(tok_cls, _TOKENIZER_TRACKED_DICTS)
        prs_cls = getattr(d_cls, "parser_class", None)
        if isinstance(prs_cls, type):
            _wrap_cls_dicts(prs_cls, _PARSER_TRACKED_DICTS)
        gen_cls = getattr(d_cls, "generator_class", None)
        if isinstance(gen_cls, type):
            _wrap_cls_dicts(gen_cls, _GENERATOR_TRACKED_DICTS + ("TRANSFORMS",))

    @classmethod  # type: ignore[misc]
    def hooked_get_or_raise(cls, dialect):
        res = orig_get_or_raise(cls, dialect)
        _wrap_dialect_family(type(res))
        if state["ctx"]:
            _record_cls(type(res))
        return res

    @classmethod  # type: ignore[misc]
    def hooked_dialect_get(cls, key, default=None):
        res = orig_dialect_get(cls, key, default)
        if state["ctx"] and isinstance(res, type):
            _record_cls(res)
        return res

    @classmethod  # type: ignore[misc]
    def hooked_dialect_getitem(cls, key):
        res = orig_dialect_getitem(cls, key)
        if state["ctx"] and isinstance(res, type):
            _record_cls(res)
        return res

    def hooked_dialect_init(self, *args, **kwargs):
        _wrap_dialect_family(type(self))
        orig_dialect_init(self, *args, **kwargs)
        if state["ctx"]:
            _record_cls(type(self))

    def hooked_tok_init(self, *args, **kwargs):
        _wrap_cls_dicts(type(self), _TOKENIZER_TRACKED_DICTS)
        orig_tok_init(self, *args, **kwargs)
        if state["ctx"]:
            _record_cls(type(self))

    def hooked_tokcore_init(self, *args, **kwargs):
        orig_tokcore_init(self, *args, **kwargs)
        if state["ctx"]:
            cls = type(self)
            if cls not in state["seen_clss"]:
                _record_cls(cls)

    def hooked_parser_init(self, *args, **kwargs):
        _wrap_cls_dicts(type(self), _PARSER_TRACKED_DICTS)
        orig_parser_init(self, *args, **kwargs)
        if state["ctx"]:
            _record_cls(type(self))

    def hooked_gen_init(self, *args, **kwargs):
        cls = type(self)
        _wrap_cls_dicts(cls, _GENERATOR_TRACKED_DICTS + ("TRANSFORMS",))
        orig_gen_init(self, *args, **kwargs)
        dc = gen_mod._DISPATCH_CACHE.get(cls)
        if dc is not None and not isinstance(dc, _TrackedDict):
            mro = _mro_keys(cls)
            if mro:
                if cls not in wrapped_dispatch_cls:
                    wrapped_dispatch_cls.add(cls)
                    wrapped_dispatch_originals[cls] = dc
                tracked_dict_classes.update(mro)
                td = _TrackedDict(dc, mro, "TRANSFORMS")
                gen_mod._DISPATCH_CACHE[cls] = td
                self._dispatch = td
        if state["ctx"]:
            _record_cls(cls)

    def hooked_annot_init(self, *args, **kwargs):
        _wrap_cls_dicts(type(self), _ANNOTATOR_TRACKED_DICTS)
        schema_arg = args[0] if len(args) >= 1 else kwargs.get("schema")
        sd = getattr(schema_arg, "dialect", None) if schema_arg is not None else None
        _wrap_dialect_family(type(sd) if sd is not None else Dialect)
        orig_annot_init(self, *args, **kwargs)
        if state["ctx"]:
            _record_cls(type(self))
            _record_cls(type(self.dialect))

    def _make_simple_init_hook(orig_fn):
        def _hook(self, *args, **kwargs):
            orig_fn(self, *args, **kwargs)
            if state["ctx"]:
                cls = type(self)
                if cls not in state["seen_clss"]:
                    _record_cls(cls)
        return _hook

    hooked_expr_init = _make_simple_init_hook(orig_expr_init)
    hooked_schema_init = _make_simple_init_hook(orig_schema_init)
    hooked_scope_init = _make_simple_init_hook(orig_scope_init)
    hooked_resolver_init = _make_simple_init_hook(orig_resolver_init)
    hooked_ctx_init = _make_simple_init_hook(orig_ctx_init)
    hooked_tbl_init = _make_simple_init_hook(orig_tbl_init)
    hooked_pyexec_init = _make_simple_init_hook(orig_pyexec_init)
    hooked_step_init = _make_simple_init_hook(orig_step_init)
    hooked_plan_init = _make_simple_init_hook(orig_plan_init)
    hooked_diff_init = _make_simple_init_hook(orig_diff_init)
    hooked_token_init = _make_simple_init_hook(orig_token_init)
    hooked_err_init = _make_simple_init_hook(orig_err_init)

    def hooked_iter_modules(path=None, prefix=""):
        if state["ctx"] and path:
            for p in path:
                rel = _rel_path(p, repo)
                if rel:
                    _record_pkg(rel)
        return orig_iter_modules(path, prefix)

    def hooked_walk_packages(path=None, prefix="", onerror=None):
        if state["ctx"] and path:
            for p in path:
                rel = _rel_path(p, repo)
                if rel:
                    _record_pkg(rel)
        return orig_walk_packages(path, prefix, onerror)

    cls_to_tag: Dict[type, str] = {}
    lifecycle_originals: List[Tuple[type, str, Any]] = []

    class HookPlugin:
        def pytest_collection_modifyitems(self, items):
            nonlocal cls_to_tag, lifecycle_originals

            def _enter_p2(tag: str) -> str:
                prev = state["ctx"]
                if tag != prev:
                    _switch(tag)
                return prev

            def _exit_p2(prev: str) -> None:
                if state["ctx"] != prev:
                    _switch(prev)

            cls_to_tag, lifecycle_originals = _wrap_test_class_lifecycle(
                items,
                cls_to_nodeids,
                _enter_p2,
                _exit_p2,
            )

        def pytest_runtest_protocol(self, item, nextitem):
            _switch(item.nodeid)
            return None

        def pytest_runtest_logfinish(self, nodeid, location):
            _switch("")

    builtins.open = hooked_open  # type: ignore[assignment]
    io.open = hooked_open  # type: ignore[assignment]
    pathlib.Path.open = hooked_path_open  # type: ignore[method-assign]
    pathlib.Path.read_text = hooked_read_text  # type: ignore[method-assign]
    pathlib.Path.read_bytes = hooked_read_bytes  # type: ignore[method-assign]
    os.listdir = hooked_listdir  # type: ignore[assignment]
    os.scandir = hooked_scandir  # type: ignore[assignment]
    os.walk = hooked_walk  # type: ignore[assignment]
    pathlib.Path.iterdir = hooked_iterdir  # type: ignore[method-assign]
    pathlib.Path.glob = hooked_glob  # type: ignore[method-assign]
    pathlib.Path.rglob = hooked_rglob  # type: ignore[method-assign]
    Dialect.get_or_raise = hooked_get_or_raise  # type: ignore[method-assign]
    _Dialect.get = hooked_dialect_get  # type: ignore[method-assign]
    _Dialect.__getitem__ = hooked_dialect_getitem  # type: ignore[method-assign]
    Dialect.__init__ = hooked_dialect_init  # type: ignore[method-assign]
    Tokenizer.__init__ = hooked_tok_init  # type: ignore[method-assign]
    TokenizerCore.__init__ = hooked_tokcore_init  # type: ignore[method-assign]
    Parser.__init__ = hooked_parser_init  # type: ignore[method-assign]
    Generator.__init__ = hooked_gen_init  # type: ignore[method-assign]
    TypeAnnotator.__init__ = hooked_annot_init  # type: ignore[method-assign]
    exp.Expr.__init__ = hooked_expr_init  # type: ignore[method-assign]
    AbstractMappingSchema.__init__ = hooked_schema_init  # type: ignore[method-assign]
    Scope.__init__ = hooked_scope_init  # type: ignore[method-assign]
    Resolver.__init__ = hooked_resolver_init  # type: ignore[method-assign]
    Context.__init__ = hooked_ctx_init  # type: ignore[method-assign]
    Table.__init__ = hooked_tbl_init  # type: ignore[method-assign]
    PythonExecutor.__init__ = hooked_pyexec_init  # type: ignore[method-assign]
    Step.__init__ = hooked_step_init  # type: ignore[method-assign]
    Plan.__init__ = hooked_plan_init  # type: ignore[method-assign]
    ChangeDistiller.__init__ = hooked_diff_init  # type: ignore[method-assign]
    Token.__init__ = hooked_token_init  # type: ignore[method-assign]
    SqlglotError.__init__ = hooked_err_init  # type: ignore[method-assign]
    pkgutil.iter_modules = hooked_iter_modules
    pkgutil.walk_packages = hooked_walk_packages

    try:
        targets = pytest_targets if pytest_targets is not None else ["tests"]
        rc = pytest.main(["-q", "--no-header", *targets], plugins=[HookPlugin()])
    finally:
        builtins.open = orig_builtin_open
        io.open = orig_open
        pathlib.Path.open = orig_path_open  # type: ignore[method-assign]
        pathlib.Path.read_text = orig_path_read_text  # type: ignore[method-assign]
        pathlib.Path.read_bytes = orig_path_read_bytes  # type: ignore[method-assign]
        os.listdir = orig_listdir
        os.scandir = orig_scandir
        os.walk = orig_walk
        pathlib.Path.iterdir = orig_path_iterdir  # type: ignore[method-assign]
        pathlib.Path.glob = orig_path_glob  # type: ignore[method-assign]
        pathlib.Path.rglob = orig_path_rglob  # type: ignore[method-assign]
        Dialect.get_or_raise = orig_gor_desc  # type: ignore[method-assign]
        _Dialect.get = orig_dget_desc  # type: ignore[method-assign]
        _Dialect.__getitem__ = orig_dgetitem_desc  # type: ignore[method-assign]
        Dialect.__init__ = orig_dialect_init  # type: ignore[method-assign]
        Tokenizer.__init__ = orig_tok_init  # type: ignore[method-assign]
        TokenizerCore.__init__ = orig_tokcore_init  # type: ignore[method-assign]
        Parser.__init__ = orig_parser_init  # type: ignore[method-assign]
        Generator.__init__ = orig_gen_init  # type: ignore[method-assign]
        TypeAnnotator.__init__ = orig_annot_init  # type: ignore[method-assign]
        exp.Expr.__init__ = orig_expr_init  # type: ignore[method-assign]
        AbstractMappingSchema.__init__ = orig_schema_init  # type: ignore[method-assign]
        Scope.__init__ = orig_scope_init  # type: ignore[method-assign]
        Resolver.__init__ = orig_resolver_init  # type: ignore[method-assign]
        Context.__init__ = orig_ctx_init  # type: ignore[method-assign]
        Table.__init__ = orig_tbl_init  # type: ignore[method-assign]
        PythonExecutor.__init__ = orig_pyexec_init  # type: ignore[method-assign]
        Step.__init__ = orig_step_init  # type: ignore[method-assign]
        Plan.__init__ = orig_plan_init  # type: ignore[method-assign]
        ChangeDistiller.__init__ = orig_diff_init  # type: ignore[method-assign]
        Token.__init__ = orig_token_init  # type: ignore[method-assign]
        SqlglotError.__init__ = orig_err_init  # type: ignore[method-assign]
        pkgutil.iter_modules = orig_iter_modules
        pkgutil.walk_packages = orig_walk_packages
        for owner, attr_name, orig_desc in lifecycle_originals:
            setattr(owner, attr_name, orig_desc)
        for owner, attr_name, orig_d in wrapped_dict_originals:
            setattr(owner, attr_name, orig_d)
        for cls, orig_dc in wrapped_dispatch_originals.items():
            gen_mod._DISPATCH_CACHE[cls] = orig_dc

    if rc != 0:
        raise RuntimeError(f"Pass 2 pytest failed with exit code {rc}")

    cls_tag_to_mask: Dict[str, int] = {}
    for cls, nids in cls_to_nodeids.items():
        tag = cls_to_tag.get(cls) or f"__CLS__::{cls.__module__}.{cls.__qualname__}"
        m = 0
        for n in nids:
            idx = id_to_idx.get(n)
            if idx is not None:
                m |= 1 << idx
        if m:
            cls_tag_to_mask[tag] = m

    def _ctxs_to_mask(ctxs: Set[str]) -> int:
        mask = 0
        for c in ctxs:
            idx = id_to_idx.get(c)
            if idx is not None:
                mask |= 1 << idx
            elif (cm := cls_tag_to_mask.get(c)) is not None:
                mask |= cm
        return mask

    def _expand(raw_map: Dict[str, Set[str]]) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for key, ctxs in sorted(raw_map.items()):
            mask = _ctxs_to_mask(ctxs)
            if mask:
                out[key] = mask
        return out

    dict_key_usage: Dict[str, Dict[str, Dict[str, int]]] = {}
    for attr, entries in sorted(raw_dict_usage.items()):
        cls_map: Dict[str, Dict[str, int]] = defaultdict(dict)
        for (ckey, kn), ctxs in entries.items():
            mask = _ctxs_to_mask(ctxs)
            if mask:
                cls_map[ckey][kn] = mask
        if cls_map:
            dict_key_usage[attr] = {ck: dict(kv) for ck, kv in sorted(cls_map.items())}

    dt = time.perf_counter() - t0
    file_access = _expand(raw_file_access)
    dir_access = _expand(raw_dir_access)
    class_usage = _expand(raw_class_usage)
    pkg_walkers = _expand(raw_pkg_walkers)
    total_dk = sum(len(kv) for cm in dict_key_usage.values() for kv in cm.values())
    print(
        f"[Pass 2] Done rc={rc} in {dt:.1f}s | file_access={len(file_access)} dir_access={len(dir_access)} "
        f"class_usage={len(class_usage)} pkg_walkers={len(pkg_walkers)} dict_keys={total_dk} "
        f"tracked_cls={len(tracked_dict_classes)}",
        file=sys.stderr,
        flush=True,
    )
    return (
        file_access,
        dir_access,
        class_usage,
        pkg_walkers,
        dict_key_usage,
        sorted(tracked_dict_classes),
    )


def _scan_attr_reads(
    rel: str,
    tree: ast.AST,
    attr_read_lines: Dict[str, Dict[str, Set[int]]],
    attr_import_test_files: Dict[str, Set[str]],
    metaclass_attrs: Set[str],
) -> None:
    """Record uppercase attribute reads, import-time test reads, and metaclass/class-body reads."""
    is_test_file = rel.startswith("tests/")
    is_src_file = rel.startswith("sqlglot/")

    def _visit(node: ast.AST, in_callable: bool, in_meta: bool, stmt_ln: Optional[int]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nxt_meta = in_meta or node.name in ("__new__", "__init_subclass__")
            for child in ast.iter_child_nodes(node):
                _visit(child, True, nxt_meta, getattr(child, "lineno", stmt_ln))
            return
        if isinstance(node, ast.Lambda):
            for child in ast.iter_child_nodes(node):
                _visit(child, True, False, stmt_ln)
            return
        if isinstance(node, ast.stmt):
            stmt_ln = getattr(node, "lineno", stmt_ln)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            aname = node.attr
            if aname.isupper() and not aname.startswith("__"):
                lines = attr_read_lines[aname][rel]
                ln0 = getattr(node, "lineno", None)
                ln1 = getattr(node, "end_lineno", ln0)
                if ln0 is not None:
                    for ln in range(ln0, (ln1 or ln0) + 1):
                        lines.add(ln)
                if stmt_ln is not None:
                    lines.add(stmt_ln)
                if is_test_file and not in_callable:
                    attr_import_test_files[aname].add(rel)
                if is_src_file and (not in_callable or in_meta):
                    metaclass_attrs.add(aname)
                return
        for child in ast.iter_child_nodes(node):
            _visit(child, in_callable, in_meta, stmt_ln)

    _visit(tree, False, False, None)


def build_static_graphs(
    repo: pathlib.Path,
) -> Tuple[
    Dict[str, List[str]],
    Dict[str, List[str]],
    Dict[str, List[str]],
    Dict[str, Dict[str, List[int]]],
    Dict[str, List[str]],
    List[str],
]:
    """Parse Python files in sqlglot/ and tests/ to build static import, typing, and attr-read graphs."""
    t0 = time.perf_counter()
    py_files: Dict[str, str] = {}
    rel_set: Set[str] = set()
    for top in ("sqlglot", "tests"):
        base = repo / top
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(repo).as_posix()
            py_files[rel] = p.read_text("utf-8", errors="replace")
            rel_set.add(rel)

    def _mod_to_rel(mod: str) -> Optional[str]:
        parts = mod.split(".")
        cand_pkg = "/".join(parts) + "/__init__.py"
        if cand_pkg in rel_set:
            return cand_pkg
        cand_mod = "/".join(parts) + ".py"
        if cand_mod in rel_set:
            return cand_mod
        return None

    direct: Dict[str, Set[str]] = defaultdict(set)
    dialect_mod_classes: Dict[str, List[str]] = {}
    typing_links: Dict[str, Set[str]] = defaultdict(set)
    attr_read_lines: Dict[str, Dict[str, Set[int]]] = defaultdict(lambda: defaultdict(set))
    attr_import_test_files: Dict[str, Set[str]] = defaultdict(set)
    metaclass_attrs: Set[str] = set()

    for rel, src in py_files.items():
        cur_mod_parts = rel.removesuffix(".py").split("/")
        pkg_parts = cur_mod_parts[:-1]

        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        _scan_attr_reads(rel, tree, attr_read_lines, attr_import_test_files, metaclass_attrs)

        cls_names = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        if rel.startswith("sqlglot/dialects/") and cls_names:
            dialect_mod_classes[rel] = [f"{rel}::{cname}" for cname in cls_names]

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    trel = _mod_to_rel(alias.name)
                    if trel and trel != rel:
                        direct[rel].add(trel)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    up = node.level - 1
                    base_parts = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else []
                    base_mod = ".".join(base_parts + ([node.module] if node.module else []))
                else:
                    base_mod = node.module or ""
                if base_mod:
                    trel = _mod_to_rel(base_mod)
                    if trel and trel != rel:
                        direct[rel].add(trel)
                        if trel.startswith("sqlglot/typing/") and rel.startswith("sqlglot/dialects/") and cls_names:
                            for cname in cls_names:
                                typing_links[trel].add(f"{rel}::{cname}")
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    sub_mod = f"{base_mod}.{alias.name}" if base_mod else alias.name
                    trel2 = _mod_to_rel(sub_mod)
                    if trel2 and trel2 != rel:
                        direct[rel].add(trel2)

    rev_direct: Dict[str, Set[str]] = defaultdict(set)
    for src_mod, deps in direct.items():
        for dep in deps:
            rev_direct[dep].add(src_mod)

    # Transitive reverse closure.
    rev_trans: Dict[str, List[str]] = {}
    for target in sorted(rel_set):
        visited: Set[str] = set()
        stack = list(rev_direct.get(target, ()))
        while stack:
            cur = stack.pop()
            if cur in visited or cur == target:
                continue
            visited.add(cur)
            stack.extend(rev_direct.get(cur, ()))
        if visited:
            rev_trans[target] = sorted(visited)
        if target.startswith("sqlglot/typing/"):
            for imp_rel in visited:
                if imp_rel.startswith("sqlglot/dialects/") and imp_rel in dialect_mod_classes:
                    typing_links[target].update(dialect_mod_classes[imp_rel])

    direct_out = {k: sorted(v) for k, v in sorted(direct.items()) if v}
    typing_out = {k: sorted(v) for k, v in sorted(typing_links.items()) if v}
    attr_reads_out = {
        aname: {r: sorted(lns) for r, lns in sorted(rmap.items()) if lns}
        for aname, rmap in sorted(attr_read_lines.items())
        if rmap
    }
    attr_tests_out = {
        aname: sorted(tfiles)
        for aname, tfiles in sorted(attr_import_test_files.items())
        if tfiles
    }
    meta_out = sorted(metaclass_attrs)
    dt = time.perf_counter() - t0
    print(
        f"[Pass 3] Static graphs built for {len(rel_set)} modules in {dt:.2f}s | "
        f"typing_links={len(typing_out)} attr_reads={len(attr_reads_out)} meta_attrs={len(meta_out)}",
        file=sys.stderr,
        flush=True,
    )
    return direct_out, rev_trans, typing_out, attr_reads_out, attr_tests_out, meta_out


def main() -> None:
    import argparse
    import subprocess

    ap = argparse.ArgumentParser(description="Build precomputed baseline maps for sqlglot.")
    ap.add_argument("repo", type=pathlib.Path)
    ap.add_argument("out_maps", type=pathlib.Path)
    ap.add_argument("--cache-dir", type=pathlib.Path, default=pathlib.Path("/tmp/rwx_cache"))
    ap.add_argument("--use-cache", action="store_true")
    args = ap.parse_args()

    repo = args.repo.resolve()
    out_path = args.out_maps.resolve()
    cache_dir = args.cache_dir.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(repo)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    p1_cache = cache_dir / "pass1_cache.pkl"
    if args.use_cache and p1_cache.is_file():
        print(f"[Pass 1] Reusing cached {p1_cache}", file=sys.stderr, flush=True)
        with p1_cache.open("rb") as f:
            cached1 = pickle.load(f)
        test_ids = cached1["test_ids"]
        line_cov = cached1["line_cov"]
    else:
        cov_file = cache_dir / "rwx_cov_pass1.sqlite"
        test_ids, line_cov = run_pass1_coverage(repo, cov_file)
        if cov_file.exists():
            cov_file.unlink()
        with p1_cache.open("wb") as f:
            pickle.dump({"test_ids": test_ids, "line_cov": line_cov}, f, protocol=5)

    p2_cache = cache_dir / "pass2_cache.pkl"
    if args.use_cache and p2_cache.is_file():
        print(f"[Pass 2] Reusing cached {p2_cache}", file=sys.stderr, flush=True)
        with p2_cache.open("rb") as f:
            pass2 = pickle.load(f)
    else:
        tids_file = cache_dir / "test_ids_tmp.json"
        tids_file.write_text(json.dumps(test_ids), encoding="utf-8")
        cmd = [
            sys.executable,
            str(pathlib.Path(__file__).resolve()),
            "--pass2-only",
            str(repo),
            str(tids_file),
            str(p2_cache),
        ]
        subprocess.check_call(cmd)
        with p2_cache.open("rb") as f:
            pass2 = pickle.load(f)

    (
        direct_imports,
        rev_imports,
        dialect_typing_links,
        attr_read_lines,
        attr_import_test_files,
        metaclass_attrs,
    ) = build_static_graphs(repo)

    payload = {
        "version": 2,
        "commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "test_ids": test_ids,
        "line_cov": line_cov,
        "file_access": pass2["file_access"],
        "dir_access": pass2["dir_access"],
        "class_usage": pass2["class_usage"],
        "pkg_walkers": pass2["pkg_walkers"],
        "dict_key_usage": pass2["dict_key_usage"],
        "tracked_dict_classes": pass2["tracked_dict_classes"],
        "imports": direct_imports,
        "rev_imports": rev_imports,
        "dialect_typing_links": dialect_typing_links,
        "attr_read_lines": attr_read_lines,
        "attr_import_test_files": attr_import_test_files,
        "metaclass_attrs": metaclass_attrs,
    }
    raw_bytes = pickle.dumps(payload, protocol=5)
    comp_bytes = zlib.compress(raw_bytes, level=1)
    out_path.write_bytes(comp_bytes)
    print(
        f"[DONE] Saved {out_path} ({len(comp_bytes) / 1024:.1f} KiB compressed, {len(raw_bytes) / 1024:.1f} KiB raw)",
        file=sys.stderr,
        flush=True,
    )


def _main_pass2() -> None:
    repo = pathlib.Path(sys.argv[2]).resolve()
    test_ids = json.loads(pathlib.Path(sys.argv[3]).read_text("utf-8"))
    pass2_out = pathlib.Path(sys.argv[4]).resolve()
    pytest_targets = sys.argv[5:] or None
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(repo)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    _install_sync_executor()
    (
        file_access,
        dir_access,
        class_usage,
        pkg_walkers,
        dict_key_usage,
        tracked_dict_classes,
    ) = run_pass2_dynamic_hooks(repo, test_ids, pytest_targets=pytest_targets)
    with pass2_out.open("wb") as f:
        pickle.dump(
            {
                "file_access": file_access,
                "dir_access": dir_access,
                "class_usage": class_usage,
                "pkg_walkers": pkg_walkers,
                "dict_key_usage": dict_key_usage,
                "tracked_dict_classes": tracked_dict_classes,
            },
            f,
            protocol=5,
        )


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--pass2-only":
        _main_pass2()
    else:
        main()
