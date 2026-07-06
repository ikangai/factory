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

import re
from typing import Optional

_TEST_SUFFIXES = ("_test.py", ".test.js", ".test.ts", ".test.jsx", ".test.tsx",
                  ".spec.js", ".spec.ts", "_test.go", "_test.rb")
_SOURCE_SUFFIXES = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb")

# Task 3.1: pull a RUNNABLE pytest ref out of a spec's free-text `acceptance` string — the
# named acceptance test the acceptance-exec gate executes in the candidate. CONSERVATIVE +
# safe-charset (a whitelist of [A-Za-z0-9_./-] in the path, [A-Za-z0-9_] in node-id segments)
# so an injected/odd token can never form a ref, and prose ("a retry test passes") yields None
# (fail-open — the gate simply doesn't run rather than fabricate a red). The ref must be under
# tests/, end in .py (NOT .pyc — the negative lookahead rejects a longer extension), and may
# carry a pytest node id (::TestClass::test_method).
#
# The safe charset includes '.' and '/' (needed for tests/sub/dir/test_x.py), so a '..' segment
# can still COMPOSE from those chars — and pytest resolves a file-path arg by filesystem
# traversal (it does NOT confine args to rootdir), so 'tests/../../x.py' would import/execute a
# module OUTSIDE the candidate's tests/ (with enough '..', outside cand_repo into the operator's
# tree = arbitrary code execution). The `_has_traversal` post-check rejects any '..' path
# segment so the whitelist truly "can't be walked out of tests/".
_TEST_REF_RE = re.compile(
    r"(?<![\w./-])(tests/[A-Za-z0-9_./-]+\.py(?![A-Za-z0-9_])(?:::[A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)*)?)")


def _has_traversal(ref: str) -> bool:
    """True if `ref`'s FILE PATH (the part before any '::' node id) contains a '..' segment —
    a directory literally NAMED with dots (e.g. 'a..b') is fine, only the exact '..' segment
    escapes the dir. Backslashes are normalized so a Windows-style separator can't hide one."""
    path = ref.split("::", 1)[0].replace("\\", "/")
    return ".." in path.split("/")


def extract_test_ref(acceptance) -> Optional[str]:
    """Extract a conservative `tests/<path>.py[::<name>[::<name>]]` pytest ref from a spec's
    free-text `acceptance` (a str, or a spec dict whose 'acceptance' is read) — or None when the
    text is prose / names no safe tests/ ref (fail-open). Never returns a partial/unsafe token,
    and never a '..' traversal ref that would resolve outside the candidate's tests/ dir."""
    if isinstance(acceptance, dict):
        acceptance = acceptance.get("acceptance", "")
    if not isinstance(acceptance, str) or not acceptance:
        return None
    m = _TEST_REF_RE.search(acceptance)
    if not m or _has_traversal(m.group(1)):
        return None
    return m.group(1)


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
