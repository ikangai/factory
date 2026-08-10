"""common/target_exec.py — the single seam through which the factory executes code it did
not write (Phase 3, docs/plans/2026-08-09-worker-isolation-design.md).

The bug this closes: `run_tests`/`run_named_test` called `subprocess.run` directly, so the
candidate's own test suite — which `require_test: true` MANDATES the worker ship, and which
the frozen gate never covers — ran as the factory user, with the blackboard, the credentials
and the killswitch in reach. A reviewer proved a candidate conftest.py writing the store.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from factory.adapters.base import TargetAdapter
from factory.common import target_exec

WRAPPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "deploy", "user-factory", "run-target-code")


class _Rec:
    """Records the argv/cwd a run would have used, without running anything."""

    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.calls = []
        self._rc, self._out, self._err = returncode, stdout, stderr

    def __call__(self, cmd, **kw):
        self.calls.append({"cmd": list(cmd), "cwd": kw.get("cwd")})
        return subprocess.CompletedProcess(cmd, self._rc, self._out, self._err)


# -- OFF: byte-identical to the direct call it replaced -------------------------------

def test_off_runs_the_command_directly_in_cwd():
    rec = _Rec()
    res = target_exec.run_target_code(["pytest", "-q"], cwd="/tmp/x", timeout=5,
                                      user="", runner=rec)
    assert rec.calls == [{"cmd": ["pytest", "-q"], "cwd": "/tmp/x"}]
    assert res.returncode == 0 and res.stdout == "ok"


def test_off_is_the_default_when_no_grader_is_configured(monkeypatch):
    monkeypatch.setattr(target_exec, "grader_user", lambda: "")
    rec = _Rec()
    target_exec.run_target_code(["pytest"], cwd="/tmp/x", timeout=5, runner=rec)
    assert rec.calls[0]["cmd"] == ["pytest"], "isolation must be OFF by default"
    assert target_exec.isolation_active() is False


# -- ON: the command is handed to the grader identity through the pinned wrapper -------

def test_on_wraps_the_command_for_the_grader_identity(monkeypatch):
    monkeypatch.setattr(target_exec, "grader_user", lambda: "factory-grader")
    monkeypatch.setattr(target_exec, "grader_wrapper", lambda: "/opt/factory/run-target-code")
    rec = _Rec()
    target_exec.run_target_code(["pytest", "-q"], cwd="/exports/c1", timeout=5, runner=rec)

    cmd = rec.calls[0]["cmd"]
    assert cmd[:4] == ["sudo", "-n", "-u", "factory-grader"]
    assert "--" in cmd and cmd[cmd.index("--") + 1] == "/opt/factory/run-target-code"
    assert cmd[-3:] == ["/exports/c1", "pytest", "-q"]
    assert rec.calls[0]["cwd"] is None, "the wrapper chdirs; we may not be able to"


def test_on_never_prompts_for_a_password():
    """A grading run that stalls on a password prompt would hang the shift, and a missing
    grant must fail loudly at the first candidate rather than silently degrade to same-user
    execution — a silent fallback is a security downgrade dressed as resilience."""
    argv = target_exec.build_argv(["pytest"], cwd="/e/c", user="g", wrapper="/w")
    assert "-n" in argv


# -- never raises: a failure must look like a red gate, not an escaping exception ------

def test_a_missing_binary_becomes_a_nonzero_result():
    def boom(cmd, **kw):
        raise FileNotFoundError("no such binary")
    res = target_exec.run_target_code(["nope"], cwd="/tmp", timeout=1, user="", runner=boom)
    assert res.returncode == 127 and "not found" in res.stderr


def test_a_timeout_becomes_a_nonzero_result():
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)
    res = target_exec.run_target_code(["pytest"], cwd="/tmp", timeout=1, user="", runner=slow)
    assert res.returncode == 124 and "timed out" in res.stderr


def test_a_missing_sudo_grant_is_a_failed_gate_not_a_fallback(monkeypatch):
    """`sudo -n` exits non-zero when no grant exists. That must surface as a red gate — it
    must NOT quietly re-run the command as the factory user."""
    monkeypatch.setattr(target_exec, "grader_user", lambda: "factory-grader")
    rec = _Rec(returncode=1, stdout="", stderr="sudo: a password is required")
    res = target_exec.run_target_code(["pytest"], cwd="/e/c", timeout=5, runner=rec)
    assert res.returncode == 1
    assert len(rec.calls) == 1, "a failed isolated run must not be retried unisolated"


# -- the knob is not reachable by anything the factory can talk into --------------------

def test_grader_user_is_not_a_board_or_harness_settable_knob():
    """A knob deciding whether untrusted code is contained must not be reachable from the
    dashboard's settings route or proposable by the self-harness loop."""
    from factory.common import config, harness_surface
    assert "super_worker.grader_user" not in config.SETTINGS_SPEC
    ok, _ = harness_surface.check_target("setting", "super_worker.grader_user")
    assert ok is False


# -- the adapters actually route through the seam --------------------------------------

class _Adapter(TargetAdapter):
    """Minimal concrete adapter — only the test-running surface matters here."""
    def test_command(self):
        return ["pytest", "tests", "-q"]

    # abstract surface the base declares; unused by these tests
    def entry(self):            return ("/t", "/t/x")
    def interpreter(self):      return "python3"
    def run(self, *a, **k):     raise NotImplementedError
    def actuate(self, *a, **k): raise NotImplementedError
    def panel_env(self, *a, **k):        return {}
    def scrub_env(self, *a, **k):        return {}
    def parse_session_dirs(self, *a, **k): return []


def test_run_tests_goes_through_the_seam(monkeypatch):
    seen = {}
    monkeypatch.setattr(target_exec, "run_target_code",
                        lambda argv, **kw: seen.update(argv=list(argv), **kw)
                        or target_exec.ExecResult(0, "1 passed"))
    ok, report = _Adapter().run_tests("/exports/c1")
    assert ok and "passed" in report
    assert seen["argv"] == ["pytest", "tests", "-q"] and seen["cwd"] == "/exports/c1"


def test_run_named_test_goes_through_the_seam(monkeypatch):
    seen = {}
    monkeypatch.setattr(target_exec, "run_target_code",
                        lambda argv, **kw: seen.update(argv=list(argv), **kw)
                        or target_exec.ExecResult(0, "1 passed"))
    status, _ = _Adapter().run_named_test("/exports/c1", "tests/test_x.py::test_y")
    assert status == "passed"
    assert "tests/test_x.py::test_y" in seen["argv"]
    assert "tests" not in seen["argv"], "the suite arg must be swapped, not appended"


def test_no_candidate_code_path_bypasses_the_seam():
    """The boundary is only auditable if it is the ONLY door. Any future direct
    subprocess.run of the target's test command in the adapter would reopen the hole."""
    import inspect
    src = inspect.getsource(TargetAdapter.run_tests) + inspect.getsource(
        TargetAdapter.run_named_test)
    # Strip comments and docstring prose: those legitimately MENTION subprocess.run while
    # explaining why it is no longer called. Only real code counts.
    code = "\n".join(l.split("#", 1)[0] for l in src.split("\n"))
    assert "subprocess.run(" not in code, "candidate code must only run via target_exec"
    assert code.count("run_target_code(") == 2


# -- the wrapper's own confinement ------------------------------------------------------

@pytest.mark.skipif(not os.path.exists(WRAPPER), reason="wrapper not installed in-tree")
def test_wrapper_refuses_a_cwd_outside_the_export_root(tmp_path):
    root = tmp_path / "root"
    (root / "proj").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    env = {**os.environ, "FACTORY_EXPORT_ROOT": str(root)}

    ok = subprocess.run([WRAPPER, str(root / "proj"), "/bin/echo", "hi"],
                        capture_output=True, text=True, env=env)
    assert ok.returncode == 0

    refused = subprocess.run([WRAPPER, str(outside), "/bin/echo", "hi"],
                             capture_output=True, text=True, env=env)
    assert refused.returncode == 126 and "refusing cwd" in refused.stderr


@pytest.mark.skipif(not os.path.exists(WRAPPER), reason="wrapper not installed in-tree")
def test_wrapper_refuses_a_symlink_that_escapes_the_export_root(tmp_path):
    """A plain prefix check would pass a symlinked export dir pointing at the very tree the
    grader is contained away from."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "sneaky")
    env = {**os.environ, "FACTORY_EXPORT_ROOT": str(root)}

    refused = subprocess.run([WRAPPER, str(root / "sneaky"), "/bin/echo", "hi"],
                             capture_output=True, text=True, env=env)
    assert refused.returncode == 126
