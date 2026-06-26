"""The develop→grade→auto-merge glue (orchestrator/develop.py): one full turn of the
autonomous code loop. Hermetic — a fake adapter (no real git) + an injected
develop_candidate + grade_fn exercise the WHOLE chain (clone → develop → fetch →
worktree → run_code_round → cleanup) without a live worker or real repos.
"""
import os
import types

from factory.orchestrator import develop
from factory.roles import common


class FakeAdapter:
    def __init__(self, *, changed=("src/clive/feature.py",), frozen=(), tests_passed=True):
        self._changed = list(changed)
        self._frozen = list(frozen)
        self._tests_passed = tests_passed
        self.calls = []

    def clone(self, dest):
        self.calls.append(("clone", dest)); os.makedirs(dest, exist_ok=True); return dest

    def default_branch(self, repo):
        return "main"

    def test_command(self):
        return ["pytest", "-q"]

    def frozen_paths(self):
        return self._frozen

    def changed_paths(self, repo, *refs):
        return list(self._changed)

    def fetch_candidate(self, repo, clone_dir, branch):
        self.calls.append(("fetch", branch)); return branch

    def add_worktree(self, repo, dest, branch):
        self.calls.append(("worktree", dest)); os.makedirs(dest, exist_ok=True); return dest

    def remove_worktree(self, repo, dest):
        self.calls.append(("rm_worktree", dest))

    def run_tests(self, repo, **k):
        return (self._tests_passed, "report")

    def merge_branch(self, repo, branch, **k):
        self.calls.append(("merge", branch)); return "MERGESHA"

    def revert_commit(self, repo, sha):
        self.calls.append(("revert", sha)); return "REVERTSHA"

    def current_commit(self, repo):
        return "HEAD"


import os  # noqa: E402

CHAMP = {"working": 0.8, "held_out": 0.7}


def _good_grade(repo):
    return {"working": 0.85, "held_out": 0.7, "held_out_measured": True,
            "divergence_alarm": False, "safety_flag": False}


def test_full_turn_develops_grades_and_merges(monkeypatch, tmp_path):
    ad = FakeAdapter(changed=["src/clive/feature.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": "did it"})

    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "main"),
                                    task="add a thing", champion_scores=CHAMP,
                                    grade_fn=_good_grade, label="factory/cand-x")
    assert res["action"] == "merged"
    seq = [c[0] for c in ad.calls]
    assert seq == ["clone", "fetch", "worktree", "merge", "rm_worktree"]   # full chain, in order


def test_no_candidate_when_worker_changed_nothing(monkeypatch, tmp_path):
    ad = FakeAdapter(changed=[])     # the worker committed no change
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                    task="t", champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "no_candidate"
    assert ("merge", "factory/cand-x") not in ad.calls and "fetch" not in [c[0] for c in ad.calls]


def test_frozen_violation_discards_before_merge(monkeypatch, tmp_path):
    ad = FakeAdapter(changed=["src/clive/selfmod/gate.py"], frozen=["src/clive/selfmod/"])
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                    task="weaken safety", champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "discarded" and res["stage"] == "frozen"
    assert "merge" not in [c[0] for c in ad.calls]    # never merged a frozen-touching change


def test_execute_claimed_tasks_closes_merged_done_and_failed_blocked(tmp_path):
    """The deterministic executor (replaces the conductor running develop-once via Bash):
    each claimed task → merged→done(sha), else→blocked(reason). Worker injected, no spawn."""
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    s.add_task("m", "merge me", source="issue"); s.set_task_status("m", "in_progress", shift_id=sh)
    s.add_task("b", "fails", source="issue"); s.set_task_status("b", "in_progress", shift_id=sh)
    s.add_task("o", "open, not claimed", source="issue")     # not in-flight → executor ignores it

    results = iter([{"action": "merged", "merge_sha": "sha1"}, {"action": "no_candidate"}])
    shipped = execute_claimed_tasks(s, sh, develop_fn=lambda text, **k: next(results))

    assert shipped == 1
    assert s.get_task("m")["status"] == "done" and s.get_task("m")["result"] == "sha1"
    assert s.get_task("b")["status"] == "blocked" and s.get_task("b")["result"] == "no_candidate"
    assert s.get_task("o")["status"] == "open"               # untouched (never claimed)
    s.close()


def test_execute_claimed_tasks_captures_the_block_reason(tmp_path):
    """A bare 'error' is undiagnosable — when develop_task raises, the exception message
    is threaded into the task result so the operator + the conductor can see WHY."""
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    s.add_task("t", "x", source="issue")
    s.set_task_status("t", "in_progress", shift_id=sh)

    def boom(text, **k):
        raise RuntimeError("git worktree add failed: fatal: branch already checked out")

    execute_claimed_tasks(s, sh, develop_fn=boom)
    t = s.get_task("t")
    assert t["status"] == "blocked"
    assert t["result"].startswith("error:") and "git worktree" in t["result"]   # the WHY is captured
    s.close()


def test_factory_worktree_creates_branch_and_is_idempotent(tmp_path):
    """Real-clive graduation: a persistent factory/auto worktree of the REAL target, created
    off HEAD, leaving the operator's checkout untouched."""
    import subprocess
    repo = str(tmp_path / "target")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
    (tmp_path / "target" / "f.txt").write_text("x")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "init"], check=True)

    class A:
        def entry(self):
            return (repo, "x")

    wt = develop.factory_worktree(A(), branch="factory/auto")
    assert wt == repo + ".factory-auto" and os.path.exists(os.path.join(wt, ".git"))
    branches = subprocess.run(["git", "-C", repo, "branch", "--list", "factory/auto"],
                              capture_output=True, text=True).stdout
    assert "factory/auto" in branches
    assert develop.factory_worktree(A()) == wt          # idempotent — reuses the worktree


def test_develop_task_real_merges_into_the_persistent_worktree(monkeypatch):
    seen = {}
    monkeypatch.setattr(develop, "factory_worktree", lambda adapter, **k: "/wt/factory-auto")
    monkeypatch.setattr(develop, "config", types.SimpleNamespace(get_adapter=lambda: object()))
    monkeypatch.setattr(develop, "develop_and_merge",
                        lambda **k: seen.update(k) or {"action": "merged", "merge_sha": "s"})
    res = develop.develop_task("do x", real=True)
    assert seen["main_repo"] == "/wt/factory-auto"      # merged into the REAL worktree, not a throwaway
    assert res["action"] == "merged"


def test_execute_claimed_tasks_passes_real_through(tmp_path):
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    s.add_task("t", "x", source="issue")
    s.set_task_status("t", "in_progress", shift_id=sh)
    seen = {}
    develop.execute_claimed_tasks(s, sh, real=True,
                                  develop_fn=lambda text, **k: seen.update(k) or {"action": "merged", "merge_sha": "z"})
    assert seen["real"] is True
    s.close()


def test_halted_kill_switch(monkeypatch, tmp_path):
    monkeypatch.setattr(develop.killswitch, "is_halted", lambda: True)
    ad = FakeAdapter()
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                    task="t", champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "halted" and ad.calls == []
