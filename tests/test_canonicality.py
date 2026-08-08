"""The canonicality matrix (Component E, design: docs/plans/2026-08-08-crash-
consistency-design.md; documented in docs/runbooks/crash-recovery.md §6) — cheap,
structural assertions, not runtime gates: SQLite is canonical for workflow/decisions,
git for artifacts, GitHub for published state, the agora bus for notifications ONLY, and
the dashboard writes ONLY through its authenticated command whitelist.

Static (AST/source-scan) checks, deliberately: a live-server or live-git test would cost
far more than this fact is worth verifying at every run, and these are structural
invariants about the CODE, not runtime state.
"""
from __future__ import annotations

import ast
import inspect
import os
import textwrap

import pytest

FACTORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(FACTORY_ROOT, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


# -- write-method discovery: derived from Blackboard itself, not hand-maintained ----

def _blackboard_write_method_names() -> set:
    """Every public Blackboard method whose body actually MUTATES (calls `self._exec(`
    or commits via `self.conn.execute(...)` + `self.conn.commit()`) — derived by
    introspection so this stays correct as store.py grows, rather than a hand-curated
    list that silently drifts."""
    from factory.common.store import Blackboard
    names = set()
    for name, member in inspect.getmembers(Blackboard, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        try:
            src = inspect.getsource(member)
        except (OSError, TypeError):
            continue
        if "self._exec(" in src or "self.conn.commit()" in src:
            names.add(name)
    return names


def test_write_method_discovery_finds_known_writers_and_excludes_known_readers():
    """Sanity check on the discovery mechanism itself before trusting it below."""
    names = _blackboard_write_method_names()
    for known_writer in ("add_task", "set_task_status", "begin_operation",
                         "complete_operation", "resolve_approval", "record_issue_sync"):
        assert known_writer in names, known_writer
    for known_reader in ("get_task", "list_tasks", "get_operation", "pending_approvals",
                         "active_mission"):
        assert known_reader not in names, known_reader


def _calls_a_write_method(source: str, write_methods: set) -> list:
    """Every write-method name called as `<anything>.<name>(...)` in `source` — an
    attribute-call scan, not a type-checked one (this module has no type info to know
    the receiver IS a Blackboard), which is exactly why this is scoped to modules that
    self-declare read-only rather than asserted repo-wide."""
    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in write_methods):
            hits.append(node.func.attr)
    return hits


# -- reporting/: the modules that SELF-DECLARE "read-only — never writes to the store" --
# (their own docstrings; see each module's header). NOT a blanket "nothing under
# reporting/ writes" claim — reporting/approvals.py, issue_sync.py, factory_memory.py,
# scope_check.py, human_queue.py are documented, legitimate workflow writers that happen
# to live in this package. See docs/runbooks/crash-recovery.md §6 for the full accounting.
_READONLY_REPORTING_MODULES = ("summary.py", "diary.py", "blog.py")

# `add_budget` is the one write ALL THREE of these modules legitimately make: ledgering
# their own LLM-call spend (budget_ledger — telemetry/accounting, not workflow state or
# a decision) is a ubiquitous, orthogonal pattern used by every role in the codebase,
# including deterministic gate-eval/harness/org code that is equally "read-only" in the
# sense this check cares about (never touches tasks/shifts/approvals/promotions). The
# "never writes to the store" docstring claim is about WORKFLOW writes; excluded here so
# the check asserts what it actually means, not a stricter claim nobody makes elsewhere.
_ACCOUNTING_ONLY_WRITES = {"add_budget"}


@pytest.mark.parametrize("module", _READONLY_REPORTING_MODULES)
def test_readonly_reporting_module_never_calls_a_store_write_method(module):
    source = _read("reporting", module)
    assert "read-only" in source.lower() and "never writes to the store" in source, (
        f"{module} no longer claims the read-only contract this test enforces — "
        f"update _READONLY_REPORTING_MODULES if that's an intentional change")
    write_methods = _blackboard_write_method_names() - _ACCOUNTING_ONLY_WRITES
    hits = _calls_a_write_method(source, write_methods)
    assert hits == [], f"{module} calls store write method(s) {hits} despite its own "\
                       f"'read-only — never writes to the store' docstring claim"


# -- the agora bus: notifications only, never the blackboard -----------------------

def test_bus_module_never_imports_the_store():
    source = _read("common", "bus.py")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "")
            assert "store" not in mod.split("."), (
                f"common/bus.py imports from {mod!r} — the bus is notifications-only "
                f"per the canonicality matrix (docs/runbooks/crash-recovery.md §6)")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "store" not in alias.name.split("."), (
                    f"common/bus.py imports {alias.name!r}")
    assert "Blackboard" not in source
    assert "paths.DB_PATH" not in source


def test_bus_module_reads_its_own_separate_db_not_the_blackboard():
    """common/bus.py connects to chat.db (the agora room state) — a completely
    different sqlite file from store/blackboard.db, and always read-only."""
    source = _read("common", "bus.py")
    assert "chat.db" in source
    assert "mode=ro" in source   # every sqlite3.connect in this module is read-only


# -- dashboard: writes ONLY through the whitelisted do_POST paths -------------------

def test_dashboard_post_guards_with_a_fixed_whitelist_before_reading_any_body():
    """`do_POST`'s FIRST real statement must be the whitelist refusal, and the
    whitelist itself must be a fixed tuple of string literals (never computed) — so no
    later change can accidentally move a body-read/store-write ahead of the gate."""
    from factory.dashboard.fleet_server import Handler
    src = textwrap.dedent(inspect.getsource(Handler.do_POST))
    tree = ast.parse(src)
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef) and func.name == "do_POST"

    # First statement after the `path = urlparse(self.path).path` assignment must be an
    # `if path not in (...)` guard that returns/refuses.
    stmts = func.body
    assert isinstance(stmts[0], ast.Assign)   # path = urlparse(self.path).path
    guard = stmts[1]
    assert isinstance(guard, ast.If), "do_POST's first conditional must be the whitelist guard"

    test = guard.test
    assert isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.NotIn), (
        "the guard must be `path not in (...)` — a positive whitelist, not a blacklist")
    whitelist_node = test.comparators[0]
    assert isinstance(whitelist_node, ast.Tuple), "the whitelist must be a literal tuple"
    paths = []
    for elt in whitelist_node.elts:
        assert isinstance(elt, ast.Constant) and isinstance(elt.value, str), (
            "every whitelist entry must be a fixed string literal, never computed")
        paths.append(elt.value)

    # The guard must actually REFUSE (return) inside its body — not just check-and-fall-
    # through into the write logic regardless.
    assert any(isinstance(n, ast.Return) for n in ast.walk(guard)) or all(
        isinstance(s, ast.Return) for s in guard.body[-1:]), \
        "the whitelist guard must refuse (return) on a miss"

    # Every whitelisted path is a real write action this test can name — a sanity floor
    # that also documents the current authoritative set for readers of this test.
    assert set(paths) == {
        "/api/mode", "/api/stop", "/api/resume", "/api/mission", "/api/settings",
        "/api/worker", "/api/queue/answer", "/api/queue/task", "/api/queue/approval",
    }


def test_dashboard_post_checks_the_whitelist_before_the_csrf_guard_and_any_body_read():
    """Ordering matters: an unknown path must 404 WITHOUT even reaching the
    origin/CSRF check or reading the request body — the whitelist is the outermost
    gate, not one check among several."""
    from factory.dashboard.fleet_server import Handler
    src = textwrap.dedent(inspect.getsource(Handler.do_POST))
    tree = ast.parse(src)
    func = tree.body[0]
    guard = func.body[1]
    assert isinstance(guard, ast.If)
    # Nothing in the guard's own body reads the request (rfile) or checks origin —
    # it is a pure path-membership refusal.
    guard_src = ast.get_source_segment(src, guard) or ""
    assert "rfile" not in guard_src
    assert "_local_origin" not in guard_src
