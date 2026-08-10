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


def export_root() -> str:
    """Where graded exports live. The wrapper refuses any cwd outside this root, so it is
    also the one directory the grader identity ever needs access to — keeping it OUT of the
    factory's home is the point (that home is 0700 precisely so the grader cannot read it).
    Created on demand, group-traversable so the grader can enter it."""
    import os
    from . import config
    sw = config.load_config().get("super_worker", {}) or {}
    root = str(sw.get("export_root") or os.environ.get("FACTORY_EXPORT_ROOT")
               or "/tmp/factory-grade")
    os.makedirs(root, exist_ok=True)
    try:
        os.chmod(root, 0o711)      # traverse-only: the grader enters its own export, and
    except OSError:                # cannot list what other candidates are being graded
        pass
    return root


def remove_export(path: str) -> None:
    """Delete a graded export. When isolation is on, the grader owns files it created, and
    the factory cannot unlink them from directories the grader also created — probed:
    shutil.rmtree raises PermissionError, and ignore_errors=True silently LEAVES the tree,
    which would leak a full checkout per candidate forever. So the removal runs as the same
    identity that made the mess, through the same pinned wrapper."""
    import os
    import shutil
    import subprocess
    if not path or not os.path.isdir(path):
        return
    user = grader_user()
    if user:
        try:
            subprocess.run(build_argv(["/bin/rm", "-rf", path], cwd=path,
                                      user=user, wrapper=grader_wrapper()),
                           capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
    shutil.rmtree(path, ignore_errors=True)   # sweeps the factory-owned parent either way


def grant_grader_access(path: str) -> None:
    """Let the grading identity read/write inside ONE export, via a targeted macOS ACL.

    A group bit is not usable here: on macOS every local account is in `staff`, so
    group-readable means readable by the operator, the worker, and anything else on the
    box — the opposite of what this phase is for. An ACL names exactly one user.

    Needed because pytest writes into its working tree (`__pycache__`, `.pytest_cache`,
    tmp files), and the export is created by the factory. No-op when isolation is off, and
    best-effort otherwise: a filesystem without ACL support surfaces as the grading run
    failing loudly (a red gate), never as a silent fallback to running unisolated."""
    import subprocess
    user = grader_user()
    if not user or not path:
        return
    perms = ("read,write,execute,delete,append,readattr,writeattr,readextattr,"
             "writeextattr,list,search,add_file,add_subdirectory,delete_child,"
             "file_inherit,directory_inherit")
    try:
        subprocess.run(["chmod", "-R", "+a", f"user:{user} allow {perms}", path],
                       capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def prepare_export(adapter, src_repo: str, dest: str, ref: str) -> str:
    """Export `ref` out of `src_repo` into `dest` and hand it to the grader.

    One call so the two halves cannot drift apart: an export nobody can enter fails every
    grading run, and an export granted without being detached would hand the grader a
    linked worktree into the factory's own git (Component C)."""
    adapter.export_tree(src_repo, dest, ref)
    grant_grader_access(dest)
    return dest
