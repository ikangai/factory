"""Spec-bound acceptance gate — a GSD (spec-driven development) integration.

The factory's gate already enforces *do no harm* (the target's test suite stays green). This
adds *prove the work*: a candidate that changes source code but adds/modifies **no test** is
rejected — so the gate measures task FULFILLMENT (the change is tested), not just
non-regression. This generalizes GSD's "the worker must satisfy the spec it was given (a
named test)" into a deterministic, diff-level check.

Test/source classification is a heuristic covering common conventions (a tests/ directory, a
test_*/*_test/*.spec/*.test name). It is NOT yet config-overridable — clive's source is .py and
its tests live under tests/, so the heuristic fits; broaden it here if you point the factory at a
target with other conventions.

design: docs/plans/2026-06-27-gsd-spec-driven-integration.md
"""
from __future__ import annotations

_TEST_SUFFIXES = ("_test.py", ".test.js", ".test.ts", ".test.jsx", ".test.tsx",
                  ".spec.js", ".spec.ts", "_test.go", "_test.rb")
_SOURCE_SUFFIXES = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb")


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").lower()


def _is_test(path: str) -> bool:
    p = _norm(path)
    base = p.rsplit("/", 1)[-1]
    if "/tests/" in p or p.startswith("tests/"):      # the strong signal — a tests/ directory
        return True
    if "/src/" in p or p.startswith("src/"):          # a module under src/ is SOURCE, even if
        return False                                  # named test_* (a production test-helper)
    return (base.startswith("test_") or p.endswith(_TEST_SUFFIXES)
            or ".test." in base or ".spec." in base)


def _is_source(path: str) -> bool:
    p = _norm(path)
    if _is_test(p):                                   # a test file is not source-needing-a-test
        return False
    return p.endswith(_SOURCE_SUFFIXES)


def acceptance_ok(changed_paths) -> tuple[bool, str]:
    """(ok, reason). Fails only when the diff changes SOURCE code yet ships NO test. A
    test-only, docs-only, or empty diff passes (nothing to prove with a new test)."""
    paths = [p for p in (changed_paths or []) if p]
    if not paths:
        return True, ""
    has_source = any(_is_source(p) for p in paths)
    has_test = any(_is_test(p) for p in paths)
    if has_source and not has_test:
        return (False, "code change ships no test — add or modify a test that proves the "
                       "change (acceptance gate)")
    return True, ""
