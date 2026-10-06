"""Ratchet: a change may not add a shape that makes a test flaky to any file.

The Determinism contract in ``docs/system-specs/common/testing-conventions.md`` names
the shapes a test must not use: a sleep as a barrier, a rebound stdlib clock, a naive
local-time read, an unseeded random draw, a literal listener port, a bare
``monkeypatch.undo()`` and the rest. The tree already holds thousands of them, so a
rule that refused every one could not land. This module counts them per file and
class instead, and holds each file a change touches to its own count at the change's
merge-base.

* A file's count may not rise; a new file starts at zero, a deleted file passes, and
  a rename git detects keeps its count. A rise is forgiven only for a site that
  moved: the same class in an identical enclosing function (the same line in
  TypeScript) that left another file in the same change. The working tree is judged,
  untracked files included.
* The base and the changed-file set are read through ``scripts/ratchet_scope.py``'s
  shared helpers (``change_merge_base``, ``change_files``, ``show_at``), the same
  module ten other diff-scoped gates read. Precedence: ``FLAKE_RATCHET_BASE`` when
  set; on CI the first parent of the merge commit a pull request, push or merge
  queue run checks out; else the newer of the merge-bases with ``upstream/<base>``
  and ``origin/<base>``, or the local ``<base>`` branch. When none is readable (a
  shallow clone, as CI's backend shards are today), the ratchet warns and compares
  nothing: judging against a stored count would red a change for sites that other,
  already merged changes added.
* A line that keeps a shape on purpose carries ``# flake-ok: <reason>`` (``//
  flake-ok: <reason>`` in TypeScript) with a reason of ten characters or more, words
  not punctuation. For a function-shaped finding (an autouse fixture) the marker goes
  on its ``def`` line or a decorator line.
* ``python test/test_flake_pattern_ratchet.py`` prints each file's current counts
  as the burn-down view; no committed file holds them.

The Python detectors read the AST, so a shape named in a docstring or a comment is not
counted; the TypeScript and JavaScript ones are line patterns. Each detector has a
positive and a negative case below, and a mutation proof that a detector finding
nothing fails its positive case. Only the files a change touches are read, so the
gate costs a git diff plus those files.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import warnings
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from source_corpus import repo_files, repo_root

_RATCHET_SCOPE_SCRIPT = repo_root() / "scripts" / "ratchet_scope.py"
_SPEC = importlib.util.spec_from_file_location("ratchet_scope", _RATCHET_SCOPE_SCRIPT)
assert _SPEC and _SPEC.loader
ratchet_scope = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ratchet_scope)

pytestmark = pytest.mark.xdist_group(name="tree_scan_flake_ratchet")

#: A ``flake-ok`` reason shorter than this is not a reason.
MIN_REASON_CHARS = 10
_FLAKE_OK = re.compile(r"(?:#|//)\s*flake-ok:\s*(?P<reason>\S.*?)\s*(?:\*/)?$")

# ── which files are scanned ───────────────────────────────────────────


def _is_python_test_file(rel: str) -> bool:
    if not rel.endswith(".py"):
        return False
    name = rel.rsplit("/", 1)[-1]
    if name == "conftest.py":
        return True
    if rel.startswith("test/"):
        return True
    if rel.startswith("src/kiro_crew/apps/builtins/"):
        parts = rel.split("/")
        return any(part == "tests" or part.endswith("_tests") for part in parts[:-1])
    return False


_JS_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".cjs")


def _is_js_test_file(rel: str) -> bool:
    if not rel.startswith("website/") or not rel.endswith(_JS_SUFFIXES):
        return False
    if "/node_modules/" in rel:
        return False
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith(("website/integration/", "website/playwright/")):
        return True
    if rel.startswith("website/src/"):
        return ".test." in name or "/test/" in rel
    if rel.startswith("website/electron/"):
        return "/test/" in rel
    return False


# ── the flake-ok escape ───────────────────────────────────────────────


def _flake_ok(line: str) -> bool:
    match = _FLAKE_OK.search(line)
    if not match:
        return False
    reason = match.group("reason")
    return len(reason) >= MIN_REASON_CHARS and any(c.isalpha() for c in reason)


# ── Python detectors (AST) ────────────────────────────────────────────

#: Stdlib names whose rebinding changes the clock or the sleep of the whole worker.
_CLOCK_ATTRS = {
    "time": frozenset(
        {
            "time",
            "time_ns",
            "monotonic",
            "monotonic_ns",
            "perf_counter",
            "perf_counter_ns",
            "sleep",
            "localtime",
            "gmtime",
        }
    ),
    "asyncio": frozenset({"sleep"}),
    "datetime": frozenset({"datetime", "date"}),
}
_CLOCK_CALLS = frozenset({"monotonic", "perf_counter", "time", "monotonic_ns", "perf_counter_ns"})
_DELTA_NAMES = re.compile(r"(elapsed|duration|took|delta|wall)", re.IGNORECASE)
_STAMP_NAMES = re.compile(
    r"^(start|started|end|ended|before|after|began|finished|now|then|t\d|t_\w+|\w+_at)$"
)
_RANDOM_FUNCS = frozenset(
    {
        "random",
        "randint",
        "randrange",
        "choice",
        "choices",
        "shuffle",
        "sample",
        "uniform",
        "getrandbits",
        "randbytes",
        "gauss",
        "seed",
    }
)
_TIMEOUT_ERRORS = frozenset({"TimeoutError", "asyncio.TimeoutError", "futures.TimeoutError"})
#: Calls that open a listener: a literal port handed to one is the D9 shape.
_LISTENERS = frozenset(
    {
        "TCPSite",
        "run_app",
        "start_server",
        "create_server",
        "HTTPServer",
        "ThreadingHTTPServer",
        "TCPServer",
        "ThreadingTCPServer",
        "UDPServer",
        "serve",
        "make_server",
    }
)
#: Where a listener call takes its port positionally (0-based).
_LISTENER_PORT_ARG = {"TCPSite": 2, "start_server": 2, "create_server": 2}


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a Name/Attribute chain, else ''."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _number(node: ast.AST | None) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    return None


def _statement_lists(node: ast.AST) -> Iterable[list[ast.stmt]]:
    """Every list of statements under *node*: the body and every nested block."""
    for child in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(child, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block


class _Module:
    """One parsed file, with what the detectors share."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        #: local name -> stdlib module, for ``import time as _time`` and the like.
        self.modules: dict[str, str] = {
            name: name for name in ("time", "asyncio", "datetime", "os")
        }
        #: local names bound to ``time.sleep`` / ``asyncio.sleep``.
        self.sleeps: dict[str, str] = {}
        #: local names bound to ``importlib.reload``.
        self.reloads: set[str] = set()
        self.parents: dict[ast.AST, ast.AST] = {}
        self.calls: list[ast.Call] = []
        self.asserts: list[ast.Assert] = []
        self.writes: list[ast.AST] = []  # Assign, AugAssign, Delete
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        # One walk; every detector reads these lists instead of walking again.
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
                stack.append(child)
            if isinstance(node, ast.Call):
                self.calls.append(node)
            elif isinstance(node, ast.Assert):
                self.asserts.append(node)
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
                self.writes.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.append(node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _CLOCK_ATTRS or alias.name == "os":
                        self.modules[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module in ("time", "asyncio"):
                for alias in node.names:
                    if alias.name == "sleep":
                        self.sleeps[alias.asname or "sleep"] = node.module
            elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "reload":
                        self.reloads.add(alias.asname or "reload")

    def stdlib_of(self, node: ast.AST) -> str | None:
        """The stdlib module a Name or Attribute names, through this file's aliases."""
        if isinstance(node, ast.Name):
            return self.modules.get(node.id)
        dotted = _dotted(node)
        if dotted.endswith((".time", ".asyncio", ".datetime")):
            # ``<subject>.time``: the module under test's own binding of the stdlib
            # module, which a test patches to change the clock for the whole worker.
            return dotted.rsplit(".", 1)[1]
        return None

    def sleep_call(self, call: ast.Call) -> str | None:
        """``"time"`` or ``"asyncio"`` when *call* is that module's sleep."""
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "sleep":
            module = self.stdlib_of(func.value)
            return module if module in ("time", "asyncio") else None
        if isinstance(func, ast.Name):
            return self.sleeps.get(func.id)
        return None

    def enclosing_function(self, node: ast.AST):
        current = self.parents.get(node)
        while current is not None and not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            current = self.parents.get(current)
        return current

    def inside_raises_timeout(self, node: ast.AST) -> bool:
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.With, ast.AsyncWith)):
                for item in current.items:
                    call = item.context_expr
                    if (
                        isinstance(call, ast.Call)
                        and _dotted(call.func).endswith("raises")
                        and call.args
                        and _dotted(call.args[0]) in _TIMEOUT_ERRORS
                    ):
                        return True
            current = self.parents.get(current)
        return False

    def inside_paused(self, node: ast.AST) -> bool:
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.With, ast.AsyncWith)):
                for item in current.items:
                    call = item.context_expr
                    if isinstance(call, ast.Call) and _dotted(call.func).endswith("paused"):
                        return True
            current = self.parents.get(current)
        return False


def _test_functions(module: _Module):
    return [fn for fn in module.functions if fn.name.startswith("test")]


def _contains_assert(statements: Iterable[ast.stmt]) -> bool:
    return any(isinstance(node, ast.Assert) for stmt in statements for node in ast.walk(stmt))


def _sleep_seconds(module: _Module, stmt: ast.stmt, kind: str) -> float | None:
    value = stmt.value if isinstance(stmt, ast.Expr) else None
    if kind == "asyncio":
        value = value.value if isinstance(value, ast.Await) else None
    if not isinstance(value, ast.Call) or module.sleep_call(value) != kind:
        return None
    return _number(value.args[0]) if value.args else None


_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _runs_after(module: _Module, fn: ast.AST, block: list[ast.stmt], index: int):
    """The statements of *fn* that run after ``block[index]``, in order.

    The rest of its own block, then whatever follows each enclosing ``with``, ``if`` or
    ``try``, out to the end of *fn*. It does not climb out of a loop: a sleep in a loop
    body is a poll interval, and what the loop is waiting for is checked by the loop.
    """
    yield from block[index + 1 :]
    owner = module.parents.get(block[0])
    while owner is not None and owner is not fn and not isinstance(owner, _LOOPS):
        if not isinstance(owner, ast.stmt):  # an except handler or a match case
            owner = module.parents.get(owner)
            continue
        parent = module.parents.get(owner)
        if parent is None:
            return
        for field in ("body", "orelse", "finalbody"):
            siblings = getattr(parent, field, None)
            if isinstance(siblings, list) and owner in siblings:
                yield from siblings[siblings.index(owner) + 1 :]
                break
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        owner = parent


def _k1_sleep_before_assert(module: _Module) -> Iterable[ast.AST]:
    for fn in _test_functions(module):
        for block in _statement_lists(fn):
            if module.enclosing_function(block[0]) is not fn:
                continue  # a helper defined inside the test runs on its own schedule
            for index, stmt in enumerate(block):
                seconds = _sleep_seconds(module, stmt, "time")
                if (
                    seconds
                    and seconds > 0
                    and _contains_assert(_runs_after(module, fn, block, index))
                ):
                    yield stmt


def _k2_async_sleep_before_assert(module: _Module) -> Iterable[ast.AST]:
    for fn in _test_functions(module):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for block in _statement_lists(fn):
            for index, stmt in enumerate(block):
                seconds = _sleep_seconds(module, stmt, "asyncio")
                if seconds and seconds > 0 and _contains_assert(block[index + 1 : index + 4]):
                    yield stmt


def _patched_target(node: ast.Call) -> tuple[ast.AST | None, str | None]:
    """``(object, attribute)`` a ``setattr`` / ``patch.object`` call patches."""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        return node.args[0], str(node.args[1].value)
    return (node.args[0] if node.args else None), None


def _k3_stdlib_clock_patch(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        name = _dotted(node.func)
        if name.endswith(("setattr", "patch.object")) and node.args:
            target, attr = _patched_target(node)
            if isinstance(target, ast.Constant) and isinstance(target.value, str):
                stdlib, _, rest = target.value.rpartition(".")
                stdlib = stdlib.rsplit(".", 1)[-1]
                if rest in _CLOCK_ATTRS.get(stdlib, ()):
                    yield node
                continue
            stdlib = module.stdlib_of(target) if target is not None else None
            if stdlib and attr in _CLOCK_ATTRS.get(stdlib, ()):
                yield node
        elif (
            (name == "patch" or name.endswith(".patch"))
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            text = str(node.args[0].value)
            stdlib, _, rest = text.rpartition(".")
            if rest in _CLOCK_ATTRS.get(stdlib.rsplit(".", 1)[-1], ()):
                yield node


def _k4_bare_undo(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "undo"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "monkeypatch"
            and not node.args
        ):
            yield node


def _restores_sys_modules(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _dotted(target.value) == "sys.modules":
                    return True
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name.endswith(("setitem", "patch.dict", "delitem")) and node.args:
                if _dotted(node.args[0]) == "sys.modules":
                    return True
    return False


def _k5_reload_or_eviction(module: _Module) -> Iterable[ast.AST]:
    for node in [*module.calls, *module.writes]:
        if isinstance(node, ast.Call) and (
            _dotted(node.func) == "importlib.reload"
            or (isinstance(node.func, ast.Name) and node.func.id in module.reloads)
        ):
            yield node
        evicted = False
        if isinstance(node, ast.Delete):
            evicted = any(
                isinstance(t, ast.Subscript) and _dotted(t.value) == "sys.modules"
                for t in node.targets
            )
        elif isinstance(node, ast.Call) and _dotted(node.func) == "sys.modules.pop":
            evicted = True
        if evicted:
            fn = module.enclosing_function(node)
            if fn is None or not _restores_sys_modules(fn):
                yield node


def _k6_raw_environ_write(module: _Module) -> Iterable[ast.AST]:
    for node in module.writes:
        targets: list[ast.AST] = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript) and _dotted(target.value) == "os.environ":
                yield node
                break


def _k7_naive_now(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        if node.args or node.keywords:
            continue
        name = _dotted(node.func)
        if name.endswith(("datetime.now", "datetime.today", "date.today")) or name in (
            "time.localtime",
            "localtime",
        ):
            yield node


def _is_clock_delta(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        for side in (node.left, node.right):
            if isinstance(side, ast.Call) and _dotted(side.func).rsplit(".", 1)[-1] in _CLOCK_CALLS:
                return True
        names = [_dotted(side).rsplit(".", 1)[-1] for side in (node.left, node.right)]
        if all(names) and all(_STAMP_NAMES.match(name) for name in names):
            return True
    if isinstance(node, (ast.Name, ast.Attribute)):
        return bool(_DELTA_NAMES.search(_dotted(node)))
    return False


def _k8_duration_bound(module: _Module) -> Iterable[ast.AST]:
    for node in module.asserts:
        if not isinstance(node.test, ast.Compare):
            continue
        compare = node.test
        left = compare.left
        for op, right in zip(compare.ops, compare.comparators):
            if (
                isinstance(op, (ast.Lt, ast.LtE))
                and _is_clock_delta(left)
                and _number(right) is not None
            ):
                yield node
                break
            left = right


def _literal_timeout(call: ast.Call, position: int) -> float | None:
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return _number(keyword.value)
    if len(call.args) > position:
        return _number(call.args[position])
    return None


def _k9_short_literal_wait(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        name = _dotted(node.func)
        if name.endswith("wait_for"):
            seconds = _literal_timeout(node, 1)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in ("wait", "result", "join"):
            seconds = _literal_timeout(node, 0)
        else:
            continue
        if seconds is None or not 0 < seconds < 2 or module.inside_raises_timeout(node):
            continue
        outer = module.parents.get(node)
        if isinstance(outer, ast.UnaryOp) and isinstance(outer.op, ast.Not):
            continue  # ``assert not event.wait(0.1)``: a short bound weakens a negative, never reds it
        yield node


def _k10_unseeded_random(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        name = _dotted(node.func)
        if name == "os.urandom" or (
            name.startswith("random.") and name.split(".", 1)[1] in _RANDOM_FUNCS
        ):
            yield node


def _k11_literal_listener_port(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        callee = _dotted(node.func).rsplit(".", 1)[-1]
        if callee == "bind" and node.args and isinstance(node.args[0], ast.Tuple):
            elts = node.args[0].elts
            if len(elts) >= 2 and _number(elts[1]):
                yield node
            continue
        if callee not in _LISTENERS:
            continue
        port = next((kw.value for kw in node.keywords if kw.arg == "port"), None)
        position = _LISTENER_PORT_ARG.get(callee)
        if port is None and position is not None and len(node.args) > position:
            port = node.args[position]
        if port is None and node.args and isinstance(node.args[0], ast.Tuple):
            elts = node.args[0].elts  # HTTPServer(("127.0.0.1", 8080), ...)
            port = elts[1] if len(elts) >= 2 else None
        if _number(port):
            yield node


def _k12_autouse_monkeypatch(module: _Module) -> Iterable[ast.AST]:
    for node in module.functions:
        if "monkeypatch" not in [arg.arg for arg in node.args.args]:
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and any(
                kw.arg == "autouse"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in decorator.keywords
            ):
                yield node
                break


def _k13_fixed_flush_ceiling(module: _Module) -> Iterable[ast.AST]:
    for node in module.calls:
        if _dotted(node.func).endswith("emit.flush") and _literal_timeout(node, 0) is not None:
            yield node


def _k14_unpaused_crew_log_rmtree(module: _Module, source: str) -> Iterable[ast.AST]:
    for node in module.calls:
        if _dotted(node.func) != "shutil.rmtree":
            continue
        text = ast.get_source_segment(source, node) or ""
        if ("crew_log" in text or "crew-log" in text) and not module.inside_paused(node):
            yield node


#: class -> words one of which every shape of that class contains. A file holding none
#: is not parsed: each set is deliberately broader than its detector, never narrower.
PY_TRIGGERS: dict[str, tuple[str, ...]] = {
    "K1-sleep-before-assert": ("sleep",),
    "K2-async-sleep-before-assert": ("sleep",),
    "K3-stdlib-clock-patch": ("setattr", "patch"),
    "K4-bare-monkeypatch-undo": ("undo",),
    "K5-reload-or-eviction": ("reload", "modules"),
    "K6-raw-environ-write": ("environ",),
    "K7-naive-local-time": ("()",),
    "K8-duration-upper-bound": ("assert",),
    "K9-short-literal-wait": ("(",),
    "K10-unseeded-random": ("random", "urandom"),
    "K11-literal-listener-port": ("(",),
    "K12-autouse-shared-monkeypatch": ("autouse",),
    "K13-fixed-flush-ceiling": ("flush",),
    "K14-unpaused-crew-log-rmtree": ("rmtree",),
}

#: class -> detector. K14 also reads the source text.
PY_DETECTORS: dict[str, Callable[..., Iterable[ast.AST]]] = {
    "K1-sleep-before-assert": _k1_sleep_before_assert,
    "K2-async-sleep-before-assert": _k2_async_sleep_before_assert,
    "K3-stdlib-clock-patch": _k3_stdlib_clock_patch,
    "K4-bare-monkeypatch-undo": _k4_bare_undo,
    "K5-reload-or-eviction": _k5_reload_or_eviction,
    "K6-raw-environ-write": _k6_raw_environ_write,
    "K7-naive-local-time": _k7_naive_now,
    "K8-duration-upper-bound": _k8_duration_bound,
    "K9-short-literal-wait": _k9_short_literal_wait,
    "K10-unseeded-random": _k10_unseeded_random,
    "K11-literal-listener-port": _k11_literal_listener_port,
    "K12-autouse-shared-monkeypatch": _k12_autouse_monkeypatch,
    "K13-fixed-flush-ceiling": _k13_fixed_flush_ceiling,
    "K14-unpaused-crew-log-rmtree": _k14_unpaused_crew_log_rmtree,
}


# ── TypeScript / JavaScript detectors (line patterns) ─────────────────

_J1 = re.compile(
    r"new Promise(?:<[^>]*>)?\(\s*\(?\s*(\w+)[^)=]*\)?\s*=>\s*\{?\s*setTimeout\(\s*\1\s*,"
    r"\s*([1-9][\d_]*)"
)
_J2 = re.compile(r"\.waitForTimeout\(")
_J3 = re.compile(r"\bDate\.now\(\)|\bnew Date\(\s*\)")
_J4 = re.compile(r"\bif\s*\(\s*!?\s*\(?\s*await\b[^;{]*?\.isVisible\(")
_J5 = re.compile(
    r"expect\([^;]*(?:performance\.now\(\)|Date\.now\(\)|\b\w*(?:elapsed|duration|took)\w*\b)[^;]*\)"
    r"\s*\.\s*toBeLessThan(?:OrEqual)?\(\s*\d",
    re.IGNORECASE,
)
_J6 = re.compile(r"\buseFakeTimers\(")
_STALLS = re.compile(r"\bawait\b|\bwaitFor\b|\bfindBy|\bfindAllBy")


def _j_lines(pattern: re.Pattern[str]) -> Callable[[str, str], list[int]]:
    def detect(rel: str, text: str) -> list[int]:
        return [n for n, line in enumerate(text.splitlines(), 1) if pattern.search(line)]

    return detect


def _j3_unpinned_date(rel: str, text: str) -> list[int]:
    if "setSystemTime" in text or "mock.timers" in text:
        return []
    return _j_lines(_J3)(rel, text)


def _j4_visible_branch(rel: str, text: str) -> list[int]:
    if not rel.startswith("website/playwright/"):
        return []
    return _j_lines(_J4)(rel, text)


def _restored_by_a_synchronous_finally(lines: list[str], index: int) -> bool:
    """Whether ``useFakeTimers`` on line *index* opens a ``try`` whose ``finally``
    restores real timers with nothing in between that a fake clock could stall."""
    rest = "\n".join(lines[index : index + 200])
    start = rest.find("try {")
    end = rest.find("finally", start)
    if start < 0 or end < 0:
        return False
    between = rest[:start].split("useFakeTimers", 1)[-1].replace("()", "")
    if between.strip(" \t\n;"):
        return False  # the try is not the next statement
    restore = rest.find("useRealTimers", end)
    return restore >= 0 and not _STALLS.search(rest[start:end])


def _j6_unrestored_fake_timers(rel: str, text: str) -> list[int]:
    if ("afterEach" in text and "useRealTimers" in text) or "onTestFinished" in text:
        return []
    lines = text.splitlines()
    return [
        n
        for n, line in enumerate(lines, 1)
        if _J6.search(line) and not _restored_by_a_synchronous_finally(lines, n - 1)
    ]


JS_DETECTORS: dict[str, Callable[[str, str], list[int]]] = {
    "J1-promise-sleep": _j_lines(_J1),
    "J2-wait-for-timeout": _j_lines(_J2),
    "J3-unpinned-date": _j3_unpinned_date,
    "J4-playwright-visible-branch": _j4_visible_branch,
    "J5-duration-upper-bound": _j_lines(_J5),
    "J6-unrestored-fake-timers": _j6_unrestored_fake_timers,
}


# ── counting ──────────────────────────────────────────────────────────


def _excuse_lines(node: ast.AST) -> range:
    """The lines a ``flake-ok`` marker may sit on to excuse *node*."""
    start = getattr(node, "lineno", 1)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        first = min([d.lineno for d in node.decorator_list] + [start])
        return range(first, start + 1)
    end = getattr(node, "end_lineno", start) or start
    return range(start, end + 1)


def _python_findings(source: str) -> Iterable[tuple[str, str | None]]:
    """``(class, site identity or None when excused)`` for each finding in a source.

    The identity is the class plus the unparsed text of the enclosing function (the
    finding itself at module level), so a site that MOVES with its function keeps it.
    """
    active = [klass for klass, words in PY_TRIGGERS.items() if any(w in source for w in words)]
    if not active:
        return
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    module = _Module(tree)
    lines = source.splitlines()
    for klass in active:
        detector = PY_DETECTORS[klass]
        found = detector(module, source) if klass.startswith("K14") else detector(module)
        for node in found:
            span = [n for n in _excuse_lines(node) if 0 < n <= len(lines)]
            if any(_flake_ok(lines[n - 1]) for n in span):
                yield klass, None
                continue
            owner = node
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = module.enclosing_function(node) or node
            yield klass, ast.unparse(owner)


def _js_findings(rel: str, source: str) -> Iterable[tuple[str, str | None]]:
    """``(class, the stripped line or None when excused)`` for each finding in a source."""
    lines = source.splitlines()
    for klass, detector in JS_DETECTORS.items():
        for number in detector(rel, source):
            line = lines[number - 1]
            yield klass, None if _flake_ok(line) else line.strip()


def _tally(findings: Iterable[tuple[str, str | None]]) -> tuple[Counter[str], int]:
    counts: Counter[str] = Counter()
    excused = 0
    for klass, site in findings:
        if site is None:
            excused += 1
        else:
            counts[klass] += 1
    return counts, excused


def count_python(source: str) -> tuple[Counter[str], int]:
    """``(class -> count, flake-ok lines)`` for one Python source."""
    return _tally(_python_findings(source))


def count_js(rel: str, source: str) -> tuple[Counter[str], int]:
    return _tally(_js_findings(rel, source))


def sites(rel: str, source: str) -> Counter[tuple[str, str]]:
    """``(class, site identity) -> count`` for one test source; excused lines left out."""
    found = _python_findings(source) if _is_python_test_file(rel) else _js_findings(rel, source)
    return Counter((klass, site) for klass, site in found if site is not None)


# ── the change in front of the ratchet ────────────────────────────────

#: Env var that forces an explicit base, matching the shared helper.
BASE_ENV = "FLAKE_RATCHET_BASE"


def merge_base(root: Path, *, env: dict[str, str] | None = None) -> tuple[str | None, str]:
    """``(base commit, how it was found)`` for this change against *root*; shared helper.

    Delegates to :func:`ratchet_scope.change_merge_base` so the base precedence
    stays the one every change-shaped gate reads.
    """
    return ratchet_scope.change_merge_base(BASE_ENV, cwd=root, env=env)


def changed_files(root: Path, base: str) -> list[tuple[str | None, str | None]]:
    """``(path at base, path now)`` for each file this change touches against *base*."""
    return ratchet_scope.change_files(base, cwd=root)


def _counted(rel: str) -> bool:
    return _is_python_test_file(rel) or _is_js_test_file(rel)


def _sites_at(root: Path, base: str | None, rel: str | None) -> Counter[tuple[str, str]]:
    if rel is None or not _counted(rel):
        return Counter()
    if base is not None:
        text = ratchet_scope.show_at(base, rel, cwd=root)
        return sites(rel, text) if text is not None else Counter()
    try:
        return sites(rel, (root / rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return Counter()


def change_risen(root: Path, base: str) -> dict[tuple[str, str], tuple[int, int]]:
    """``(class, path) -> (count now, count allowed)`` for every rise in this change.

    Each file is held to its own count per class at *base*; a new file starts at 0.
    A rise is forgiven only for a site that MOVED: the same (class, identity), where
    the identity is the enclosing function's unparsed Python text or the stripped
    JavaScript line, that left another file in the same change. One forgiveness per
    site that left.
    """
    files = []
    for old, new in sorted(changed_files(root, base), key=lambda pair: str(pair[1] or pair[0])):
        before = _sites_at(root, base, old)
        now = _sites_at(root, None, new)
        files.append((new, before, now))
    left: Counter[tuple[str, str]] = Counter()
    for _new, before, now in files:
        left.update(before - now)
    risen: dict[tuple[str, str], tuple[int, int]] = {}
    for new, before, now in files:
        if new is None:
            continue
        arrived = now - before
        for klass in sorted({klass for klass, _site in now}):
            was = sum(count for (k, _site), count in before.items() if k == klass)
            count = sum(count for (k, _site), count in now.items() if k == klass)
            excess = count - was
            for site in sorted(key for key in arrived if key[0] == klass):
                moved = min(excess, arrived[site], left[site])
                left[site] -= moved
                excess -= moved
            if excess > 0:
                risen[(klass, new)] = (count, count - excess)
    return risen


# ── the ratchet ───────────────────────────────────────────────────────


def test_no_changed_file_gains_a_flaky_shape() -> None:
    root = repo_root()
    base, how = merge_base(root)
    if base is None:
        message = f"flake-pattern ratchet compared nothing: {how}"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            _write_job_summary(f"**Warning:** {message}.")
        warnings.warn(message, UserWarning, stacklevel=1)
        return
    risen = change_risen(root, base)
    print(f"compared with {base[:12]}, {how}")
    detail = "\n".join(
        f"  {path}: {klass} {now} > {allowed}"
        for (klass, path), (now, allowed) in sorted(risen.items())
    )
    assert not risen, (
        "this change adds a shape the Determinism contract rules out "
        "(docs/system-specs/common/testing-conventions.md#determinism-contract-read-this-first); "
        f"each count is this file's against {base[:12]}:\n{detail}\nFix the new site, or carry "
        "`# flake-ok: <reason>` on its line when the shape is deliberate."
    )


def _write_job_summary(line: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# ── the detectors find what they say, and only that ───────────────────

#: class -> (snippets the detector must count once each, a near miss it must not count).
PY_CASES: dict[str, tuple[tuple[str, ...], str]] = {
    "K1-sleep-before-assert": (
        (
            "import time\ndef test_x():\n    start()\n    time.sleep(0.2)\n    assert done()\n",
            "import time as _t\ndef test_x():\n    _t.sleep(1)\n    assert done()\n",
            "from time import sleep as pause\ndef test_x():\n    pause(0.5)\n    assert done()\n",
            "import time\ndef test_x():\n    with patch(f):\n        start()\n"
            "        time.sleep(0.2)\n    assert called\n",
            "import time\ndef test_x():\n    for item in items:\n        send(item)\n"
            "        if slow:\n            time.sleep(0.1)\n        assert got(item)\n",
        ),
        "import time\ndef test_x():\n    time.sleep(0)\n    assert done()\ndef helper():\n"
        "    time.sleep(1)\n    assert x\ndef test_poll():\n    while not done():\n"
        "        time.sleep(0.01)\n    assert done()\ndef test_inner():\n    def slow():\n"
        "        time.sleep(1)\n        assert x\n    run(slow)\n",
    ),
    "K2-async-sleep-before-assert": (
        (
            "import asyncio\nasync def test_x():\n    await asyncio.sleep(0.05)\n    assert seen\n",
            "import asyncio\nasync def test_x():\n    async with c:\n"
            "        await asyncio.sleep(0.1)\n        assert seen\n",
            "import asyncio as aio\nasync def test_x():\n    await aio.sleep(1)\n    assert seen\n",
        ),
        "import asyncio\nasync def test_x():\n    await asyncio.sleep(0)\n    assert seen\n"
        "async def test_y():\n    await asyncio.sleep(1)\n    a()\n    b()\n    c()\n"
        "    assert late\n",
    ),
    "K3-stdlib-clock-patch": (
        (
            "import time\ndef test_x(monkeypatch):\n    monkeypatch.setattr(time, 'monotonic', f)\n",
            "import time as _time\nfrom unittest import mock\ndef test_x():\n"
            "    with mock.patch.object(_time, 'time', f):\n        pass\n",
            "def test_x(monkeypatch):\n    monkeypatch.setattr('time.sleep', f)\n",
            "def test_x(monkeypatch):\n    monkeypatch.setattr(subject.time, 'monotonic', f)\n",
        ),
        "import os\ndef test_x(monkeypatch):\n    monkeypatch.setattr(subject, 'time', fake)\n"
        "    monkeypatch.setattr(os, 'getcwd', f)\n    monkeypatch.setattr('time.tzname', t)\n",
    ),
    "K4-bare-monkeypatch-undo": (
        ("def test_x(monkeypatch):\n    monkeypatch.undo()\n",),
        "def test_x(monkeypatch):\n    with monkeypatch.context() as mp:\n        mp.undo()\n",
    ),
    "K5-reload-or-eviction": (
        (
            "import importlib\ndef test_x():\n    importlib.reload(mod)\n",
            "from importlib import reload as again\ndef test_x():\n    again(mod)\n",
            "import sys\ndef test_x():\n    del sys.modules['m']\n",
        ),
        "import sys\ndef test_x(monkeypatch):\n    del sys.modules['m']\n"
        "    sys.modules['m'] = saved\n    reload(config)\n    store.reload()\n",
    ),
    "K6-raw-environ-write": (
        ("import os\ndef test_x():\n    os.environ['A'] = '1'\n",),
        "import os\ndef test_x(monkeypatch):\n    monkeypatch.setenv('A', '1')\n    x = os.environ['A']\n",
    ),
    "K7-naive-local-time": (
        (
            "from datetime import date\ndef test_x():\n    assert f() == date.today()\n",
            "import datetime\ndef test_x():\n    assert f() < datetime.datetime.now()\n",
            "from datetime import datetime\ndef test_x():\n"
            "    assert day() == datetime.now().astimezone().date()\n",
        ),
        "from datetime import datetime, timezone\ndef test_x():\n"
        "    assert f() == datetime.now(timezone.utc)\n",
    ),
    "K8-duration-upper-bound": (
        (
            "import time\ndef test_x():\n    t = time.monotonic()\n    run()\n"
            "    assert time.monotonic() - t < 1.0\n",
            "def test_x():\n    assert (\n        elapsed < 5.0\n    ), 'took too long'\n",
            "def test_x():\n    assert 0.15 < elapsed < 1.0\n",
            "def test_x():\n    assert after - before < 0.05\n",
        ),
        "import time\ndef test_x():\n    t = time.monotonic()\n    run()\n"
        "    assert time.monotonic() - t < LOST_RUN_SECS\n    assert elapsed > 0.1\n",
    ),
    "K9-short-literal-wait": (
        (
            "def test_x():\n    thread.join(0.5)\n",
            "async def test_x():\n    await asyncio.wait_for(f, 1)\n",
        ),
        "import pytest\ndef test_x():\n    thread.join(30)\n    with pytest.raises(TimeoutError):\n"
        "        fut.result(timeout=0.01)\n    assert not stopped.wait(0.1)\n",
    ),
    "K10-unseeded-random": (
        ("import random\ndef test_x():\n    data = random.randbytes(8)\n",),
        "import random\ndef test_x():\n    data = random.Random(7).randbytes(8)\n",
    ),
    "K11-literal-listener-port": (
        (
            "def test_x():\n    web.TCPSite(runner, '127.0.0.1', 8765)\n",
            "def test_x():\n    s.bind(('127.0.0.1', 9000))\n",
            "def test_x():\n    HTTPServer(('127.0.0.1', 8080), Handler)\n",
        ),
        "def test_x():\n    web.TCPSite(runner, '127.0.0.1', 0)\n    s.bind(('127.0.0.1', 0))\n"
        "    record = AppProcess(port=9137)\n",
    ),
    "K12-autouse-shared-monkeypatch": (
        ("import pytest\n@pytest.fixture(autouse=True)\ndef _pin(monkeypatch):\n    pass\n",),
        "import pytest\n@pytest.fixture(autouse=True)\ndef _pin(_floor_monkeypatch):\n    pass\n"
        "@pytest.fixture\ndef helper(monkeypatch):\n    pass\n",
    ),
    "K13-fixed-flush-ceiling": (
        ("def test_x():\n    assert emit.flush(timeout=5.0)\n",),
        "def test_x():\n    assert emit.flush()\n",
    ),
    "K14-unpaused-crew-log-rmtree": (
        (
            "import shutil\ndef test_x():\n    shutil.rmtree(crew_log_path('session', sid).parent)\n",
        ),
        "import shutil\ndef test_x():\n    with eager.paused():\n"
        "        shutil.rmtree(crew_log_path('session', sid).parent)\n",
    ),
}

JS_CASES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "J1-promise-sleep": (
        "website/src/a.test.tsx",
        (
            "await new Promise((r) => setTimeout(r, 50));\n",
            "await new Promise<void>((resolve) => setTimeout(resolve, 20));\n",
            "await new Promise((r) => { setTimeout(r, 150); });\n",
        ),
        "await new Promise((r) => setTimeout(r, 0));\n",
    ),
    "J2-wait-for-timeout": (
        "website/playwright/a.spec.ts",
        ("await page.waitForTimeout(400);\n",),
        "await expect(page.getByText('x')).toBeVisible();\n",
    ),
    "J3-unpinned-date": (
        "website/src/a.test.tsx",
        ("const now = Date.now();\n",),
        "vi.setSystemTime(new Date('2026-01-01'));\nconst now = Date.now();\n",
    ),
    "J4-playwright-visible-branch": (
        "website/playwright/a.spec.ts",
        ("if (await page.getByRole('dialog').isVisible()) {\n",),
        "await expect(page.getByRole('dialog')).toBeVisible();\n",
    ),
    "J5-duration-upper-bound": (
        "website/src/a.test.ts",
        ("expect(performance.now() - start).toBeLessThan(50);\n",),
        "expect(performance.now() - start).toBeLessThan(BUDGET_MS);\n",
    ),
    "J6-unrestored-fake-timers": (
        "website/src/a.test.ts",
        ("it('x', () => {\n  vi.useFakeTimers();\n});\n",),
        "it('x', () => {\n  vi.useFakeTimers();\n  try {\n    vi.advanceTimersByTime(5);\n"
        "  } finally {\n    vi.useRealTimers();\n  }\n});\n",
    ),
}


@pytest.mark.parametrize("klass", sorted(PY_CASES))
def test_each_python_detector_counts_its_shape_and_not_the_near_miss(klass: str) -> None:
    positives, negative = PY_CASES[klass]
    for positive in positives:
        assert count_python(positive)[0][klass] == 1, f"{klass} missed its own shape"
        assert any(w in positive for w in PY_TRIGGERS[klass]), f"{klass}'s trigger skips it"
    assert any(w in negative for w in PY_TRIGGERS[klass]), f"{klass}'s near miss is never read"
    assert count_python(negative)[0][klass] == 0, f"{klass} counted a near miss"


@pytest.mark.parametrize("klass", sorted(JS_CASES))
def test_each_js_detector_counts_its_shape_and_not_the_near_miss(klass: str) -> None:
    rel, positives, negative = JS_CASES[klass]
    for positive in positives:
        assert count_js(rel, positive)[0][klass] == 1, f"{klass} missed its own shape"
    assert count_js(rel, negative)[0][klass] == 0, f"{klass} counted a near miss"


def test_every_detector_has_cases() -> None:
    assert set(PY_CASES) == set(PY_DETECTORS) == set(PY_TRIGGERS)
    assert set(JS_CASES) == set(JS_DETECTORS)


@pytest.mark.parametrize("klass", sorted(PY_CASES))
def test_a_detector_that_finds_nothing_fails_its_positive_case(klass, monkeypatch) -> None:
    """Mutation proof: the positive case is what catches a detector gone blind."""
    monkeypatch.setitem(PY_DETECTORS, klass, lambda *_args: ())
    with pytest.raises(AssertionError, match="missed its own shape"):
        test_each_python_detector_counts_its_shape_and_not_the_near_miss(klass)


@pytest.mark.parametrize("klass", sorted(JS_CASES))
def test_a_js_detector_that_finds_nothing_fails_its_positive_case(klass, monkeypatch) -> None:
    monkeypatch.setitem(JS_DETECTORS, klass, lambda *_args: [])
    with pytest.raises(AssertionError, match="missed its own shape"):
        test_each_js_detector_counts_its_shape_and_not_the_near_miss(klass)


def test_a_flake_ok_line_is_excused_only_with_a_reason() -> None:
    shape = "import time\ndef test_x():\n    time.sleep(0.2){}\n    assert done()\n"
    counts, excused = count_python(shape.format("  # flake-ok: the sleep is the subject here"))
    assert counts["K1-sleep-before-assert"] == 0 and excused == 1
    for unreasoned in (
        "  # flake-ok: short",
        "  # flake-ok: ..........",
        "  # flake-ok: 1234567890",
    ):
        counts, excused = count_python(shape.format(unreasoned))
        assert counts["K1-sleep-before-assert"] == 1 and excused == 0, unreasoned
    js = "await page.waitForTimeout(400); // flake-ok: video pacing for the recording\n"
    assert count_js("website/playwright/a.spec.ts", js) == (Counter(), 1)


def test_a_fixtures_flake_ok_sits_on_its_decorator_or_def_line_only() -> None:
    fixture = (
        "import pytest\n@pytest.fixture(autouse=True){}\ndef _pin(monkeypatch):\n" "    x = 1{}\n"
    )
    marker = "  # flake-ok: patches nothing; fixes the setup order"
    assert count_python(fixture.format(marker, ""))[0]["K12-autouse-shared-monkeypatch"] == 0
    assert count_python(fixture.format("", marker))[0]["K12-autouse-shared-monkeypatch"] == 1


def test_the_scan_covers_the_test_trees_and_nothing_else() -> None:
    assert _is_python_test_file("test/test_a.py")
    assert _is_python_test_file("src/kiro_crew/apps/builtins/x/tests/test_a.py")
    assert _is_python_test_file(
        "src/kiro_crew/apps/builtins/x/crew/runtime/container_tests/test_a.py"
    )
    assert _is_python_test_file("conftest.py")
    assert not _is_python_test_file("src/kiro_crew/session.py")
    assert _is_js_test_file("website/src/pages/a.test.tsx")
    assert _is_js_test_file("website/playwright/a.spec.ts")
    assert _is_js_test_file("website/electron/test/a.test.js")
    assert not _is_js_test_file("website/src/pages/a.tsx")
    assert not _is_js_test_file("website/node_modules/x/test/a.test.js")


def _run_git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    ).stdout.strip()


_SITE = "def test_{name}():\n    time.sleep(0.2)\n    assert done()\n"


def _sites(*names: str) -> str:
    return "import time\n" + "".join(_SITE.format(name=name) for name in names)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A repo whose ``main`` holds a file with one K1 site, checked out on a branch."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GITHUB_BASE_REF", "GITHUB_EVENT_NAME", BASE_ENV, "GIT_INDEX_FILE", "GIT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    root = tmp_path / "repo"
    (root / "test").mkdir(parents=True)
    (root / "test" / "test_a.py").write_text(_sites("a"), encoding="utf-8")
    (root / "test" / "test_b.py").write_text("def test_b():\n    assert True\n", encoding="utf-8")
    _run_git(root, "init", "-q")
    _run_git(root, "checkout", "-q", "-b", "main")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "base")
    _run_git(root, "checkout", "-q", "-b", "change")
    return root


def test_the_merge_base_is_found_against_the_local_base_branch(git_repo) -> None:
    base, how = merge_base(git_repo)
    assert base == _run_git(git_repo, "rev-parse", "main") and "main" in how


def test_a_pull_request_merge_commit_is_measured_against_its_first_parent(
    git_repo, monkeypatch
) -> None:
    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "change")
    before = _run_git(git_repo, "rev-parse", "main")
    _run_git(git_repo, "checkout", "-q", "main")
    _run_git(git_repo, "merge", "-q", "--no-ff", "-m", "merge", "change")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    base, how = merge_base(git_repo)
    assert base == before and "first parent" in how
    assert change_risen(git_repo, base) == {("K1-sleep-before-assert", "test/test_b.py"): (1, 0)}


def test_a_site_already_on_the_base_does_not_red_a_change_to_another_file(git_repo) -> None:
    (git_repo / "test" / "test_b.py").write_text("def test_b():\n    assert 1\n", encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


def test_a_new_site_in_a_changed_file_is_named(git_repo, monkeypatch) -> None:
    import test_flake_pattern_ratchet as module

    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K1-sleep-before-assert", "test/test_b.py"): (1, 0)
    }
    monkeypatch.setattr(module, "repo_root", lambda: git_repo)
    with pytest.raises(AssertionError, match=r"test/test_b\.py: K1-sleep-before-assert 1 > 0"):
        module.test_no_changed_file_gains_a_flaky_shape()


def test_a_renamed_or_deleted_file_keeps_the_change_green(git_repo) -> None:
    _run_git(git_repo, "mv", "test/test_a.py", "test/test_c.py")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}
    _run_git(git_repo, "rm", "-q", "-f", "test/test_c.py")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


def test_sites_moved_into_a_new_file_stay_green_up_to_what_left(git_repo) -> None:
    (git_repo / "test" / "test_a.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    (git_repo / "test" / "test_part.py").write_text(_sites("a"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}
    (git_repo / "test" / "test_part.py").write_text(_sites("a", "extra"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K1-sleep-before-assert", "test/test_part.py"): (2, 1)
    }


def test_an_unreadable_merge_base_warns_and_compares_nothing(tmp_path, monkeypatch) -> None:
    import test_flake_pattern_ratchet as module

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for name in (
        BASE_ENV,
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GITHUB_ACTIONS",
        "GITHUB_STEP_SUMMARY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    assert merge_base(tmp_path)[0] is None
    with pytest.warns(UserWarning, match="compared nothing"):
        module.test_no_changed_file_gains_a_flaky_shape()


def test_the_ci_warning_goes_to_an_isolated_job_summary(tmp_path, monkeypatch) -> None:
    import test_flake_pattern_ratchet as module

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for name in (BASE_ENV, "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, "repo_root", lambda: tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with pytest.warns(UserWarning, match="compared nothing"):
        module.test_no_changed_file_gains_a_flaky_shape()
    assert "compared nothing" in summary.read_text(encoding="utf-8")


def test_a_forks_stale_origin_loses_to_a_newer_upstream(git_repo) -> None:
    old_main = _run_git(git_repo, "rev-parse", "main")
    _run_git(git_repo, "checkout", "-q", "main")
    (git_repo / "test" / "test_merged.py").write_text(_sites("merged"), encoding="utf-8")
    _run_git(git_repo, "add", "-A")
    _run_git(git_repo, "commit", "-q", "-m", "merged upstream")
    new_main = _run_git(git_repo, "rev-parse", "main")
    _run_git(git_repo, "update-ref", "refs/remotes/origin/main", old_main)
    _run_git(git_repo, "update-ref", "refs/remotes/upstream/main", new_main)
    _run_git(git_repo, "checkout", "-q", "-b", "fork-change")
    base, how = merge_base(git_repo)
    assert base == new_main and "upstream/main" in how
    assert change_risen(git_repo, base) == {}  # the merged site is on the base, not the change


def test_an_explicit_base_wins_over_everything(git_repo, monkeypatch) -> None:
    base = _run_git(git_repo, "rev-parse", "main")
    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "change")
    monkeypatch.setenv(BASE_ENV, "change")
    assert merge_base(git_repo) == (
        _run_git(git_repo, "rev-parse", "change"),
        f"the merge-base with change ({BASE_ENV})",
    )
    monkeypatch.setenv(BASE_ENV, base)
    assert merge_base(git_repo)[0] == base
    monkeypatch.setenv(BASE_ENV, "no-such-ref")
    assert merge_base(git_repo)[0] is None


def test_a_push_is_measured_against_its_first_parent(git_repo, monkeypatch) -> None:
    before = _run_git(git_repo, "rev-parse", "HEAD")
    (git_repo / "test" / "test_b.py").write_text(_sites("b"), encoding="utf-8")
    _run_git(git_repo, "commit", "-q", "-am", "pushed")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    base, how = merge_base(git_repo)
    assert base == before and "push" in how


def test_a_fixed_site_does_not_forgive_a_different_new_one(git_repo) -> None:
    (git_repo / "test" / "test_a.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    (git_repo / "test" / "test_new.py").write_text(_sites("new"), encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {
        ("K1-sleep-before-assert", "test/test_new.py"): (1, 0)
    }


def test_a_site_moved_between_existing_files_stays_green(git_repo) -> None:
    (git_repo / "test" / "test_a.py").write_text(
        "def test_other():\n    assert 1\n", encoding="utf-8"
    )
    moved = "def test_b():\n    assert True\n" + _sites("a").replace("import time\n", "")
    (git_repo / "test" / "test_b.py").write_text("import time\n" + moved, encoding="utf-8")
    assert change_risen(git_repo, merge_base(git_repo)[0]) == {}


def main() -> int:
    """Print current counts per file for burn-down review; no gating."""
    import json

    rows: dict[str, dict[str, int]] = {}
    for path in repo_files():
        rel = path.relative_to(repo_root()).as_posix()
        if not _counted(rel):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        counts, _ok = count_python(source) if _is_python_test_file(rel) else count_js(rel, source)
        for klass, count in counts.items():
            if count:
                rows.setdefault(rel, {})[klass] = count
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
