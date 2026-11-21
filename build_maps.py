#!/usr/bin/env python3
"""Build precomputed static + dynamic baseline maps for sqlglot at pinned commit eb11aec.

Outputs RWX_track/maps/blast_maps.json.gz containing ONLY baseline commit facts:
  - test_ids: list[str] (canonical pytest nodeids sorted deterministically)
  - line_cov: {rel_path: {lineno_str: [test_idx, ...]}}
  - class_usage: {f"{rel_path}::{qualname}": [test_idx, ...]}
  - file_access: {rel_path: [test_idx, ...]}
  - pkg_walkers: {pkg_prefix: [test_idx, ...]}
  - imports: {rel_path: [imported_rel_path, ...]}
  - rev_imports: {rel_path: [importing_rel_path, ...]} (transitive)
  - dialect_typing_links: {typing_rel_path: [class_key, ...]}
"""
from __future__ import annotations

import ast
import concurrent.futures
import gzip
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
                yield fn(*args)

        def shutdown(self, wait=True, *, cancel_futures=False):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.shutdown()
            return False

    concurrent.futures.ProcessPoolExecutor = SyncExecutor  # type: ignore[misc]


def run_pass1_coverage(repo: pathlib.Path, out_cov_file: pathlib.Path) -> Tuple[List[str], Dict[str, Dict[str, List[int]]]]:
    """Run pytest with coverage.py dynamic contexts and setUpClass attribution."""
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

    class CovPlugin:
        def pytest_collection_modifyitems(self, items):
            seen_cls: Set[type] = set()
            for item in items:
                collected_nodeids.append(item.nodeid)
                cls = getattr(item, "cls", None)
                if cls is not None:
                    cls_to_nodeids[cls].append(item.nodeid)
                    if cls not in seen_cls:
                        seen_cls.add(cls)
                        orig = cls.__dict__.get("setUpClass")
                        if orig is not None:
                            raw_fn = orig.__func__ if isinstance(orig, classmethod) else orig
                            cls_tag = f"__CLS__::{cls.__module__}.{cls.__qualname__}"

                            def _make_wrapped(fn, tag):
                                @classmethod
                                def _wrapped(c):
                                    prev = cur_ctx_stack[-1]
                                    cur_ctx_stack.append(tag)
                                    cov.switch_context(tag)
                                    try:
                                        return fn(c)
                                    finally:
                                        cur_ctx_stack.pop()
                                        cov.switch_context(prev)
                                return _wrapped

                            cls.setUpClass = _make_wrapped(raw_fn, cls_tag)  # type: ignore[method-assign]

        def pytest_runtest_protocol(self, item, nextitem):
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

    # Canonical index map over collected tests.
    test_ids = sorted(set(collected_nodeids))
    id_to_idx = {nid: i for i, nid in enumerate(test_ids)}

    cls_tag_to_idxs: Dict[str, List[int]] = {}
    for cls, nids in cls_to_nodeids.items():
        tag = f"__CLS__::{cls.__module__}.{cls.__qualname__}"
        cls_tag_to_idxs[tag] = [id_to_idx[n] for n in nids if n in id_to_idx]

    data = cov.get_data()
    line_cov: Dict[str, Dict[str, List[int]]] = {}
    for fname in sorted(data.measured_files()):
        rel = _rel_path(fname, repo)
        if not rel or not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            continue
        by_ln = data.contexts_by_lineno(fname)
        out_lines: Dict[str, List[int]] = {}
        for ln, ctxs in by_ln.items():
            idxs: Set[int] = set()
            for c in ctxs:
                if not c:
                    continue
                if c in id_to_idx:
                    idxs.add(id_to_idx[c])
                elif c in cls_tag_to_idxs:
                    idxs.update(cls_tag_to_idxs[c])
            if idxs:
                out_lines[str(ln)] = sorted(idxs)
        if out_lines:
            line_cov[rel] = out_lines

    print(f"[Pass 1] Extracted line_cov for {len(line_cov)} files", file=sys.stderr, flush=True)
    return test_ids, line_cov


def run_pass2_dynamic_hooks(
    repo: pathlib.Path,
    test_ids: List[str],
    pytest_targets: Optional[List[str]] = None,
) -> Tuple[Dict[str, List[int]], Dict[str, List[int]], Dict[str, List[int]]]:
    """Run pytest with lightweight file_access, class_usage, and pkgutil hooks."""
    import pytest
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect, _Dialect
    from sqlglot.generator import Generator
    from sqlglot.optimizer.annotate_types import TypeAnnotator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    t0 = time.perf_counter()
    print("[Pass 2] Running dynamic file_access & class_usage instrumentation...", file=sys.stderr, flush=True)

    id_to_idx = {nid: i for i, nid in enumerate(test_ids)}
    cls_to_idxs: Dict[type, List[int]] = defaultdict(list)

    # Current context: "" means outside test; otherwise nodeid or __CLS__::... tag.
    state: Dict[str, Any] = {
        "ctx": "",
        "seen_files": set(),
        "seen_clss": set(),
        "seen_pkgs": set(),
    }

    raw_file_access: Dict[str, Set[str]] = defaultdict(set)
    raw_class_usage: Dict[str, Set[str]] = defaultdict(set)
    raw_pkg_walkers: Dict[str, Set[str]] = defaultdict(set)
    cls_key_cache: Dict[type, List[str]] = {}

    def _switch(new_ctx: str) -> None:
        state["ctx"] = new_ctx
        state["seen_files"].clear()
        state["seen_clss"].clear()
        state["seen_pkgs"].clear()

    def _record_file(path_arg: Any) -> None:
        ctx = state["ctx"]
        if not ctx:
            return
        rel = _rel_path(path_arg, repo)
        if not rel or rel in state["seen_files"]:
            return
        state["seen_files"].add(rel)
        raw_file_access[rel].add(ctx)

    def _mro_keys(cls: type) -> List[str]:
        cached = cls_key_cache.get(cls)
        if cached is not None:
            return cached
        keys: List[str] = []
        for base in cls.__mro__:
            k = _class_key(base, repo)
            if k:
                keys.append(k)
        cls_key_cache[cls] = keys
        return keys

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

    # Hook file opens (read mode).
    orig_open = io.open
    orig_builtin_open = open
    orig_path_open = pathlib.Path.open
    orig_path_read_text = pathlib.Path.read_text
    orig_path_read_bytes = pathlib.Path.read_bytes

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

    # Hook Dialect / Tokenizer / Parser / Generator / TypeAnnotator / Expr instantiation & lookup.
    orig_gor_desc = Dialect.__dict__["get_or_raise"]
    orig_get_or_raise = orig_gor_desc.__func__
    orig_dget_desc = _Dialect.__dict__["get"]
    orig_dialect_get = orig_dget_desc.__func__
    orig_dgetitem_desc = _Dialect.__dict__["__getitem__"]
    orig_dialect_getitem = orig_dgetitem_desc.__func__
    orig_tok_init = Tokenizer.__init__
    orig_parser_init = Parser.__init__
    orig_gen_init = Generator.__init__
    orig_annot_init = TypeAnnotator.__init__
    orig_expr_init = exp.Expr.__init__
    orig_iter_modules = pkgutil.iter_modules
    orig_walk_packages = pkgutil.walk_packages

    @classmethod  # type: ignore[misc]
    def hooked_get_or_raise(cls, dialect):
        res = orig_get_or_raise(cls, dialect)
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

    def hooked_tok_init(self, *args, **kwargs):
        orig_tok_init(self, *args, **kwargs)
        if state["ctx"]:
            _record_cls(type(self))

    def hooked_parser_init(self, *args, **kwargs):
        orig_parser_init(self, *args, **kwargs)
        if state["ctx"]:
            _record_cls(type(self))

    def hooked_gen_init(self, *args, **kwargs):
        orig_gen_init(self, *args, **kwargs)
        if state["ctx"]:
            _record_cls(type(self))

    def hooked_annot_init(self, *args, **kwargs):
        orig_annot_init(self, *args, **kwargs)
        if state["ctx"]:
            _record_cls(type(self))

    def hooked_expr_init(self, *args, **kwargs):
        orig_expr_init(self, *args, **kwargs)
        ctx = state["ctx"]
        if ctx:
            cls = type(self)
            if cls not in state["seen_clss"]:
                _record_cls(cls)

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

    cls_tag_to_idxs: Dict[str, List[int]] = {}

    class HookPlugin:
        def pytest_collection_modifyitems(self, items):
            seen_cls: Set[type] = set()
            for item in items:
                cls = getattr(item, "cls", None)
                if cls is not None:
                    if item.nodeid in id_to_idx:
                        cls_to_idxs[cls].append(id_to_idx[item.nodeid])
                    if cls not in seen_cls:
                        seen_cls.add(cls)
                        orig = cls.__dict__.get("setUpClass")
                        if orig is not None:
                            raw_fn = orig.__func__ if isinstance(orig, classmethod) else orig
                            cls_tag = f"__CLS__::{cls.__module__}.{cls.__qualname__}"

                            def _make_wrapped(fn, tag):
                                @classmethod
                                def _wrapped(c):
                                    prev = state["ctx"]
                                    _switch(tag)
                                    try:
                                        return fn(c)
                                    finally:
                                        _switch(prev)
                                return _wrapped

                            cls.setUpClass = _make_wrapped(raw_fn, cls_tag)  # type: ignore[method-assign]

        def pytest_runtest_protocol(self, item, nextitem):
            _switch(item.nodeid)
            return None

        def pytest_runtest_logfinish(self, nodeid, location):
            _switch("")

    import builtins
    builtins.open = hooked_open  # type: ignore[assignment]
    io.open = hooked_open  # type: ignore[assignment]
    pathlib.Path.open = hooked_path_open  # type: ignore[method-assign]
    pathlib.Path.read_text = hooked_read_text  # type: ignore[method-assign]
    pathlib.Path.read_bytes = hooked_read_bytes  # type: ignore[method-assign]
    Dialect.get_or_raise = hooked_get_or_raise  # type: ignore[method-assign]
    _Dialect.get = hooked_dialect_get  # type: ignore[method-assign]
    _Dialect.__getitem__ = hooked_dialect_getitem  # type: ignore[method-assign]
    Tokenizer.__init__ = hooked_tok_init  # type: ignore[method-assign]
    Parser.__init__ = hooked_parser_init  # type: ignore[method-assign]
    Generator.__init__ = hooked_gen_init  # type: ignore[method-assign]
    TypeAnnotator.__init__ = hooked_annot_init  # type: ignore[method-assign]
    exp.Expr.__init__ = hooked_expr_init  # type: ignore[method-assign]
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
        Dialect.get_or_raise = orig_gor_desc  # type: ignore[method-assign]
        _Dialect.get = orig_dget_desc  # type: ignore[method-assign]
        _Dialect.__getitem__ = orig_dgetitem_desc  # type: ignore[method-assign]
        Tokenizer.__init__ = orig_tok_init  # type: ignore[method-assign]
        Parser.__init__ = orig_parser_init  # type: ignore[method-assign]
        Generator.__init__ = orig_gen_init  # type: ignore[method-assign]
        TypeAnnotator.__init__ = orig_annot_init  # type: ignore[method-assign]
        exp.Expr.__init__ = orig_expr_init  # type: ignore[method-assign]
        pkgutil.iter_modules = orig_iter_modules
        pkgutil.walk_packages = orig_walk_packages

    for cls, idxs in cls_to_idxs.items():
        cls_tag_to_idxs[f"__CLS__::{cls.__module__}.{cls.__qualname__}"] = idxs

    def _expand(raw_map: Dict[str, Set[str]]) -> Dict[str, List[int]]:
        out: Dict[str, List[int]] = {}
        for key, ctxs in sorted(raw_map.items()):
            idxs: Set[int] = set()
            for c in ctxs:
                if c in id_to_idx:
                    idxs.add(id_to_idx[c])
                elif c in cls_tag_to_idxs:
                    idxs.update(cls_tag_to_idxs[c])
            if idxs:
                out[key] = sorted(idxs)
        return out

    dt = time.perf_counter() - t0
    file_access = _expand(raw_file_access)
    class_usage = _expand(raw_class_usage)
    pkg_walkers = _expand(raw_pkg_walkers)
    print(
        f"[Pass 2] Done rc={rc} in {dt:.1f}s | file_access={len(file_access)} "
        f"class_usage={len(class_usage)} pkg_walkers={len(pkg_walkers)}",
        file=sys.stderr,
        flush=True,
    )
    return file_access, class_usage, pkg_walkers


def build_static_graphs(repo: pathlib.Path) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, List[str]]]:
    """Parse Python files in sqlglot/ and tests/ to build direct & transitive reverse import graphs."""
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
    # Link typing modules <-> importing dialect classes statically.
    typing_links: Dict[str, Set[str]] = defaultdict(set)

    for rel, src in py_files.items():
        cur_mod_parts = rel.removesuffix(".py").split("/")
        if cur_mod_parts[-1] == "__init__":
            pkg_parts = cur_mod_parts[:-1]
        else:
            pkg_parts = cur_mod_parts[:-1]

        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        # Identify classes defined in this module that reference imported typing metadata.
        cls_names = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]

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

    direct_out = {k: sorted(v) for k, v in sorted(direct.items()) if v}
    typing_out = {k: sorted(v) for k, v in sorted(typing_links.items()) if v}
    dt = time.perf_counter() - t0
    print(f"[Pass 3] Static import graph built for {len(rel_set)} modules in {dt:.2f}s", file=sys.stderr, flush=True)
    return direct_out, rev_trans, typing_out


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

    p1_cache = cache_dir / "pass1_cache.json"
    if args.use_cache and p1_cache.is_file():
        print(f"[Pass 1] Reusing cached {p1_cache}", file=sys.stderr, flush=True)
        cached1 = json.loads(p1_cache.read_text("utf-8"))
        test_ids = cached1["test_ids"]
        line_cov = cached1["line_cov"]
    else:
        cov_file = cache_dir / "rwx_cov_pass1.sqlite"
        test_ids, line_cov = run_pass1_coverage(repo, cov_file)
        if cov_file.exists():
            cov_file.unlink()
        p1_cache.write_text(
            json.dumps({"test_ids": test_ids, "line_cov": line_cov}, separators=(",", ":")),
            encoding="utf-8",
        )

    p2_cache = cache_dir / "pass2_cache.json"
    if args.use_cache and p2_cache.is_file():
        print(f"[Pass 2] Reusing cached {p2_cache}", file=sys.stderr, flush=True)
        pass2 = json.loads(p2_cache.read_text("utf-8"))
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
        pass2 = json.loads(p2_cache.read_text("utf-8"))

    direct_imports, rev_imports, dialect_typing_links = build_static_graphs(repo)

    payload = {
        "version": 1,
        "commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "test_ids": test_ids,
        "line_cov": line_cov,
        "file_access": pass2["file_access"],
        "class_usage": pass2["class_usage"],
        "pkg_walkers": pass2["pkg_walkers"],
        "imports": direct_imports,
        "rev_imports": rev_imports,
        "dialect_typing_links": dialect_typing_links,
    }
    raw_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with gzip.open(out_path, "wb", compresslevel=6) as f:
        f.write(raw_bytes)
    gz_size = out_path.stat().st_size
    print(
        f"[DONE] Saved {out_path} ({gz_size / 1024:.1f} KiB compressed, {len(raw_bytes) / 1024:.1f} KiB raw)",
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
    file_access, class_usage, pkg_walkers = run_pass2_dynamic_hooks(repo, test_ids, pytest_targets=pytest_targets)
    pass2_out.write_text(
        json.dumps(
            {
                "file_access": file_access,
                "class_usage": class_usage,
                "pkg_walkers": pkg_walkers,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--pass2-only":
        _main_pass2()
    else:
        main()
