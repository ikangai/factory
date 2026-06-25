"""Frozen-source validator (design: docs/plans/2026-06-25-autonomous-code-factory.md).

The CODE-level analog of the spec's frozen block. When the factory can change the
target's source, a candidate diff is checked against a FROZEN PATH-SET (the target's
safety-critical files — sandbox, permission gates, destructive-action policy, self-mod
gate). A diff that touches any frozen path is rejected BEFORE grading, structurally —
so the autonomous factory can make the target better but never weaken its safety. The
frozen set is config, declared per target by the Target Adapter's `frozen_paths()`.

Pure + deterministic: no I/O, no live agent. `changed_paths` come either from a parsed
unified diff or directly from `git diff --name-only`.
"""
from __future__ import annotations

import fnmatch
import re

_PLUS = re.compile(r"^\+\+\+ b/(.+)$")
_MINUS = re.compile(r"^--- a/(.+)$")
_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$")


def changed_paths_from_diff(diff_text: str | None) -> list[str]:
    """Extract the set of file paths a unified diff touches — handling adds/deletes
    (one side is /dev/null) and renames (the `diff --git` header)."""
    paths: set[str] = set()
    for line in (diff_text or "").splitlines():
        m = _GIT.match(line)
        if m:
            paths.add(m.group(1))
            paths.add(m.group(2))
            continue
        m = _PLUS.match(line) or _MINUS.match(line)
        if m and m.group(1) != "/dev/null":
            paths.add(m.group(1))
    paths.discard("/dev/null")
    return sorted(paths)


def _is_frozen(path: str, patterns) -> bool:
    """A path is frozen if it matches a frozen pattern as an fnmatch glob, an exact
    path, or a directory prefix (so `clive/sandbox` freezes everything beneath it)."""
    p = path.lstrip("./")
    for raw in patterns:
        pat = str(raw).lstrip("./")
        if fnmatch.fnmatch(p, pat):
            return True
        base = pat.rstrip("/*")               # dir-prefix form: "clive/sandbox[/**]"
        if base and (p == base or p.startswith(base + "/")):
            return True
    return False


def frozen_violations(changed_paths, frozen_patterns) -> list[str]:
    """The subset of `changed_paths` that touch a frozen pattern (the violations)."""
    return [p for p in changed_paths if _is_frozen(p, frozen_patterns)]


def validate_code_candidate(*, diff_text: str | None = None, changed_paths=None,
                            frozen_patterns=()) -> tuple[bool, list[str]]:
    """Return (ok, violations). `ok` is False iff the candidate touches a frozen path.
    Supply either a raw unified diff (`diff_text`) or `changed_paths` directly."""
    paths = changed_paths if changed_paths is not None else changed_paths_from_diff(diff_text)
    violations = frozen_violations(paths, frozen_patterns)
    return (not violations, violations)
