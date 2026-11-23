#!/usr/bin/env python3
"""Build precomputed static + dynamic baseline maps for sqlglot at pinned commit eb11aec.

Outputs maps/blast_maps.pkl.zlib containing ONLY baseline commit facts:
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
  - once_lines: {rel_path: [lineno_int, ...]} lines executed once per process (import / class creation /
    metaclass hooks / memoized builders); per-test coverage under-attributes them (version 3)
  - collection_lines: {rel_path: {lineno_int: [test_rel_path, ...]}} sqlglot lines executed during test
    file collection / import (version 4)
  - import_trigger_lines: {rel_path: {lineno_int: [trigger_rel_path, ...]}} runtime-executed lines also
    executed while importing trigger module(s) (version 5)
  - dict_value_import_reads: frozenset[str] of UPPER attr names whose values are read unsafely at import/once time
  - str_const_idents: frozenset[str] of identifier string constants across sqlglot/ and tests/
  - known_words: frozenset[str] of all identifiers and \\w+ tokens in strings across sqlglot/ and tests/
  - lazy_test_importers: {rel_path: [test_rel_path, ...]} reached only via lazy/dynamic imports
  - path_mentions: {top_level_entry: [test_rel_path, ...]} for files outside sqlglot/ and tests/
  - eq_safe_sites: {rel_path: frozenset[(lineno, col, end_lineno, end_col)]} dynamically proven safe Compare sites (version 4)
  - eq_sites_stats: summary dict from --eqsites-only pass (version 4)
  - subprocess_test_ids: list[str] of tests referencing subprocess/Popen/os.system/sys.executable (version 4)

Refresh only the probe-derived keys on an existing maps file:
  python build_maps.py --augment <sqlglot_repo> maps/blast_maps.pkl.zlib [cache_dir]
"""
from __future__ import annotations

import ast
import builtins
import concurrent.futures
import csv
import enum
import gzip
import hashlib
import importlib
import io
import json
import math
import os
import pathlib
import pickle
import pkgutil
import re
import subprocess
import sys
import time
import unittest
import zlib
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple


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


def _install_unittest_lifecycle_hook(
    enter_ctx: Callable[[str], str],
    exit_ctx: Callable[[str], None],
) -> Tuple[Dict[type, str], Callable[[], None]]:
    """Wrap setUpClass and tearDownClass BEFORE pytest binds them during collection.

    In pytest 9, UnitTestCase._register_unittest_setup_class_fixture captures
    `setup = getattr(cls, "setUpClass", None)` during collection, before
    pytest_collection_modifyitems runs. Wrapping during collection ensures class
    setup/teardown lines and file/class hooks run under `__CLS__::<qualname>`.
    """
    import _pytest.unittest as _pu

    cls_to_tag: Dict[type, str] = {}
    wrapped: Set[Tuple[type, str]] = set()
    originals: List[Tuple[type, str, Any]] = []

    def _make_wrapped(fn: Callable[..., Any]) -> classmethod:
        @classmethod  # type: ignore[misc]
        def _wrapped(c: type) -> Any:
            tag = cls_to_tag.get(c) or f"__CLS__::{c.__module__}.{c.__qualname__}"
            prev = enter_ctx(tag)
            try:
                return fn(c)
            finally:
                exit_ctx(prev)

        return _wrapped

    orig_reg = _pu.UnitTestCase._register_unittest_setup_class_fixture

    def _hooked_register(self_ut: Any, cls: type) -> Any:
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
        return orig_reg(self_ut, cls)

    _pu.UnitTestCase._register_unittest_setup_class_fixture = _hooked_register  # type: ignore[method-assign]

    def _restore() -> None:
        _pu.UnitTestCase._register_unittest_setup_class_fixture = orig_reg  # type: ignore[method-assign]
        for owner, attr_name, orig_desc in reversed(originals):
            setattr(owner, attr_name, orig_desc)

    return cls_to_tag, _restore


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
                cls = getattr(item, "cls", None)
                if cls is not None:
                    cls_to_nodeids[cls].append(item.nodeid)

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

    cls_to_tag, restore_lifecycle = _install_unittest_lifecycle_hook(_enter_ctx, _exit_ctx)
    try:
        rc = pytest.main(["-q", "--no-header", "tests"], plugins=[CovPlugin()])
    finally:
        restore_lifecycle()
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
    if isinstance(k, enum.Enum):
        name = getattr(k, "name", None)
        if isinstance(name, str) and name in type(k).__members__:
            return f"enum:{type(k).__name__}.{name}"
        return None
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

    def _log_dict_all(td_id: int, mro_keys: Tuple[str, ...], attr: str) -> None:
        ctx = state["ctx"]
        if not ctx:
            return
        cache_k = (td_id, "__all__")
        seen = state["seen_dict_keys"]
        if cache_k in seen:
            return
        seen.add(cache_k)
        by_attr = raw_dict_usage[attr]
        for ckey in mro_keys:
            by_attr[(ckey, "__all__")].add(ctx)

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

        def __iter__(self):
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict.__iter__(self)

        def keys(self):
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict.keys(self)

        def items(self):
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict.items(self)

        def values(self):
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict.values(self)

        def copy(self) -> dict:
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict(dict.items(self))

        def __or__(self, other):
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict.__or__(self, other)

        def __ror__(self, other):
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict.__ror__(self, other)

        def __eq__(self, other):
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict.__eq__(self, other)

        def __ne__(self, other):
            if state["ctx"]:
                _log_dict_all(self._td_id, self._mro, self._attr)
            return dict.__ne__(self, other)

        def __reduce__(self):
            return (dict, (dict(dict.items(self)),))

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

    def _enter_p2(tag: str) -> str:
        prev = state["ctx"]
        if tag != prev:
            _switch(tag)
        return prev

    def _exit_p2(prev: str) -> None:
        if state["ctx"] != prev:
            _switch(prev)

    cls_to_tag, restore_lifecycle = _install_unittest_lifecycle_hook(_enter_p2, _exit_p2)

    class HookPlugin:
        def pytest_collection_modifyitems(self, items):
            for item in items:
                cls = getattr(item, "cls", None)
                if cls is not None:
                    cls_to_nodeids[cls].append(item.nodeid)

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
        restore_lifecycle()
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

        def _add_dep(src_rel: str, dep_rel: Optional[str]) -> None:
            if not dep_rel or dep_rel == src_rel:
                return
            direct[src_rel].add(dep_rel)
            parts = dep_rel.split("/")
            # Importing a.b.c in Python executes ancestor a/__init__.py, a/b/__init__.py first.
            for i in range(1, len(parts)):
                pkg_init = "/".join(parts[:i]) + "/__init__.py"
                if pkg_init in rel_set and pkg_init != src_rel:
                    direct[src_rel].add(pkg_init)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _add_dep(rel, _mod_to_rel(alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    up = node.level - 1
                    base_parts = pkg_parts[: len(pkg_parts) - up] if up <= len(pkg_parts) else []
                    base_mod = ".".join(base_parts + ([node.module] if node.module else []))
                else:
                    base_mod = node.module or ""
                if base_mod:
                    trel = _mod_to_rel(base_mod)
                    _add_dep(rel, trel)
                    if trel and trel != rel and trel.startswith("sqlglot/typing/") and rel.startswith("sqlglot/dialects/") and cls_names:
                        for cname in cls_names:
                            typing_links[trel].add(f"{rel}::{cname}")
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    sub_mod = f"{base_mod}.{alias.name}" if base_mod else alias.name
                    _add_dep(rel, _mod_to_rel(sub_mod))

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


def build_lazy_test_importers(repo: pathlib.Path, rev_imports: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Test files that reach a module ONLY through dynamic/lazy imports.

    A package __init__ with a module-level __getattr__ (PEP 562), or a module calling
    importlib.import_module(f"pkg.{...}"), loads sibling submodules lazily (sqlglot.dialects resolving
    `from sqlglot.dialects import Athena`, Dialect._try_load). Static rev_imports misses those edges. Kept as a
    separate key (used only for once-per-process escalation) so ordinary importer fallbacks stay narrow.
    """
    py_files: Dict[str, str] = {}
    for top in ("sqlglot", "tests"):
        for p in sorted((repo / top).rglob("*.py")):
            py_files[p.relative_to(repo).as_posix()] = p.read_text("utf-8", errors="replace")
    rel_set = set(py_files)
    lazy_edges: Dict[str, Set[str]] = defaultdict(set)  # sub-module -> lazy loader module
    for rel, src in py_files.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lazy_pkgs: Set[str] = set()
        if rel.endswith("/__init__.py") and any(
            isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in tree.body
        ):
            lazy_pkgs.add(rel.rsplit("/", 1)[0])
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Attribute, ast.Name))
                and (node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id) == "import_module"
                and node.args
                and isinstance(node.args[0], ast.JoinedStr)
                and node.args[0].values
                and isinstance(node.args[0].values[0], ast.Constant)
                and isinstance(node.args[0].values[0].value, str)
                and node.args[0].values[0].value.endswith(".")
            ):
                lazy_pkgs.add(node.args[0].values[0].value[:-1].replace(".", "/"))
        for pkg in lazy_pkgs:
            for sub in rel_set:
                if sub != rel and sub.startswith(pkg + "/") and sub.count("/") == pkg.count("/") + 1:
                    lazy_edges[sub].add(rel)
    out: Dict[str, List[str]] = {}
    for target in sorted(rel_set):
        static = set(rev_imports.get(target, ()))
        frontier = static | {target}
        extra: Set[str] = set()
        for mod in frontier:
            for loader in lazy_edges.get(mod, ()):
                extra.add(loader)
                extra.update(rev_imports.get(loader, ()))
        tests = sorted(x for x in extra - static if x.startswith("tests/"))
        if tests:
            out[target] = tests
    return out


_ONCE_PROBE_SCRIPT = r'''
import importlib, importlib.abc, importlib.machinery, json, os, pathlib, pkgutil, sys, warnings
warnings.simplefilter("ignore")
repo, out = sys.argv[1], sys.argv[2]
repo_path = pathlib.Path(repo).resolve()
sqlglot_root = (repo_path / "sqlglot").resolve()
sys.path.insert(0, str(repo_path))
os.chdir(repo_path)
import coverage
cov = coverage.Coverage(data_file=None, source=[os.path.join(str(repo_path), "sqlglot")], branch=False)
cov.start()

_ctx_stack = [""]

def _set_base_ctx(tag):
    _ctx_stack[0] = tag
    cov.switch_context(tag)

class _ImpFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return None
        orig = pathlib.Path(spec.origin).resolve()
        if not orig.is_relative_to(sqlglot_root):
            return None
        rel_origin = orig.relative_to(repo_path).as_posix()
        loader = spec.loader
        if loader is not None and not getattr(loader, "_blast_imp_wrapped", False):
            orig_exec = loader.exec_module
            def _wrapped_exec(mod, _orig=orig_exec, _ctx="imp:" + rel_origin):
                _ctx_stack.append(_ctx)
                cov.switch_context(_ctx)
                try:
                    return _orig(mod)
                finally:
                    _ctx_stack.pop()
                    cov.switch_context(_ctx_stack[-1])
            loader.exec_module = _wrapped_exec
            loader._blast_imp_wrapped = True
        return spec

sys.meta_path.insert(0, _ImpFinder())

_set_base_ctx("imp")
import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect, Dialects
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass

tests_root = repo_path / "tests"
for test_file in sorted(tests_root.rglob("test_*.py"), key=lambda p: p.as_posix()):
    rel = test_file.relative_to(repo_path).as_posix()
    modname = rel[:-3].replace("/", ".")
    _set_base_ctx("col:" + rel)
    try:
        importlib.import_module(modname)
    except Exception:
        pass

SQLS = [
    "SELECT a, b + 1 AS c, 'x' AS s, CAST(d AS INT) FROM t AS x JOIN u ON x.a = u.a WHERE c = 1 GROUP BY a ORDER BY b LIMIT 5",
    "INSERT INTO t (a) VALUES (1)",
    "CREATE TABLE t (a INT, b VARCHAR(10))",
    "WITH q AS (SELECT 1 AS a) SELECT SUM(a) OVER (PARTITION BY a) FROM q UNION ALL SELECT 2",
]

def _round(tag):
    _set_base_ctx(tag)
    from sqlglot.optimizer import optimize
    for d in [""] + [x.value for x in Dialects if x.value]:
        try:
            dia = Dialect.get_or_raise(d)
            dia.tokenizer_class()
            dia.parser_class()
            dia.generator_class()
            for sql in SQLS:
                try:
                    for e in dia.parse(sql):
                        if e is not None:
                            e.sql(dialect=dia)
                            e.sql(dialect=dia, pretty=True)
                except Exception:
                    pass
        except Exception:
            pass
    try:
        optimize(sqlglot.parse_one(SQLS[0]), schema={"t": {"a": "INT", "b": "INT", "d": "TEXT"}, "u": {"a": "INT"}})
    except Exception:
        pass

_round("r1")
_round("r2")
cov.stop()
data = cov.get_data()
once_out = {}
collect_out = {}
trig_out = {}
for fname in data.measured_files():
    rel = os.path.relpath(fname, str(repo_path)).replace(os.sep, "/")
    if not rel.startswith("sqlglot/"):
        continue
    impL, r1, r2 = set(), set(), set()
    col_map = {}
    trig_map = {}
    by_ln = data.contexts_by_lineno(fname)
    for ln, ctxs in by_ln.items():
        if any(c == "imp" or c.startswith("imp:") for c in ctxs):
            impL.add(ln)
        if "r1" in ctxs:
            r1.add(ln)
        if "r2" in ctxs:
            r2.add(ln)
        cols = sorted(c[4:] for c in ctxs if c.startswith("col:"))
        if cols:
            col_map[str(ln)] = cols
    once = sorted((impL - r2) | (r1 - r2))
    for ln in sorted(impL & r2):
        ctxs = by_ln.get(ln, ())
        trigs = sorted({c[4:] for c in ctxs if c.startswith("imp:")})
        if not trigs:
            trigs = [rel]
        trig_map[str(ln)] = trigs
    if once:
        once_out[rel] = once
    if col_map:
        collect_out[rel] = col_map
    if trig_map:
        trig_out[rel] = trig_map
with open(out, "w") as f:
    json.dump({"once": once_out, "collect": collect_out, "trig": trig_out}, f)
'''


def run_once_lines_probe(
    repo: pathlib.Path, cache_dir: pathlib.Path
) -> Tuple[Dict[str, List[int]], Dict[str, Dict[int, List[str]]], Dict[str, Dict[int, List[str]]]]:
    """Detect lines executed once per process, during test collection, or triggered by importing a module.

    - `once_lines`: lines in `(impL - r2) | (r1 - r2)`.
    - `collection_lines`: {rel_path: {lineno: [test_rel_path, ...]}} hit while importing each `tests/**/test_*.py`.
    - `import_trigger_lines`: {rel_path: {lineno: [trigger_rel_path, ...]}} for runtime-executed lines (`impL & r2`).
    """
    import subprocess

    out = cache_dir / "once_lines.json"
    script = cache_dir / "once_probe.py"
    script.write_text(_ONCE_PROBE_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["SKIP_INTEGRATION"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    t0 = time.perf_counter()
    subprocess.check_call([sys.executable, str(script), str(repo), str(out)], env=env, cwd=str(repo))
    res = json.loads(out.read_text("utf-8"))
    once_lines = {k: [int(x) for x in v] for k, v in res.get("once", {}).items()}
    collection_lines = {
        k: {int(ln): list(tfs) for ln, tfs in d.items()} for k, d in res.get("collect", {}).items()
    }
    import_trigger_lines = {
        k: {int(ln): list(trigs) for ln, trigs in d.items()} for k, d in res.get("trig", {}).items()
    }
    col_line_cnt = sum(len(d) for d in collection_lines.values())
    trig_line_cnt = sum(len(d) for d in import_trigger_lines.values())
    print(
        f"[Pass 4] once+collect+trig probe: once={len(once_lines)} files / {sum(len(v) for v in once_lines.values())} lines, "
        f"collect={len(collection_lines)} files / {col_line_cnt} lines, "
        f"trig={len(import_trigger_lines)} files / {trig_line_cnt} lines in {time.perf_counter() - t0:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return once_lines, collection_lines, import_trigger_lines


_IDENT_STR_RE = re.compile(r"^[A-Za-z_]\w*$")
_WORD_RE = re.compile(r"\w+")
_SAFE_DICT_CALL_FUNCS = frozenset({"len", "bool", "sorted", "set", "list", "tuple", "frozenset"})


def build_vocab_sets(repo: pathlib.Path) -> Tuple[Set[str], Set[str]]:
    """Scan sqlglot/ and tests/ .py files for identifier string constants and known word tokens."""
    str_const_idents: Set[str] = set()
    known_words: Set[str] = set()
    for top in ("sqlglot", "tests"):
        for p in sorted((repo / top).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            try:
                tree = ast.parse(p.read_text("utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    s = node.value
                    if _IDENT_STR_RE.match(s):
                        str_const_idents.add(s)
                    known_words.update(_WORD_RE.findall(s))
                elif isinstance(node, ast.Name):
                    known_words.add(node.id)
                elif isinstance(node, ast.Attribute):
                    known_words.add(node.attr)
                elif isinstance(node, ast.arg):
                    known_words.add(node.arg)
                elif isinstance(node, ast.alias):
                    known_words.add(node.name)
                    known_words.update(node.name.split("."))
                    if node.asname:
                        known_words.add(node.asname)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    known_words.update(node.module.split("."))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    known_words.add(node.name)
    return str_const_idents, known_words


def build_dict_value_import_reads(repo: pathlib.Path, once_union: Dict[str, Set[int]]) -> Set[str]:
    """Identify UPPER attr names read by value on import-time / once-only lines inside sqlglot/."""
    unsafe: Set[str] = set()

    def _enclosing_scope(node: ast.AST, parent: Dict[ast.AST, ast.AST]) -> Optional[ast.AST]:
        cur = parent.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Module)):
                return cur
            cur = parent.get(cur)
        return None

    def _loads_in_scope(scope: ast.AST, ident: str) -> List[ast.Name]:
        out: List[ast.Name] = []
        for sub in ast.walk(scope):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id == ident:
                out.append(sub)
        return out

    def _is_keys_only_ok(
        node: ast.AST,
        attr_name: str,
        scope: Optional[ast.AST],
        parent: Dict[ast.AST, ast.AST],
        depth: int,
    ) -> bool:
        p = parent.get(node)
        if p is None:
            return False
        if isinstance(p, ast.Compare):
            return any(comp is node and isinstance(op, (ast.In, ast.NotIn)) for op, comp in zip(p.ops, p.comparators))
        if isinstance(p, (ast.For, ast.AsyncFor, ast.comprehension)) and p.iter is node:
            return True
        if isinstance(p, ast.Call):
            if (
                isinstance(p.func, ast.Name)
                and p.func.id in _SAFE_DICT_CALL_FUNCS
                and len(p.args) == 1
                and p.args[0] is node
                and not p.keywords
            ):
                return True
            if node in p.args:
                return _is_safe_handoff_call(p)
        if isinstance(p, ast.keyword) and p.value is node:
            gp = parent.get(p)
            if isinstance(gp, ast.Call):
                return _is_safe_handoff_call(gp)
            return False
        if isinstance(p, (ast.If, ast.While, ast.IfExp)) and p.test is node:
            return True
        if isinstance(p, ast.BoolOp):
            return True
        if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not) and p.operand is node:
            return True
        if isinstance(p, ast.Attribute) and p.value is node and p.attr == "keys":
            gp = parent.get(p)
            return isinstance(gp, ast.Call) and gp.func is p
        if isinstance(p, ast.Dict) and isinstance(scope, ast.ClassDef):
            if any(k is None and v is node for k, v in zip(p.keys, p.values)):
                gp = parent.get(p)
                if parent.get(gp) is scope:  # directly in ClassDef body
                    if isinstance(gp, ast.Assign) and gp.value is p and len(gp.targets) == 1 and isinstance(gp.targets[0], ast.Name) and gp.targets[0].id == attr_name:
                        return True
                    if isinstance(gp, ast.AnnAssign) and gp.value is p and isinstance(gp.target, ast.Name) and gp.target.id == attr_name:
                        return True
        if depth == 0 and scope is not None and isinstance(p, ast.Assign) and p.value is node and len(p.targets) == 1 and isinstance(p.targets[0], ast.Name):
            alias_name = p.targets[0].id
            loads = _loads_in_scope(scope, alias_name)
            if not loads:
                return False
            return all(_is_keys_only_ok(ld, attr_name, scope, parent, depth + 1) for ld in loads)
        return False

    def _is_safe_handoff_call(call: ast.Call) -> bool:
        fn = call.func
        if isinstance(fn, ast.Name) and fn.id in {"dict", "copy", "deepcopy"}:
            return False
        if isinstance(fn, ast.Attribute) and fn.attr in {"copy", "update", "deepcopy"}:
            return False
        return True

    for p in sorted((repo / "sqlglot").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(repo).as_posix()
        once_lines = once_union.get(rel)
        if not once_lines:
            continue
        try:
            tree = ast.parse(p.read_text("utf-8", errors="replace"))
        except SyntaxError:
            continue
        parent: Dict[ast.AST, ast.AST] = {}
        for par in ast.walk(tree):
            for ch in ast.iter_child_nodes(par):
                parent[ch] = par
        for node in ast.walk(tree):
            ln = getattr(node, "lineno", 0)
            if ln not in once_lines:
                continue
            attr_name: Optional[str] = None
            scope = _enclosing_scope(node, parent)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr.isupper():
                attr_name = node.attr
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id.isupper() and isinstance(scope, ast.ClassDef):
                attr_name = node.id
            if attr_name is None:
                continue
            if not _is_keys_only_ok(node, attr_name, scope, parent, 0):
                unsafe.add(attr_name)
    return unsafe


def _audit_ast_canon_dict_key(node: ast.AST) -> Optional[str]:
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
            ce = _audit_ast_canon_dict_key(elt)
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


def _audit_strip_key_prefixes(norm_key: str) -> str:
    return re.sub(r"\b(?:enum|int|str|cls):", "", norm_key)


def _audit_extract_single_assign(stmt: ast.stmt) -> Optional[Tuple[str, ast.AST]]:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        return stmt.target.id, stmt.value
    return None


def build_dict_key_mismatch_attrs(
    repo: pathlib.Path,
    dict_key_usage: Dict[str, Dict[str, Dict[str, Any]]],
    tracked_classes: Any,
) -> Set[str]:
    """Detect tracked dict attrs whose static AST keys disagree in prefix/format with runtime dict_key_usage."""
    tracked_set = set(tracked_classes)
    mismatch: Set[str] = set()
    # Any runtime int: non-numeric or Enum-like str:TokenType./str:DType. key indicates normalization mismatch.
    bad_int_re = re.compile(r"(?:^|\()int:(?!-?\d+(?:$|\)|,))")
    bad_enum_str_re = re.compile(r"(?:^|\()str:(?:TokenType|DType)\.")
    for attr, by_cls in dict_key_usage.items():
        for kv in by_cls.values():
            for rk in kv:
                if bad_int_re.search(rk) or bad_enum_str_re.search(rk):
                    mismatch.add(attr)
                    break

    global_stripped: Dict[str, Dict[str, Set[str]]] = {}
    for attr, by_cls in dict_key_usage.items():
        sm: Dict[str, Set[str]] = defaultdict(set)
        for kv in by_cls.values():
            for rk in kv:
                if rk != "__all__":
                    sm[_audit_strip_key_prefixes(rk)].add(rk)
        global_stripped[attr] = sm

    def _scan_body(body: List[ast.stmt], rel: str, cls_stack: List[str]) -> None:
        if cls_stack:
            counts: Dict[str, int] = defaultdict(int)
            for st in body:
                sa = _audit_extract_single_assign(st)
                if sa is not None:
                    counts[sa[0]] += 1
            cls_key = f"{rel}::{'.'.join(cls_stack)}"
            if cls_key in tracked_set:
                for st in body:
                    sa = _audit_extract_single_assign(st)
                    if sa is None or not isinstance(sa[1], ast.Dict):
                        continue
                    attr, dnode = sa
                    if counts[attr] != 1 or attr not in dict_key_usage:
                        continue
                    by_cls = dict_key_usage.get(attr, {}).get(cls_key, {})
                    local_stripped: Dict[str, Set[str]] = defaultdict(set)
                    for rk in by_cls:
                        if rk != "__all__":
                            local_stripped[_audit_strip_key_prefixes(rk)].add(rk)
                    sm_attr = global_stripped.get(attr, {})
                    for k in dnode.keys:
                        if k is None:
                            continue
                        ck = _audit_ast_canon_dict_key(k)
                        if ck is None or ck in by_cls:
                            continue
                        sk = _audit_strip_key_prefixes(ck)
                        alts = (local_stripped.get(sk, set()) or sm_attr.get(sk, set())) - {ck}
                        if alts:
                            mismatch.add(attr)
                            break
        for st in body:
            if isinstance(st, ast.ClassDef):
                cls_stack.append(st.name)
                _scan_body(st.body, rel, cls_stack)
                cls_stack.pop()

    for p in sorted((repo / "sqlglot").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(repo).as_posix()
        try:
            tree = ast.parse(p.read_text("utf-8", errors="replace"), filename=rel)
        except SyntaxError:
            continue
        _scan_body(tree.body, rel, [])
    return mismatch


def build_path_mentions(repo: pathlib.Path) -> Dict[str, List[str]]:
    """Top-level repo entries outside sqlglot/ & tests/ (e.g. sqlglotc/, setup.py, pyproject.toml) -> test
    files whose source mentions that name (e.g. tests/test_sqlglotc.py runs sqlglotc/setup.py via runpy,
    which neither coverage of sqlglot/ nor the open() hooks attribute)."""
    tops = sorted(
        p.name
        for p in repo.iterdir()
        if p.name not in ("sqlglot", "tests") and not p.name.startswith(".")
    )
    texts = {
        p.relative_to(repo).as_posix(): p.read_text("utf-8", errors="replace")
        for p in sorted((repo / "tests").rglob("*.py"))
    }
    out: Dict[str, List[str]] = {}
    for top in tops:
        stem = top.rsplit(".", 1)[0] if "." in top else top
        needles = {f'"{top}"', f"'{top}'", f"/{top}", f'"{stem}"', f"'{stem}'"}
        hits = sorted(rel for rel, txt in texts.items() if any(n in txt for n in needles))
        if hits:
            out[top] = hits
    return out


_SITECUSTOMIZE_CODE = """\
import os

if os.environ.get("BLAST_EQ_OUT") and os.environ.get("BLAST_EQ_REPO"):
    import eqsite_hook

    eqsite_hook.install()
"""

_EQSITE_HOOK_CODE = r'''
from __future__ import annotations

import ast
import atexit
import builtins
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import sys
import types
from typing import Any, Dict, Optional, Set, Tuple

_OP_NAMES: Dict[type, str] = {
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.Eq: "==",
    ast.NotEq: "!=",
}
_ALLOWED_OWNERS = frozenset({object, type(None), bool, int, float, complex, str, bytes})
_FAST_SAFE_TYPES = frozenset({type(None), bool, int, float, complex, str, bytes})
_METHOD_TYPES = (types.FunctionType, classmethod, staticmethod)

_seen: Set[Tuple[str, int, int, int, int]] = set()
_unsafe: Set[Tuple[str, int, int, int, int]] = set()
_transformed_modules: Set[str] = set()
_type_safe_cache: Dict[type, bool] = {}
_installed = False


def _is_type_safe(cls: type) -> bool:
    if cls in _FAST_SAFE_TYPES:
        return True
    cached = _type_safe_cache.get(cls)
    if cached is not None:
        return cached
    ok = True
    for aname in ("__eq__", "__ne__"):
        owner = None
        val = None
        for k in cls.__mro__:
            d = k.__dict__
            if aname in d:
                owner = k
                val = d[aname]
                break
        if owner not in _ALLOWED_OWNERS or isinstance(val, _METHOD_TYPES):
            ok = False
            break
    _type_safe_cache[cls] = ok
    return ok


def _cmp(rel: str, lineno: int, col: int, end_lineno: int, end_col: int, op: str, a: Any, b: Any) -> Any:
    key = (rel, lineno, col, end_lineno, end_col)
    _seen.add(key)
    try:
        if op == "is":
            res = a is b
        elif op == "is not":
            res = a is not b
        elif op == "==":
            res = a == b
        else:
            res = a != b
    except BaseException:
        _unsafe.add(key)
        raise

    if key not in _unsafe:
        if not (_is_type_safe(type(a)) and _is_type_safe(type(b))):
            _unsafe.add(key)
        else:
            try:
                if op == "is":
                    alt = a == b
                elif op == "is not":
                    alt = a != b
                elif op == "==":
                    alt = a is b
                else:
                    alt = a is not b
                if type(res) is not bool or type(alt) is not bool or res != alt:
                    _unsafe.add(key)
            except Exception:
                _unsafe.add(key)
    return res


class _EqTransformer(ast.NodeTransformer):
    def __init__(self, rel: str) -> None:
        self.rel = rel

    def _visit_arguments(self, args: ast.arguments) -> None:
        # Never transform type annotations on arguments; only defaults are evaluated at def time.
        args.defaults = [self.visit(d) for d in args.defaults]
        args.kw_defaults = [self.visit(d) if d is not None else None for d in args.kw_defaults]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        self._visit_arguments(node.args)
        node.body = [self.visit(s) for s in node.body]
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        self._visit_arguments(node.args)
        node.body = [self.visit(s) for s in node.body]
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.bases = [self.visit(b) for b in node.bases]
        node.keywords = [self.visit(k) for k in node.keywords]
        node.body = [self.visit(s) for s in node.body]
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        node.target = self.visit(node.target)
        if node.value is not None:
            node.value = self.visit(node.value)
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if len(node.ops) == 1 and len(node.comparators) == 1:
            op_name = _OP_NAMES.get(type(node.ops[0]))
            if op_name is not None:
                lineno = getattr(node, "lineno", 0) or 0
                col = getattr(node, "col_offset", 0) or 0
                end_lineno = getattr(node, "end_lineno", lineno) or lineno
                end_col = getattr(node, "end_col_offset", col) or col
                call = ast.Call(
                    func=ast.Name(id="__blast_cmp__", ctx=ast.Load()),
                    args=[
                        ast.Constant(self.rel),
                        ast.Constant(lineno),
                        ast.Constant(col),
                        ast.Constant(end_lineno),
                        ast.Constant(end_col),
                        ast.Constant(op_name),
                        node.left,
                        node.comparators[0],
                    ],
                    keywords=[],
                )
                return ast.copy_location(call, node)
        return node


def _transform_source(src: str, rel: str) -> ast.AST:
    tree = ast.parse(src)
    tree = _EqTransformer(rel).visit(tree)
    ast.fix_missing_locations(tree)
    return tree


class _EqSiteLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname: str, path: str, rel: str) -> None:
        super().__init__(fullname, path)
        self.rel = rel

    def get_code(self, fullname: str) -> Any:
        data = self.get_data(self.path)
        src = importlib.util.decode_source(data)
        tree = _transform_source(src, self.rel)
        _transformed_modules.add(self.rel)
        return compile(tree, self.path, "exec", dont_inherit=True)


class _EqSiteFinder(importlib.abc.MetaPathFinder):
    def __init__(self, repo_root: pathlib.Path) -> None:
        self.repo_root = repo_root.resolve()
        self.repo_sqlglot = (self.repo_root / "sqlglot").resolve()

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return None
        orig = pathlib.Path(spec.origin).resolve()
        if not orig.is_relative_to(self.repo_sqlglot):
            return None
        rel = orig.relative_to(self.repo_root).as_posix()
        spec.loader = _EqSiteLoader(fullname, str(orig), rel)
        return spec


def dump_records() -> Optional[pathlib.Path]:
    out_dir = os.environ.get("BLAST_EQ_OUT")
    if not out_dir:
        return None
    out_path = pathlib.Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    rec_file = out_path / f"rec.{os.getpid()}.json"
    tmp_file = out_path / f".rec.{os.getpid()}.tmp"
    payload = {
        "pid": os.getpid(),
        "modules": sorted(_transformed_modules),
        "seen": [list(x) for x in sorted(_seen)],
        "unsafe": [list(x) for x in sorted(_unsafe)],
    }
    tmp_file.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp_file, rec_file)
    return rec_file


def install() -> None:
    global _installed
    if _installed:
        return
    repo_str = os.environ.get("BLAST_EQ_REPO")
    if not repo_str:
        return
    _installed = True
    sys.dont_write_bytecode = True
    builtins.__blast_cmp__ = _cmp  # type: ignore[attr-defined]
    sys.meta_path.insert(0, _EqSiteFinder(pathlib.Path(repo_str)))
    atexit.register(dump_records)
'''


def _prepare_eqhook_dir(workdir: pathlib.Path, clean_records: bool = True) -> Tuple[pathlib.Path, pathlib.Path]:
    hook_dir = workdir / "eqhook"
    out_dir = workdir / "eqout"
    hook_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    if clean_records:
        for old in out_dir.glob("rec.*.json"):
            old.unlink(missing_ok=True)
        for old in out_dir.glob(".rec.*.tmp"):
            old.unlink(missing_ok=True)
    (hook_dir / "eqsite_hook.py").write_text(_EQSITE_HOOK_CODE.lstrip("\n"), encoding="utf-8")
    (hook_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE_CODE, encoding="utf-8")
    return hook_dir, out_dir


def build_subprocess_tests(repo: pathlib.Path, test_ids: Optional[List[str]] = None) -> List[str]:
    """Static AST fallback identifying test IDs that reference subprocess/Popen/os.system/sys.executable."""
    def _has_subproc_ref(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in ("subprocess", "Popen"):
                return True
            if isinstance(sub, ast.Attribute):
                if isinstance(sub.value, ast.Name):
                    if sub.value.id == "os" and sub.attr == "system":
                        return True
                    if sub.value.id == "sys" and sub.attr == "executable":
                        return True
                    if sub.value.id == "subprocess":
                        return True
                if sub.attr == "Popen":
                    return True
        return False

    matched_prefixes: Set[str] = set()
    for p in sorted((repo / "tests").rglob("*.py")):
        rel = p.relative_to(repo).as_posix()
        try:
            tree = ast.parse(p.read_text("utf-8", errors="replace"))
        except SyntaxError:
            continue
        file_level_hit = False
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                cls_other_hit = False
                for body_stmt in stmt.body:
                    if isinstance(body_stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and body_stmt.name.startswith("test_"):
                        if _has_subproc_ref(body_stmt):
                            matched_prefixes.add(f"{rel}::{stmt.name}::{body_stmt.name}")
                    elif _has_subproc_ref(body_stmt):
                        cls_other_hit = True
                if cls_other_hit:
                    matched_prefixes.add(f"{rel}::{stmt.name}::")
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name.startswith("test_"):
                if _has_subproc_ref(stmt):
                    matched_prefixes.add(f"{rel}::{stmt.name}")
            elif _has_subproc_ref(stmt):
                file_level_hit = True
        if file_level_hit:
            matched_prefixes.add(f"{rel}::")

    if test_ids is None:
        return sorted(matched_prefixes)

    out: List[str] = []
    for tid in test_ids:
        for pref in matched_prefixes:
            if pref.endswith("::"):
                if tid.startswith(pref):
                    out.append(tid)
                    break
            elif tid == pref or tid.startswith(pref + "["):
                out.append(tid)
                break
    return sorted(out)


def _site_dict_to_frozensets(raw: Dict[str, List[List[int]]]) -> Dict[str, Any]:
    return {
        rel: frozenset((int(x[0]), int(x[1]), int(x[2]), int(x[3])) for x in coords)
        for rel, coords in raw.items()
    }


def _main_eqsites() -> None:
    import pytest

    repo = pathlib.Path(sys.argv[2]).resolve()
    out_json = pathlib.Path(sys.argv[3]).resolve()
    workdir = pathlib.Path(sys.argv[4]).resolve()
    targets = sys.argv[5:] or ["tests"]

    hook_dir, out_dir = _prepare_eqhook_dir(workdir, clean_records=True)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["BLAST_EQ_OUT"] = str(out_dir)
    os.environ["BLAST_EQ_REPO"] = str(repo)
    py_bin = str(pathlib.Path(sys.executable).parent)
    old_path = os.environ.get("PATH", "")
    if py_bin not in old_path.split(os.pathsep):
        os.environ["PATH"] = f"{py_bin}{os.pathsep}{old_path}" if old_path else py_bin
    old_pp = os.environ.get("PYTHONPATH", "")
    pp_parts = [p for p in (str(hook_dir), str(repo)) if p]
    if old_pp:
        pp_parts.append(old_pp)
    os.environ["PYTHONPATH"] = os.pathsep.join(pp_parts)

    if str(hook_dir) not in sys.path:
        sys.path.insert(0, str(hook_dir))
    import eqsite_hook  # type: ignore[import-not-found]

    eqsite_hook.install()

    os.chdir(repo)
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    _install_sync_executor()

    failed_nodeids: List[str] = []

    class _RecPlugin:
        def pytest_runtest_logreport(self, report):
            if report.failed:
                failed_nodeids.append(report.nodeid)

    t0 = time.perf_counter()
    print(f"[Pass 6 eqsites] Running instrumented pytest on {targets}...", file=sys.stderr, flush=True)
    rc = pytest.main(["-q", "--no-header", "-p", "no:cacheprovider", *targets], plugins=[_RecPlugin()])
    dt = time.perf_counter() - t0
    eqsite_hook.dump_records()

    rec_files = sorted(out_dir.glob("rec.*.json"))
    all_modules: Set[str] = set()
    all_seen: Set[Tuple[str, int, int, int, int]] = set()
    all_unsafe: Set[Tuple[str, int, int, int, int]] = set()
    main_pid = os.getpid()
    child_records: List[Dict[str, Any]] = []

    for rf in rec_files:
        data = json.loads(rf.read_text("utf-8"))
        mods = set(data.get("modules", ()))
        seen_t = {(str(x[0]), int(x[1]), int(x[2]), int(x[3]), int(x[4])) for x in data.get("seen", ())}
        uns_t = {(str(x[0]), int(x[1]), int(x[2]), int(x[3]), int(x[4])) for x in data.get("unsafe", ())}
        all_modules |= mods
        all_seen |= seen_t
        all_unsafe |= uns_t
        if int(data.get("pid", -1)) != main_pid:
            child_records.append({
                "file": rf.name,
                "pid": data.get("pid"),
                "modules_count": len(mods),
                "has_sqlglot_init": "sqlglot/__init__.py" in mods,
                "seen_count": len(seen_t),
                "seen_in_sqlglot_init": sum(1 for x in seen_t if x[0] == "sqlglot/__init__.py"),
                "unsafe_count": len(uns_t),
            })

    safe_set = all_seen - all_unsafe
    by_file: Dict[str, List[List[int]]] = defaultdict(list)
    for rel, l, c, el, ec in sorted(safe_set):
        by_file[rel].append([l, c, el, ec])

    stats = {
        "rc": int(rc),
        "failed_nodeids": sorted(set(failed_nodeids)),
        "duration_s": round(dt, 2),
        "transformed_modules_count": len(all_modules),
        "transformed_modules": sorted(all_modules),
        "record_files_count": len(rec_files),
        "child_records": child_records,
        "seen_count": len(all_seen),
        "unsafe_count": len(all_unsafe),
        "safe_count": len(safe_set),
    }
    print(
        f"[Pass 6 eqsites] rc={rc} failed={stats['failed_nodeids']} "
        f"modules={stats['transformed_modules_count']} rec_files={stats['record_files_count']} "
        f"child_recs={len(child_records)} seen={stats['seen_count']} "
        f"unsafe={stats['unsafe_count']} safe={stats['safe_count']} in {dt:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({"safe_sites": dict(by_file), "stats": stats}), encoding="utf-8")
    if rc != 0:
        raise RuntimeError(f"eqsites pytest failed rc={rc}, failed={stats['failed_nodeids']}")


def run_eqsite_probe(repo: pathlib.Path, cache_dir: pathlib.Path) -> Dict[str, Any]:
    """Run full-suite Compare site safety instrumentation in a subprocess and parse the result."""
    import subprocess

    workdir = cache_dir / "eq_work"
    out_json = cache_dir / "eq_sites.json"
    hook_dir, _ = _prepare_eqhook_dir(workdir, clean_records=True)
    env = dict(os.environ)
    env["SKIP_INTEGRATION"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["BLAST_EQ_OUT"] = str(workdir / "eqout")
    env["BLAST_EQ_REPO"] = str(repo)
    py_bin = str(pathlib.Path(sys.executable).parent)
    old_path = env.get("PATH", "")
    if py_bin not in old_path.split(os.pathsep):
        env["PATH"] = f"{py_bin}{os.pathsep}{old_path}" if old_path else py_bin
    old_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([str(hook_dir), str(repo)] + ([old_pp] if old_pp else []))
    cmd = [
        sys.executable,
        str(pathlib.Path(__file__).resolve()),
        "--eqsites-only",
        str(repo),
        str(out_json),
        str(workdir),
    ]
    subprocess.check_call(cmd, env=env, cwd=str(repo))
    return json.loads(out_json.read_text("utf-8"))


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


def run_tpcds_probe_maps(
    repo: pathlib.Path,
    test_ids: List[str],
    line_cov: Dict[str, Any],
) -> Dict[str, Any]:
    """Precompute TPC-DS/TPC-H oracle digests, per-query line coverage, and max_nodes file set."""
    import coverage

    t0 = time.perf_counter()
    os.environ["SKIP_INTEGRATION"] = "0"
    repo_str = str(repo.resolve())
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)

    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import Table, ensure_tables
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import ensure_schema
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, load_sql_fixture_pairs

    mn_tid = "tests/test_parser.py::TestParser::test_max_nodes"
    mn_bidx = test_ids.index(mn_tid)
    mn_files: Set[str] = {"sqlglot/parsers/athena.py", "sqlglot/errors.py"}
    for rp, lmap in line_cov.items():
        if not rp.startswith("sqlglot/"):
            continue
        for m in lmap.values():
            if (isinstance(m, int) and ((m >> mn_bidx) & 1)) or (not isinstance(m, int) and mn_bidx in m):
                mn_files.add(rp)
                break
    max_nodes_files = frozenset(sorted(mn_files))

    dir_tpcds = FIXTURES_DIR + "/optimizer/tpc-ds/"
    tpcds_tables: Dict[str, Any] = {}
    for table, columns in TPCDS_SCHEMA.items():
        fn = f"{dir_tpcds}{table}.csv.gz"
        with open(fn, "rb") as fb:
            is_gz = fb.read(2) == b"\x1f\x8b"
        opener = gzip.open(fn, "rt", newline="") if is_gz else open(fn, encoding="utf-8", newline="")
        with opener as f:
            reader = csv.reader(f, delimiter="|")
            rows: List[Tuple[Any, ...]] = []
            ctypes: List[Any] = []
            next(reader)
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
            tpcds_tables[table] = Table(columns=columns, rows=rows)

    tpcds_sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    tpch_sqls = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))

    tpcds_exec_qids: List[int] = []
    tpcds_digests: Dict[int, str] = {}
    s1_asts: Dict[int, Any] = {}
    s1_dks: Dict[int, str] = {}
    s2_opts: Dict[int, Any] = {}

    w_s2_ast = parse_one("SELECT 1")
    w_s2_schema = ensure_schema({"t": {"a": "INT"}}, dialect=None)
    w_s3_opt = optimize(
        parse_one("SELECT a FROM t WHERE a = 1"),
        w_s2_schema,
        leave_tables_isolated=True,
        dialect=None,
    )

    cov = coverage.Coverage(data_file=None, source=[str(repo / "sqlglot")], branch=False)
    cov.start()
    try:
        cov.switch_context("s1:init")
        _ = pickle.loads(pickle.dumps(parse_one("SELECT 1")))
        _ = transpile("SELECT 1", write="duckdb")[0]
        for qi, (meta, sql, _) in enumerate(tpcds_sqls):
            cov.switch_context(f"s1:{qi}")
            ast_q = pickle.loads(pickle.dumps(parse_one(sql)))
            s1_asts[qi] = ast_q
            if meta.get("execute"):
                tpcds_exec_qids.append(qi)
                s1_dks[qi] = transpile(sql, write="duckdb")[0]

        cov.switch_context("s2:init")
        schema_obj = ensure_schema(TPCDS_SCHEMA, dialect=None)
        _ = optimize(w_s2_ast.copy(), w_s2_schema, leave_tables_isolated=True, dialect=None)
        for qi in tpcds_exec_qids:
            cov.switch_context(f"s2:{qi}")
            schema_q = ensure_schema(TPCDS_SCHEMA, dialect=None)
            s2_opts[qi] = optimize(s1_asts[qi].copy(), schema_q, leave_tables_isolated=True, dialect=None)

        cov.switch_context("s3:init")
        w_tabs = ensure_tables({"t": Table(["a"], [(1,)])}, dialect=None)
        _ = PythonExecutor(tables=w_tabs).execute(Plan(w_s3_opt))
        for qi in tpcds_exec_qids:
            cov.switch_context(f"s3:{qi}")
            tabs = {t.name: tpcds_tables[t.name] for t in find_tables(s1_asts[qi])}
            tables_ = ensure_tables(tabs, dialect=None)
            if tables_.supported_table_args and tables_.supported_table_args != schema_obj.supported_table_args:
                raise RuntimeError("Tables must support the same table args as schema")
            tbl = PythonExecutor(tables=tables_).execute(Plan(s2_opts[qi]))
            c_cols = [str(c) for c in tbl.columns]
            c_rows = [[_canon_cell(x) for x in r] for r in tbl.rows]
            tpcds_digests[int(qi)] = _canon_digest([s1_dks[qi], c_cols, c_rows])
    finally:
        cov.stop()

    tpcds_opt_asts_zlib = zlib.compress(
        pickle.dumps({int(qi): s2_opts[qi] for qi in tpcds_exec_qids}, protocol=5),
        level=9,
    )
    roundtrip_opts = pickle.loads(zlib.decompress(tpcds_opt_asts_zlib))
    for qi in tpcds_exec_qids:
        tabs = {t.name: tpcds_tables[t.name] for t in find_tables(s1_asts[qi])}
        tables_ = ensure_tables(tabs, dialect=None)
        tbl = PythonExecutor(tables=tables_).execute(Plan(roundtrip_opts[qi]))
        c_cols = [str(c) for c in tbl.columns]
        c_rows = [[_canon_cell(x) for x in r] for r in tbl.rows]
        assert _canon_digest([s1_dks[qi], c_cols, c_rows]) == tpcds_digests[int(qi)], f"roundtrip mismatch q{qi}"

    tpcds_parse_digests: Dict[int, str] = {}
    tpcds_opt_digests: Dict[int, str] = {}
    for qi, _ in enumerate(tpcds_sqls):
        ast_q = s1_asts[qi]
        if qi in s1_dks:
            dk_sql = s1_dks[qi]
            tpcds_parse_digests[int(qi)] = _canon_digest([repr(ast_q), dk_sql])
            tpcds_opt_digests[int(qi)] = _canon_digest([repr(s2_opts[qi]), dk_sql])
        else:
            tpcds_parse_digests[int(qi)] = _canon_digest(repr(ast_q))

    # Trace TestOptimizer.test_tpcds (all 99 TPC-DS queries: parse o1 vs optimize+walk+sql(pretty=True) o2).
    import sqlglot as _sqlglot_mod
    from sqlglot import exp as _exp_mod, optimizer as _opt_pkg
    from sqlglot.schema import MappingSchema as _MappingSchema
    from tests.helpers import string_to_bool as _str_to_bool

    opt_tpcds_parse_digests: Dict[int, str] = {}
    o1_asts: Dict[int, Any] = {}
    prev_schema = getattr(_sqlglot_mod, "schema", None)
    _sqlglot_mod.schema = _MappingSchema()
    cov_opt = coverage.Coverage(data_file=None, source=[str(repo / "sqlglot")], branch=False)
    cov_opt.start()
    try:
        cov_opt.switch_context("o1:init")
        _ = parse_one("SELECT 1")
        for qi, (meta, sql, _) in enumerate(tpcds_sqls):
            cov_opt.switch_context(f"o1:{qi}")
            dialect = meta.get("dialect")
            ast_o = parse_one(sql, read=dialect)
            o1_asts[qi] = ast_o
            opt_tpcds_parse_digests[int(qi)] = _canon_digest(repr(ast_o))

        cov_opt.switch_context("o2:init")
        w_opt = _opt_pkg.optimize(parse_one("SELECT 1"), schema={"t": {"a": "INT"}})
        _ = w_opt.sql(pretty=True)
        for qi, (meta, sql, expected) in enumerate(tpcds_sqls):
            cov_opt.switch_context(f"o2:{qi}")
            dialect = meta.get("dialect")
            func_kwargs: Dict[str, Any] = {"schema": json.loads(meta["schema"]) if meta.get("schema") else TPCDS_SCHEMA}
            if meta.get("leave_tables_isolated") is not None:
                func_kwargs["leave_tables_isolated"] = _str_to_bool(meta["leave_tables_isolated"])
            if meta.get("validate_qualify_columns") is not None:
                func_kwargs["validate_qualify_columns"] = _str_to_bool(meta["validate_qualify_columns"])
            if meta.get("canonicalize_table_aliases") is not None:
                func_kwargs["canonicalize_table_aliases"] = _str_to_bool(meta["canonicalize_table_aliases"])
            if dialect:
                func_kwargs["dialect"] = dialect
            optimized = _opt_pkg.optimize(o1_asts[qi].copy(), **func_kwargs)
            for expression in optimized.walk():
                for arg_key, arg in expression.args.items():
                    for child in arg if isinstance(arg, list) else [arg]:
                        if isinstance(child, _exp_mod.Expr):
                            assert child.arg_key == arg_key
                            assert child.parent is expression
            actual = optimized.sql(pretty=True, dialect=dialect)
            assert actual == expected, f"opt_tpcds baseline mismatch q{qi}"
    finally:
        cov_opt.stop()
        _sqlglot_mod.schema = prev_schema

    opt_data = cov_opt.get_data()
    all_opt_mask = (1 << len(tpcds_sqls)) - 1
    repo_prefix = str(repo.resolve()) + os.sep
    opt_tpcds_qcov: Dict[str, Dict[int, int]] = {}
    opt_tpcds_s2_lines: Dict[str, List[int]] = {}
    for fname in sorted(opt_data.measured_files()):
        try:
            rfname = str(pathlib.Path(fname).resolve())
        except OSError:
            rfname = fname
        if not rfname.startswith(repo_prefix):
            continue
        rel = rfname[len(repo_prefix) :].replace(os.sep, "/")
        if not rel.startswith("sqlglot/"):
            continue
        oqmap: Dict[int, int] = {}
        os2set: Set[int] = set()
        for ln, ctxs in opt_data.contexts_by_lineno(fname).items():
            omask = 0
            in_o2 = False
            for c in ctxs:
                if not c:
                    continue
                if c == "o1:init":
                    omask |= all_opt_mask
                elif c == "o2:init":
                    omask |= all_opt_mask
                    in_o2 = True
                elif c.startswith("o1:"):
                    omask |= 1 << int(c[3:])
                elif c.startswith("o2:"):
                    omask |= 1 << int(c[3:])
                    in_o2 = True
            li = int(ln)
            if omask:
                oqmap[li] = omask
            if in_o2:
                os2set.add(li)
        if oqmap:
            opt_tpcds_qcov[rel] = dict(sorted(oqmap.items()))
        if os2set:
            opt_tpcds_s2_lines[rel] = sorted(os2set)

    data = cov.get_data()
    exec_set = set(tpcds_exec_qids)
    all_exec_mask = sum(1 << q for q in tpcds_exec_qids)
    tpcds_query_cov: Dict[str, Dict[int, int]] = {}
    tpcds_parse_cov: Dict[str, List[int]] = {}
    tpcds_s2_lines: Dict[str, List[int]] = {}
    tpcds_s3_lines: Dict[str, List[int]] = {}
    tpcds_s3_query_cov: Dict[str, Dict[int, int]] = {}
    for fname in sorted(data.measured_files()):
        try:
            rfname = str(pathlib.Path(fname).resolve())
        except OSError:
            rfname = fname
        if not rfname.startswith(repo_prefix):
            continue
        rel = rfname[len(repo_prefix) :].replace(os.sep, "/")
        if not rel.startswith("sqlglot/"):
            continue
        qmap: Dict[int, int] = {}
        pset: Set[int] = set()
        s2set: Set[int] = set()
        s3set: Set[int] = set()
        s3qmap: Dict[int, int] = {}
        for ln, ctxs in data.contexts_by_lineno(fname).items():
            emask = 0
            m3 = 0
            in_p = False
            in_s2 = False
            for c in ctxs:
                if not c:
                    continue
                if c == "s1:init":
                    emask |= all_exec_mask
                elif c == "s2:init":
                    emask |= all_exec_mask
                    in_s2 = True
                elif c == "s3:init":
                    emask |= all_exec_mask
                    m3 |= all_exec_mask
                elif c.startswith("s1:"):
                    q = int(c[3:])
                    if q in exec_set:
                        emask |= 1 << q
                    else:
                        in_p = True
                elif c.startswith("s2:"):
                    q = int(c[3:])
                    emask |= 1 << q
                    in_s2 = True
                elif c.startswith("s3:"):
                    q = int(c[3:])
                    emask |= 1 << q
                    m3 |= 1 << q
            li = int(ln)
            if emask:
                qmap[li] = emask
            if in_p:
                pset.add(li)
            if in_s2:
                s2set.add(li)
            if m3:
                s3set.add(li)
                s3qmap[li] = m3
        if qmap:
            tpcds_query_cov[rel] = dict(sorted(qmap.items()))
        if pset:
            tpcds_parse_cov[rel] = sorted(pset)
        if s2set:
            tpcds_s2_lines[rel] = sorted(s2set)
        if s3set:
            tpcds_s3_lines[rel] = sorted(s3set)
        if s3qmap:
            tpcds_s3_query_cov[rel] = dict(sorted(s3qmap.items()))

    tpch_digests = tuple(_canon_digest(pickle.loads(pickle.dumps(parse_one(sql))).sql()) for _, sql, _ in tpch_sqls)
    opt_tpch_digests = tuple(
        _canon_digest([transpile(sql, write="duckdb")[0], transpile(opt_sql, write="duckdb")[0]])
        for _, sql, opt_sql in tpch_sqls
    )
    elapsed = time.perf_counter() - t0
    print(
        f"[Pass 7 tpcds_probe] exec_q={len(tpcds_exec_qids)}/{len(tpcds_sqls)} "
        f"pdig={len(tpcds_parse_digests)} odig={len(tpcds_opt_digests)} opt_asts={len(tpcds_opt_asts_zlib)//1024}KiB "
        f"tpch={len(tpch_digests)} opt_tpch={len(opt_tpch_digests)} "
        f"qcov_files={len(tpcds_query_cov)} opt_tpcds_files={len(opt_tpcds_qcov)} "
        f"s2_files={len(tpcds_s2_lines)} s3_files={len(tpcds_s3_lines)} "
        f"pcov_files={len(tpcds_parse_cov)} mn_files={len(max_nodes_files)} in {elapsed:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return {
        "tpcds_exec_qids": tuple(tpcds_exec_qids),
        "tpcds_digests": tpcds_digests,
        "tpcds_parse_digests": tpcds_parse_digests,
        "tpcds_opt_digests": tpcds_opt_digests,
        "tpcds_opt_asts_zlib": tpcds_opt_asts_zlib,
        "tpcds_query_cov": tpcds_query_cov,
        "tpcds_parse_cov": tpcds_parse_cov,
        "tpcds_s2_lines": tpcds_s2_lines,
        "tpcds_s3_lines": tpcds_s3_lines,
        "tpcds_s3_query_cov": tpcds_s3_query_cov,
        "opt_tpcds_parse_digests": opt_tpcds_parse_digests,
        "opt_tpcds_qcov": opt_tpcds_qcov,
        "opt_tpcds_s2_lines": opt_tpcds_s2_lines,
        "opt_tpch_digests": opt_tpch_digests,
        "tpch_digests": tpch_digests,
        "max_nodes_files": max_nodes_files,
    }


def _main_tpcds_probe_only() -> None:
    repo = pathlib.Path(sys.argv[2]).resolve()
    maps_path = pathlib.Path(sys.argv[3]).resolve()
    os.chdir(repo)
    payload = pickle.loads(zlib.decompress(maps_path.read_bytes()))
    old_digests = dict(payload.get("tpcds_digests", {}))
    payload.update(run_tpcds_probe_maps(repo, payload["test_ids"], payload["line_cov"]))
    if old_digests:
        assert old_digests == payload["tpcds_digests"], "tpcds_digests mismatch against previous map!"
    payload["version"] = max(int(payload.get("version", 9)), 10)
    raw_bytes = pickle.dumps(payload, protocol=5)
    comp_bytes = zlib.compress(raw_bytes, level=9)
    assert len(comp_bytes) < 2 * 1024 * 1024, f"Map too large: {len(comp_bytes)} bytes"
    maps_path.write_bytes(comp_bytes)
    print(
        f"[DONE] Augmented tpcds_probe maps in {maps_path} ({len(comp_bytes) / 1024:.1f} KiB compressed, {len(raw_bytes) / 1024:.1f} KiB raw)",
        file=sys.stderr,
        flush=True,
    )


_FAST_PROBE_DISCOVERY_SCRIPT = r"""
import concurrent.futures, hashlib, importlib, io, json, os, subprocess, sys, tempfile, types, unittest
from collections import defaultdict

_WRITE_COUNT = 0
def _audit_open(ev, args):
    global _WRITE_COUNT
    if ev == "open" and len(args) >= 2 and isinstance(args[1], str):
        if any(c in args[1] for c in ("w", "a", "+", "x")):
            p = args[0]
            if p != os.devnull and not isinstance(p, int):
                _WRITE_COUNT += 1
sys.addaudithook(_audit_open)

payload_in = json.loads(sys.stdin.read())
repo = payload_in["repo"]
test_ids = payload_in["test_ids"]
special = {
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    "tests/test_optimizer.py::TestOptimizer::test_tpcds",
    "tests/test_optimizer.py::TestOptimizer::test_scope",
    "tests/test_optimizer.py::TestOptimizer::test_annotate_funcs",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
} | set(payload_in.get("subprocess_test_ids", ()))

class _MissingDepError(RuntimeError):
    pass

class _MissingStubObj:
    def __init__(self, name):
        self._name = name
    def __getattr__(self, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        return _MissingStubObj(f"{self._name}.{attr}")
    def __call__(self, *a, **kw):
        raise _MissingDepError(f"missing dep called: {self._name}")
    def __enter__(self):
        raise _MissingDepError(f"missing dep enter: {self._name}")
    def __exit__(self, *a):
        return False
    def __iter__(self):
        raise _MissingDepError(f"missing dep iter: {self._name}")
    def __bool__(self):
        raise _MissingDepError(f"missing dep bool: {self._name}")
    def __getitem__(self, key):
        raise _MissingDepError(f"missing dep getitem: {self._name}")

for root_mod in ("duckdb", "numpy", "pandas", "pandas.testing", "pytz", "dateutil", "dateutil.relativedelta"):
    try:
        importlib.import_module(root_mod)
        continue
    except ImportError:
        pass
    for existing in [k for k in list(sys.modules) if k == root_mod or k.startswith(root_mod + ".")]:
        del sys.modules[existing]
    m = types.ModuleType(root_mod)
    m.__path__ = []
    m.__getattr__ = lambda attr, _r=root_mod: (_ for _ in ()).throw(AttributeError(attr)) if (attr.startswith("__") and attr.endswith("__")) else _MissingStubObj(f"{_r}.{attr}")
    sys.modules[root_mod] = m
if "dateutil.relativedelta" in sys.modules:
    sys.modules["dateutil"].relativedelta = sys.modules["dateutil.relativedelta"]

for tf_attr in ("TemporaryDirectory", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile", "mkstemp", "mkdtemp"):
    if hasattr(tempfile, tf_attr):
        setattr(tempfile, tf_attr, _MissingStubObj(f"tempfile.{tf_attr}"))
for sp_attr in ("Popen", "run", "call", "check_call", "check_output"):
    if hasattr(subprocess, sp_attr):
        setattr(subprocess, sp_attr, _MissingStubObj(f"subprocess.{sp_attr}"))

class _SyncPoolExecutor(concurrent.futures.Executor):
    def submit(self, fn, /, *args, **kwargs):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            fut.set_exception(exc)
        return fut
    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

class _DFToken:
    __slots__ = ("sql_s",)
    def __init__(self, sql_s):
        self.sql_s = sql_s

class _ConnRecorder:
    def __init__(self):
        self.calls = []
        self.digests = []
    def execute(self, sql_s, *args, **kwargs):
        if args or kwargs or not isinstance(sql_s, str):
            raise _MissingDepError("conn.execute args")
        self.calls.append(sql_s)
        tok = _DFToken(sql_s)
        return types.SimpleNamespace(df=lambda: tok)
    def verify_pair(self, a, b, **kw):
        if kw or not isinstance(a, _DFToken) or not isinstance(b, _DFToken):
            raise _MissingDepError("assert_frame_equal args")
        self.digests.append(hashlib.sha256((a.sql_s + "\0" + b.sql_s).encode("utf-8")).hexdigest()[:16])

def _make_opt_duckdb_conn(mod_obj):
    dk = sys.modules.get("duckdb")
    if dk is None or not hasattr(dk, "__file__"):
        return None
    src = open(os.path.join(repo, "tests", "test_optimizer.py"), encoding="utf-8").read()
    i0 = src.index("CREATE TABLE x (a INT, b INT);")
    i1 = src.index("CROSS JOIN (VALUES (TRUE), (FALSE), (NULL)) AS z(c);") + len("CROSS JOIN (VALUES (TRUE), (FALSE), (NULL)) AS z(c);")
    conn = dk.connect(":memory:")
    conn.execute(src[i0:i1])
    return conn

class _StrictTestResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.any_bad = False
    def addError(self, test, err):
        self.any_bad = True
        super().addError(test, err)
    def addFailure(self, test, err):
        self.any_bad = True
        super().addFailure(test, err)
    def addSkip(self, test, reason):
        self.any_bad = True
        super().addSkip(test, reason)
    def addExpectedFailure(self, test, err):
        self.any_bad = True
        super().addExpectedFailure(test, err)
    def addUnexpectedSuccess(self, test):
        self.any_bad = True
        super().addUnexpectedSuccess(test)
    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.any_bad = True
        super().addSubTest(test, subtest, err)

sys.path.insert(0, repo)
by_cls = defaultdict(list)
for tid in test_ids:
    if tid in special:
        continue
    parts = tid.split("::")
    if len(parts) != 3:
        continue
    by_cls[(parts[0], parts[1])].append((tid, parts[2]))

fast_unprobed = []
opt_conn_digests = {}
orig_out, orig_err = sys.stdout, sys.stderr
sys.stdout = io.StringIO()
sys.stderr = io.StringIO()
opt_real_conn = None
try:
    for (tfile, cls_name), items in sorted(by_cls.items()):
        try:
            mod = importlib.import_module(tfile[:-3].replace("/", "."))
            cls = getattr(mod, cls_name)
        except BaseException:
            fast_unprobed.extend(tid for tid, _ in items)
            continue
        if getattr(cls, "__unittest_skip__", False):
            fast_unprobed.extend(tid for tid, _ in items)
            continue
        is_opt = (tfile == "tests/test_optimizer.py" and cls_name == "TestOptimizer")
        is_exec = (tfile == "tests/test_executor.py" and cls_name == "TestExecutor")
        orig_schema = None
        orig_afe = None
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
            fast_unprobed.extend(tid for tid, _ in items)
            continue

        try:
            for tid, meth_name in items:
                try:
                    w0 = _WRITE_COUNT
                    tc = cls(meth_name)
                    if getattr(tc, "__unittest_skip__", False) or getattr(getattr(cls, meth_name, None), "__unittest_skip__", False):
                        fast_unprobed.append(tid)
                        continue
                    rec = None
                    if is_opt:
                        rec = _ConnRecorder()
                        cls.conn = rec
                        mod.assert_frame_equal = rec.verify_pair
                    res = _StrictTestResult()
                    tc.run(res)
                    ok = (
                        res.testsRun == 1
                        and not res.any_bad
                        and not res.failures
                        and not res.errors
                        and not res.skipped
                        and res.wasSuccessful()
                        and _WRITE_COUNT == w0
                    )
                    if ok and rec is not None:
                        ok = len(rec.calls) == 2 * len(rec.digests)
                    if ok:
                        if rec is not None and rec.digests:
                            opt_conn_digests[tid] = rec.digests
                        continue
                    if is_opt and orig_afe is not None and not isinstance(orig_afe, _MissingStubObj):
                        try:
                            if opt_real_conn is None:
                                opt_real_conn = _make_opt_duckdb_conn(mod)
                            if opt_real_conn is not None:
                                w1 = _WRITE_COUNT
                                cls.conn = opt_real_conn
                                mod.assert_frame_equal = orig_afe
                                tc2 = cls(meth_name)
                                res2 = _StrictTestResult()
                                tc2.run(res2)
                                ok2 = (
                                    res2.testsRun == 1
                                    and not res2.any_bad
                                    and not res2.failures
                                    and not res2.errors
                                    and not res2.skipped
                                    and res2.wasSuccessful()
                                    and _WRITE_COUNT == w1
                                )
                                if ok2:
                                    continue
                        except BaseException:
                            pass
                    fast_unprobed.append(tid)
                except BaseException:
                    fast_unprobed.append(tid)
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
finally:
    sys.stdout, sys.stderr = orig_out, orig_err

sys.stdout.write(json.dumps({
    "fast_unprobed_ids": sorted(set(fast_unprobed)),
    "opt_conn_digests": opt_conn_digests,
}))
sys.stdout.flush()
"""


def run_fast_probe_maps(
    repo: pathlib.Path,
    test_ids: List[str],
    subprocess_test_ids: Sequence[str],
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    venv_py = pathlib.Path("/home/nikhi/rwx-venv/bin/python")
    py_exec = str(venv_py) if venv_py.exists() else sys.executable
    sub_env = {"HOME": "/tmp", "PATH": "/usr/bin:/bin", "SKIP_INTEGRATION": "0"}
    proc = subprocess.run(
        [py_exec, "-B", "-c", _FAST_PROBE_DISCOVERY_SCRIPT],
        input=json.dumps({
            "repo": str(repo),
            "test_ids": list(test_ids),
            "subprocess_test_ids": list(subprocess_test_ids),
        }),
        text=True,
        capture_output=True,
        env=sub_env,
        cwd=str(repo),
        check=True,
    )
    data = json.loads(proc.stdout)
    fast_unprobed_ids = frozenset(data["fast_unprobed_ids"])
    opt_conn_digests = {str(k): tuple(v) for k, v in data["opt_conn_digests"].items()}
    elapsed = time.perf_counter() - t0
    print(
        f"[Pass 8 fast_probe] py={py_exec} unprobed={len(fast_unprobed_ids)} "
        f"opt_conn={len(opt_conn_digests)} ({sum(len(v) for v in opt_conn_digests.values())} pairs) "
        f"in {elapsed:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return {
        "fast_unprobed_ids": fast_unprobed_ids,
        "opt_conn_digests": opt_conn_digests,
    }


def _main_fast_probe_only() -> None:
    repo = pathlib.Path(sys.argv[2]).resolve()
    maps_path = pathlib.Path(sys.argv[3]).resolve()
    os.chdir(repo)
    payload = pickle.loads(zlib.decompress(maps_path.read_bytes()))
    payload.update(run_fast_probe_maps(repo, payload["test_ids"], payload.get("subprocess_test_ids", ())))
    payload["version"] = max(int(payload.get("version", 9)), 10)
    raw_bytes = pickle.dumps(payload, protocol=5)
    comp_bytes = zlib.compress(raw_bytes, level=9)
    assert len(comp_bytes) < 2 * 1024 * 1024, f"Map too large: {len(comp_bytes)} bytes"
    maps_path.write_bytes(comp_bytes)
    print(
        f"[DONE] Augmented fast_probe maps in {maps_path} ({len(comp_bytes) / 1024:.1f} KiB compressed, {len(raw_bytes) / 1024:.1f} KiB raw)",
        file=sys.stderr,
        flush=True,
    )


def _augment_main() -> None:
    """Add/refresh static + probe-derived keys on an existing maps file without re-running pass 1/2."""
    repo = pathlib.Path(sys.argv[2]).resolve()
    maps_path = pathlib.Path(sys.argv[3]).resolve()
    cache_dir = pathlib.Path(sys.argv[4] if len(sys.argv) > 4 else "/tmp/rwx_cache").resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = pickle.loads(zlib.decompress(maps_path.read_bytes()))
    (
        payload["imports"],
        payload["rev_imports"],
        payload["dialect_typing_links"],
        payload["attr_read_lines"],
        payload["attr_import_test_files"],
        payload["metaclass_attrs"],
    ) = build_static_graphs(repo)
    payload["lazy_test_importers"] = build_lazy_test_importers(repo, payload["rev_imports"])
    once_lines, collection_lines, import_trigger_lines = run_once_lines_probe(repo, cache_dir)
    payload["once_lines"] = once_lines
    payload["collection_lines"] = collection_lines
    payload["import_trigger_lines"] = import_trigger_lines
    once_union: Dict[str, Set[int]] = {}
    for rel, lns in once_lines.items():
        once_union.setdefault(rel, set()).update(lns)
    for rel, lmap in import_trigger_lines.items():
        once_union.setdefault(rel, set()).update(lmap.keys())
    str_const_idents, known_words = build_vocab_sets(repo)
    payload["dict_value_import_reads"] = frozenset(build_dict_value_import_reads(repo, once_union))
    payload["str_const_idents"] = frozenset(str_const_idents)
    payload["known_words"] = frozenset(known_words)
    payload["dict_key_mismatch_attrs"] = frozenset(
        build_dict_key_mismatch_attrs(repo, payload.get("dict_key_usage", {}), payload.get("tracked_dict_classes", ()))
    )
    print(f"[Pass 5] dict_key_mismatch_attrs: {sorted(payload['dict_key_mismatch_attrs'])}", file=sys.stderr, flush=True)
    payload["path_mentions"] = build_path_mentions(repo)
    print(f"[Pass 5] path_mentions: {payload['path_mentions']}", file=sys.stderr, flush=True)
    eq_res = run_eqsite_probe(repo, cache_dir)
    payload["eq_safe_sites"] = _site_dict_to_frozensets(eq_res.get("safe_sites", {}))
    payload["eq_sites_stats"] = eq_res.get("stats", {})
    payload["subprocess_test_ids"] = build_subprocess_tests(repo, payload.get("test_ids"))
    print(f"[Pass 6] subprocess_test_ids: {payload['subprocess_test_ids']}", file=sys.stderr, flush=True)
    os.chdir(repo)
    payload.update(run_tpcds_probe_maps(repo, payload["test_ids"], payload["line_cov"]))
    payload.update(run_fast_probe_maps(repo, payload["test_ids"], payload.get("subprocess_test_ids", ())))
    payload["version"] = max(int(payload.get("version", 9)), 10)
    raw_bytes = pickle.dumps(payload, protocol=5)
    comp_bytes = zlib.compress(raw_bytes, level=9)
    maps_path.write_bytes(comp_bytes)
    print(f"[DONE] Augmented {maps_path} ({len(comp_bytes) / 1024:.1f} KiB)", file=sys.stderr, flush=True)


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
    py_bin = str(pathlib.Path(sys.executable).parent)
    old_path = os.environ.get("PATH", "")
    if py_bin not in old_path.split(os.pathsep):
        os.environ["PATH"] = f"{py_bin}{os.pathsep}{old_path}" if old_path else py_bin
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

    once_lines, collection_lines, import_trigger_lines = run_once_lines_probe(repo, cache_dir)
    once_union: Dict[str, Set[int]] = {}
    for rel, lns in once_lines.items():
        once_union.setdefault(rel, set()).update(lns)
    for rel, lmap in import_trigger_lines.items():
        once_union.setdefault(rel, set()).update(lmap.keys())
    str_const_idents, known_words = build_vocab_sets(repo)
    dict_value_import_reads = frozenset(build_dict_value_import_reads(repo, once_union))
    dict_key_mismatch_attrs = frozenset(
        build_dict_key_mismatch_attrs(repo, pass2["dict_key_usage"], pass2["tracked_dict_classes"])
    )
    print(f"[Pass 5] dict_key_mismatch_attrs: {sorted(dict_key_mismatch_attrs)}", file=sys.stderr, flush=True)
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
        "once_lines": once_lines,
        "collection_lines": collection_lines,
        "import_trigger_lines": import_trigger_lines,
        "dict_value_import_reads": dict_value_import_reads,
        "str_const_idents": frozenset(str_const_idents),
        "known_words": frozenset(known_words),
        "dict_key_mismatch_attrs": dict_key_mismatch_attrs,
        "path_mentions": build_path_mentions(repo),
        "lazy_test_importers": build_lazy_test_importers(repo, rev_imports),
        "subprocess_test_ids": build_subprocess_tests(repo, test_ids),
    }
    eq_res = run_eqsite_probe(repo, cache_dir)
    payload["eq_safe_sites"] = _site_dict_to_frozensets(eq_res.get("safe_sites", {}))
    payload["eq_sites_stats"] = eq_res.get("stats", {})
    payload.update(run_tpcds_probe_maps(repo, test_ids, line_cov))
    payload.update(run_fast_probe_maps(repo, test_ids, payload.get("subprocess_test_ids", ())))
    payload["version"] = 10
    raw_bytes = pickle.dumps(payload, protocol=5)
    comp_bytes = zlib.compress(raw_bytes, level=9)
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
    py_bin = str(pathlib.Path(sys.executable).parent)
    old_path = os.environ.get("PATH", "")
    if py_bin not in old_path.split(os.pathsep):
        os.environ["PATH"] = f"{py_bin}{os.pathsep}{old_path}" if old_path else py_bin
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
    elif len(sys.argv) >= 2 and sys.argv[1] == "--eqsites-only":
        _main_eqsites()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--augment":
        _augment_main()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--tpcds-probe-only":
        _main_tpcds_probe_only()
    elif len(sys.argv) >= 2 and sys.argv[1] == "--fast-probe-only":
        _main_fast_probe_only()
    else:
        main()
