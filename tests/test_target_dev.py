"""Code-development seam on the Target Adapter (design: docs/plans/2026-06-25-...):

  * run_tests(cwd)  — run the target's OWN test suite as the hard correctness gate for
                      a code candidate (a change that breaks the target's tests can't
                      promote, full-auto or not).
  * clone(dest)     — a self-contained git clone of the target (own .git, so it works
                      across the Guest-House user boundary) for a developer super-worker.

Hermetic — subprocess is monkeypatched; ONE real clone of a tiny throwaway repo proves
the clone mechanism end-to-end without cloning the real target.
"""
import os
import subprocess
import types

from factory.adapters.clive import CliveAdapter
from factory.common import config


def _done(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_tests_passes_when_suite_green(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["cwd"] = kw.get("cwd")
        return _done(0, "42 passed in 1.2s")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, report = CliveAdapter().run_tests("/tmp/clone")
    assert ok and "passed" in report
    assert seen["cwd"] == "/tmp/clone"               # ran in the candidate's checkout
    assert "pytest" in " ".join(seen["cmd"])         # clive's test suite


def test_run_tests_fails_when_suite_red(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _done(1, "1 failed, 41 passed"))
    ok, report = CliveAdapter().run_tests("/tmp/clone")
    assert not ok and "failed" in report


def test_run_tests_never_crashes_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)
    monkeypatch.setattr(subprocess, "run", boom)
    ok, report = CliveAdapter().run_tests("/tmp/clone")
    assert not ok and report


def test_run_tests_uses_configured_command(monkeypatch):
    monkeypatch.setattr(config, "target_config", lambda: {"test_command": ["pytest", "-x"]})
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: seen.__setitem__("cmd", cmd) or _done(0, "ok"))
    CliveAdapter().run_tests("/tmp/c")
    assert seen["cmd"] == ["pytest", "-x"]           # config overrides the clive default


def test_run_named_test_rejects_parent_traversal(monkeypatch):
    """Defense-in-depth: even if a '..' ref reaches the adapter directly, pytest is NEVER
    spawned against a traversal path — it maps to a fail-open 'missing' (a telemetry skip,
    not a discard) so nothing outside the candidate's tests/ can be imported/executed."""
    called = {"ran": False}

    def boom(*a, **k):
        called["ran"] = True
        raise AssertionError("subprocess must not run for a traversal ref")

    monkeypatch.setattr(subprocess, "run", boom)
    status, report = CliveAdapter().run_named_test(
        "/tmp/clone", "tests/../../../etc/passwd.py::test_x")
    assert status == "missing" and called["ran"] is False and report


def test_run_named_test_runs_a_safe_ref(monkeypatch):
    """A ref with no '..' segment runs normally (guard is not over-broad)."""
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: seen.__setitem__("cmd", cmd) or _done(0, "1 passed"))
    status, _ = CliveAdapter().run_named_test("/tmp/clone", "tests/test_x.py::test_y")
    assert status == "passed"
    assert "tests/test_x.py::test_y" in seen["cmd"]


def test_clone_constructs_git_clone(monkeypatch):
    monkeypatch.setattr(config, "clive_entry", lambda: ("/fake/clive", "/fake/clive/clive.py"))
    seen = {}
    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, **kw: seen.__setitem__("cmd", cmd) or _done(0))
    dest = CliveAdapter().clone("/tmp/dest")
    assert dest == "/tmp/dest"
    assert seen["cmd"][:2] == ["git", "clone"]
    assert "/fake/clive" in seen["cmd"] and "/tmp/dest" in seen["cmd"]


def test_clone_real_tiny_repo(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.py").write_text("print('hi')\n")
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=str(src), check=True, capture_output=True)
    monkeypatch.setattr(config, "clive_entry", lambda: (str(src), str(src / "x")))

    dest = str(tmp_path / "clone")
    CliveAdapter().clone(dest)
    assert os.path.exists(os.path.join(dest, "hello.py"))   # files came across…
    assert os.path.isdir(os.path.join(dest, ".git"))        # …with a self-contained .git
