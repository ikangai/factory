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


# ==========================================================================================
# Components C/D — what the grader is actually handed. These pin the two properties the
# whole boundary rests on: the checkout must not link back to the factory's git, and the
# POST-MERGE re-baseline (which v1 of this design missed entirely) must be isolated too.
# ==========================================================================================
def _repo(path, *, files=(("f.txt", "one"),)):
    import subprocess as sp
    os.makedirs(path, exist_ok=True)
    sp.run(["git", "init", "-q", "-b", "main", path], check=True)
    sp.run(["git", "-C", path, "config", "user.email", "t@e.c"], check=True)
    sp.run(["git", "-C", path, "config", "user.name", "t"], check=True)
    for name, body in files:
        with open(os.path.join(path, name), "w") as fh:
            fh.write(body)
    sp.run(["git", "-C", path, "add", "-A"], check=True)
    sp.run(["git", "-C", path, "commit", "-qm", "c1"], check=True)
    return path


def test_export_tree_is_self_contained_and_shares_no_inodes(tmp_path):
    """A linked worktree's .git is a FILE pointing into the source's object store and refs —
    including the branch about to be merged. And a plain local clone HARDLINKS objects when
    both sides share a filesystem, so the copy's inodes are the original's: that is exactly
    how the discarded v1 design would have mutated the real target repo."""
    src = _repo(str(tmp_path / "src"))
    dest = str(tmp_path / "export")
    _Adapter().export_tree(src, dest, "main")

    assert os.path.isdir(os.path.join(dest, ".git")), "must be its own repo, not a link"
    src_objs = [os.path.join(r, f) for r, _, fs in os.walk(os.path.join(src, ".git", "objects"))
                for f in fs]
    assert src_objs, "fixture produced no objects"
    for p in src_objs:
        assert os.stat(p).st_nlink == 1, "export hardlinked the source's objects"


def test_export_carries_no_route_back_to_the_factory_repo(tmp_path):
    """`git clone` leaves an `origin` remote aimed at the source, with fetch AND push URLs
    — so an export handed to another identity would ship with a ready-made route home:
    `git fetch origin <branch>` reads every branch the factory has, not just the
    candidate's (probed: it worked). A 0700 factory home makes that path unreadable in a
    correct guest house, but "detached" must not depend on a permission bit elsewhere
    being right."""
    import subprocess as sp
    src = _repo(str(tmp_path / "src"))
    sp.run(["git", "-C", src, "checkout", "-qb", "other"], check=True)
    with open(os.path.join(src, "other.txt"), "w") as fh:
        fh.write("factory-only work")
    sp.run(["git", "-C", src, "add", "-A"], check=True)
    sp.run(["git", "-C", src, "commit", "-qm", "other"], check=True)
    sp.run(["git", "-C", src, "checkout", "-q", "main"], check=True)

    dest = str(tmp_path / "export")
    _Adapter().export_tree(src, dest, "main")

    remotes = sp.run(["git", "-C", dest, "remote", "-v"], capture_output=True, text=True)
    assert remotes.stdout.strip() == "", "the export must carry no remote"
    back = sp.run(["git", "-C", dest, "fetch", "origin", "other"],
                  capture_output=True, text=True)
    assert back.returncode != 0, "the export could still fetch the factory's other branches"
    # and it is still a working repo, which is the point of exporting rather than archiving
    assert sp.run(["git", "-C", dest, "log", "-1"], capture_output=True).returncode == 0


def test_export_root_is_traverse_only(monkeypatch, tmp_path):
    """The grader enters its own export and cannot list what else is being graded."""
    monkeypatch.setenv("FACTORY_EXPORT_ROOT", str(tmp_path / "grade"))
    root = target_exec.export_root()
    assert os.path.isdir(root)
    assert os.stat(root).st_mode & 0o777 == 0o711


def test_remove_export_runs_as_the_grader_when_isolated(monkeypatch, tmp_path):
    """The factory cannot unlink files another identity created in directories that
    identity also created — probed: rmtree raises, and ignore_errors silently LEAVES the
    tree, leaking a full checkout per candidate forever."""
    victim = tmp_path / "export"
    victim.mkdir()
    (victim / "f").write_text("x")
    calls = []
    monkeypatch.setattr(target_exec, "grader_user", lambda: "factory-grader")
    monkeypatch.setattr(target_exec, "grader_wrapper", lambda: "/opt/factory/run-target-code")
    monkeypatch.setattr(target_exec.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)))

    target_exec.remove_export(str(victim))

    assert calls, "isolated cleanup must go through the grader"
    assert calls[0][:4] == ["sudo", "-n", "-u", "factory-grader"]
    assert "/bin/rm" in calls[0] and str(victim) in calls[0]


def test_remove_export_is_a_plain_rmtree_when_not_isolated(monkeypatch, tmp_path):
    victim = tmp_path / "export"
    victim.mkdir()
    (victim / "f").write_text("x")
    monkeypatch.setattr(target_exec, "grader_user", lambda: "")
    called = []
    monkeypatch.setattr(target_exec.subprocess, "run", lambda *a, **k: called.append(a))

    target_exec.remove_export(str(victim))

    assert called == [], "OFF must not invoke sudo at all"
    assert not victim.exists()


# ==========================================================================================
# The ON wiring itself. A mutation that hardwired isolation_active() to False left the whole
# suite green — nothing asserted that arming it changes anything, which is exactly why two
# of three export sites shipped landing outside the root the wrapper confines to.
# ==========================================================================================
def test_new_export_always_lands_inside_the_export_root(tmp_path, monkeypatch):
    monkeypatch.setenv("FACTORY_EXPORT_ROOT", str(tmp_path / "grade"))
    monkeypatch.setattr(target_exec, "grader_user", lambda: "")   # skip the ACL call
    src = _repo(str(tmp_path / "src"))

    dest = target_exec.new_export(_Adapter(), src, "main")

    root = os.path.realpath(target_exec.export_root())
    assert os.path.realpath(dest).startswith(root + os.sep), (
        "an export outside the root is refused by the wrapper — silently, in the red-proof "
        "case, where a refusal would otherwise SATISFY the gate")
    assert os.path.isdir(os.path.join(dest, ".git"))


def test_a_wrapper_refusal_is_not_a_test_result():
    """rc 126 must not fall through to 'missing': 'missing' satisfies the red-proof gate, so
    an infrastructure refusal landing there turns the discriminating-test gate off while
    reporting success."""
    import factory.common.target_exec as te
    assert te.WRAPPER_REFUSED == 126

    class _RefusingAdapter(_Adapter):
        pass

    ad = _RefusingAdapter()
    import unittest.mock as mock
    with mock.patch.object(te, "run_target_code",
                           return_value=te.ExecResult(126, "", "refusing cwd outside")):
        status, report = ad.run_named_test("/anywhere", "tests/t.py::t")
        assert status == "refused", "a refusal must be distinguishable from 'missing'"
        ok, rep = ad.run_tests("/anywhere")
        assert ok is False and "REFUSED" in rep
