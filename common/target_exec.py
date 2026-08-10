"""common/target_exec.py — the ONE place the factory executes code it did not write.

WHY (Phase 3, docs/plans/2026-08-09-worker-isolation-design.md): the factory runs
worker-authored code as itself on every candidate — the test suite, the red-proof nodes,
the acceptance test, and the post-merge re-baseline. `super_worker.require_test: true`
*mandates* the worker ship a test, and the frozen gate never covers `tests/`, so supplying
Python that the factory will execute is not an attack, it is the documented contract. A
reviewer proved a candidate `conftest.py` writing to the blackboard.

An OS boundary around the developer's `claude -p` call does nothing about this, which is
why the first version of Phase 3 was scoped wrong. THIS is the shortest path from a worker
to the control plane, and it is the one this module closes.

THE SEAM: every execution of candidate-authored code goes through `run_target_code`.
Keeping it to one function is what makes the boundary auditable — and what lets a test
assert that no execution path bypasses it.

- `grader_user` unset (the default): identical to the direct `subprocess.run` this
  replaced, byte for byte. Phase 3 ships OFF, like Phases 1 and 2.
- `grader_user` set: the command runs as that unprivileged identity via a wrapper the
  operator installs read-only and pins in sudoers. No root, and no `chown` — the first
  design's grant was effectively `sudo (root) chown -R <user> *`, a privilege-escalation
  primitive handed to the account the conductor runs as.

WHAT THIS DOES NOT DO: it does not make the worker honest. A worker can still lie in its
reply; the deterministic gates own that. It closes reach, not honesty.
"""
from __future__ import annotations

import subprocess
from typing import Optional, Sequence


class ExecResult:
    """The subset of CompletedProcess every caller here uses. A tiny class rather than the
    real thing so the isolated path (which wraps argv) and the direct path return exactly
    the same shape."""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def grader_user() -> str:
    """`super_worker.grader_user` — config-only, deliberately NOT in SETTINGS_SPEC.

    A knob that decides whether untrusted code is contained must not be reachable by the
    dashboard's settings route or proposable by the self-harness loop. Same reasoning as
    `super_worker.organizer` and `harness_engineer`: a loop that can widen its own
    containment is not contained."""
    from . import config
    sw = config.load_config().get("super_worker", {}) or {}
    return str(sw.get("grader_user") or "").strip()


def grader_wrapper() -> str:
    """Absolute path to the operator-installed, read-only wrapper the sudoers rule pins.
    Never derived from anything a worker can influence."""
    from . import config
    sw = config.load_config().get("super_worker", {}) or {}
    return str(sw.get("grader_wrapper") or "/opt/factory/run-target-code").strip()


def isolation_active() -> bool:
    return bool(grader_user())


def build_argv(argv: Sequence[str], *, cwd: str, user: str, wrapper: str) -> list[str]:
    """The isolated command line. `sudo -n` (never prompt): a grading run that stalls on a
    password prompt would hang the shift, and a missing grant must fail loudly at the first
    candidate rather than silently degrade to same-user execution — a silent fallback would
    be a security downgrade disguised as resilience."""
    return ["sudo", "-n", "-u", user, "--", wrapper, cwd, *list(argv)]


def run_target_code(argv: Sequence[str], *, cwd: str, timeout: int,
                    user: Optional[str] = None, runner=None) -> ExecResult:
    """Execute `argv` in `cwd`, isolated when a grader identity is configured.

    Never raises: a missing binary, a timeout, or a missing sudo grant becomes a non-zero
    ExecResult, because every caller here already treats a non-zero result as a failed gate.
    That keeps the failure mode of turning isolation ON the same shape as any other red
    gate, instead of an exception escaping into the rail."""
    # Resolved at CALL time, not bound as a default: a default argument would capture
    # subprocess.run at import and silently ignore every test that monkeypatches it.
    runner = runner or subprocess.run
    user = grader_user() if user is None else user
    if user:
        cmd = build_argv(argv, cwd=cwd, user=user, wrapper=grader_wrapper())
        run_cwd = None          # the wrapper does the chdir; we may not be able to cd there
    else:
        cmd = list(argv)
        run_cwd = cwd
    try:
        p = runner(cmd, cwd=run_cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return ExecResult(127, "", f"command not found: {e}")
    except subprocess.TimeoutExpired as e:
        return ExecResult(124, "", f"timed out after {timeout}s: {e}")
    return ExecResult(getattr(p, "returncode", 1),
                      getattr(p, "stdout", "") or "", getattr(p, "stderr", "") or "")
