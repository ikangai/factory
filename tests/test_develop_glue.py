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

    def branch_exists(self, repo, branch):
        return getattr(self, "_has_branch", True)

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
    # ships a test alongside the source so the now-default-on require_test gate passes
    ad = FakeAdapter(changed=["src/clive/feature.py", "tests/test_feature.py"], tests_passed=True)
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


def test_no_candidate_when_worker_produced_no_branch(monkeypatch, tmp_path):
    """ada's find: a worker that crashes / commits nothing leaves NO candidate branch; the
    rail must return a clean no_candidate, not let `git diff base <missing>` (exit 128) surface
    as an 'error — blocked'. changed_paths must not even run when the branch is absent."""
    ad = FakeAdapter(changed=["x"])           # changed_paths WOULD return a change if reached…
    ad._has_branch = False                     # …but the worker never created the branch

    def boom(*a, **k):
        raise AssertionError("changed_paths must NOT run when the candidate branch is missing")

    ad.changed_paths = boom
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                    task="t", champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "no_candidate"
    assert "fetch" not in [c[0] for c in ad.calls]   # never fetched/merged a non-existent branch


def test_no_candidate_when_changed_paths_git_diff_fails(monkeypatch, tmp_path):
    """exit-128 part 2 (turing's find): the branch EXISTS (branch_exists passes) but
    `git diff base branch` still fails (ref-resolution etc.) — changed_paths raises
    CalledProcessError(128). That must surface as a clean no_candidate, not a masked
    error-block. The second unguarded git-diff site, beyond the missing-branch one."""
    import subprocess
    ad = FakeAdapter(changed=["x"])           # branch_exists True (default), but the diff blows up

    def diff_128(*a, **k):
        raise subprocess.CalledProcessError(128, ["git", "diff", "--name-only"])

    ad.changed_paths = diff_128
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                    task="t", champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "no_candidate"
    assert "fetch" not in [c[0] for c in ad.calls]   # never tried to fetch/merge an un-diffable candidate


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

    def fake(text, **k):    # map by task text — parallel workers finish in any order
        return {"action": "merged", "merge_sha": "sha1"} if "merge me" in text else {"action": "no_candidate"}
    shipped = execute_claimed_tasks(s, sh, develop_fn=fake)

    assert shipped == 1
    assert s.get_task("m")["status"] == "done" and s.get_task("m")["result"] == "sha1"
    assert s.get_task("b")["status"] == "blocked" and s.get_task("b")["result"] == "no_candidate"
    assert s.get_task("o")["status"] == "open"               # untouched (never claimed)
    s.close()


def test_execute_dispatches_by_profile_and_ledgers_it(tmp_path):
    """Task 5.4: the rail resolves each task's capability profile ON THE MAIN THREAD and threads
    the overlay + resolved model into the worker, then ledgers the spend under the profile name.
    A named specialist → its overlay + standard tier; an unset profile → generalist + account
    default; an unknown-but-named profile → fails open DOWNWARD to standard, never frontier."""
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    s.add_profile("python-dev", description="d", overlay="PERSONA-MARKER", model="standard")
    s.add_task("t", "specialist task", source="issue")
    s.set_task_profile("t", "python-dev")
    s.set_task_status("t", "in_progress", shift_id=sh)
    s.add_task("g", "generalist task", source="issue")           # no profile → generalist default
    s.set_task_status("g", "in_progress", shift_id=sh)
    s.add_task("x", "orphaned task", source="issue")
    s.set_task_profile("x", "ghost-retired")                     # a name with no row → fail open
    s.set_task_status("x", "in_progress", shift_id=sh)

    seen = {}

    def fake(text, **k):
        seen[text] = dict(k)
        return {"action": "merged", "merge_sha": "s", "tokens": 100, "cost": 0.01, "seconds": 2.0}

    execute_claimed_tasks(s, sh, develop_fn=fake)

    spec = seen["specialist task"]
    assert spec["profile_overlay"] == "PERSONA-MARKER" and spec["model"] == "claude-sonnet-4-6"
    assert s._one("SELECT profile FROM budget_ledger WHERE role_or_run='developer:t'")["profile"] == "python-dev"

    gen = seen["generalist task"]                                # unset → account default, generalist
    assert gen["profile_overlay"] == "" and gen["model"] == ""
    assert s._one("SELECT profile FROM budget_ledger WHERE role_or_run='developer:g'")["profile"] == "generalist"

    orphan = seen["orphaned task"]                               # unknown name → standard, NOT frontier
    assert orphan["profile_overlay"] == "" and orphan["model"] == "claude-sonnet-4-6"
    assert s._one("SELECT profile FROM budget_ledger WHERE role_or_run='developer:x'")["profile"] == "generalist"
    s.close()


def test_execute_threads_require_test_to_the_gate(tmp_path):
    """Task 6.1: require_test is threaded from the run entry through execute_claimed_tasks into the
    developer pipeline (so a store override can retune it) instead of being re-read from config
    inside develop_and_merge."""
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    s.add_task("t", "x", source="issue")
    s.set_task_status("t", "in_progress", shift_id=sh)
    seen = {}

    def fake(text, **k):
        seen.update(k)
        return {"action": "merged", "merge_sha": "s"}

    execute_claimed_tasks(s, sh, develop_fn=fake, require_test=True)
    assert seen["require_test"] is True
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


def test_execute_caps_tasks_per_shift(tmp_path):
    """Unattended safety: the executor runs at most max_tasks claimed tasks per shift; the
    rest stay in_progress (run_shift requeues them) so one shift can't fan out unbounded."""
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    for i in range(5):
        s.add_task(f"t{i}", "x", source="issue")
        s.set_task_status(f"t{i}", "in_progress", shift_id=sh)
    calls = []

    def fake(text, **k):
        calls.append(text)
        return {"action": "merged", "merge_sha": "abc123def456"}

    shipped = execute_claimed_tasks(s, sh, develop_fn=fake, max_tasks=2)
    assert len(calls) == 2 and shipped == 2           # only the cap ran
    assert len(s.tasks_in_flight(sh)) == 3            # the rest left for requeue
    s.close()


def test_execute_halts_between_tasks_on_kill_switch(tmp_path, monkeypatch):
    """A long execute phase honors STOP — it re-checks the kill-switch between tasks."""
    from factory.orchestrator import develop as dev
    from factory.common import killswitch
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    for i in range(3):
        s.add_task(f"t{i}", "x", source="issue")
        s.set_task_status(f"t{i}", "in_progress", shift_id=sh)
    state = {"halt": False}
    monkeypatch.setattr(killswitch, "is_halted", lambda: state["halt"])
    calls = []

    def fake(text, **k):
        calls.append(text)
        state["halt"] = True                          # STOP trips after the first task
        return {"action": "merged", "merge_sha": "x"}

    dev.execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    assert len(calls) == 1                             # stopped before the 2nd task
    s.close()


def test_execute_runs_super_workers_in_parallel(tmp_path):
    """The conductor claims distinct-file tasks; the rail develops them CONCURRENTLY."""
    import threading
    import time
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    for i in range(3):
        s.add_task(f"t{i}", "x", source="issue")
        s.set_task_status(f"t{i}", "in_progress", shift_id=sh)
    active = {"now": 0, "max": 0}
    lk = threading.Lock()

    def fake(text, **k):
        with lk:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lk:
            active["now"] -= 1
        return {"action": "merged", "merge_sha": "abc123def456"}

    shipped = execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=3)
    assert shipped == 3 and active["max"] >= 2          # genuinely concurrent, not one-at-a-time
    s.close()


def test_execute_serializes_the_merge_lock_only_in_real_mode(tmp_path):
    """Parallel workers in REAL mode share the factory/auto worktree → a merge lock is passed;
    throwaway clones are isolated → no lock."""
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard

    def seen_lock(real, tag):
        s = Blackboard(str(tmp_path / f"f{tag}.db"))
        s.init_db()
        sh = s.start_shift(token_budget=1)
        s.add_task("t", "x", source="issue")
        s.set_task_status("t", "in_progress", shift_id=sh)
        captured = []

        def fake(text, merge_lock=None, **k):
            captured.append(merge_lock)
            return {"action": "merged", "merge_sha": "x"}

        execute_claimed_tasks(s, sh, develop_fn=fake, real=real)
        s.close()
        return captured[0]

    assert seen_lock(True, "real") is not None          # real → serialize the shared worktree
    assert seen_lock(False, "throw") is None            # throwaway → isolated, no lock


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


def test_rail_ledgers_every_developer_dispatch(tmp_path):
    """Task 0.3: the rail records tokens/cost/seconds per task, per shift, for every
    non-halted verdict — merged and no_candidate ledgered, halted NOT (nothing ran)."""
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db")); s.init_db()
    sh = s.start_shift(token_budget=1)
    for tid, title in [("m", "merge me"), ("n", "no cand"), ("h", "halt me")]:
        s.add_task(tid, title, source="issue")
        s.set_task_status(tid, "in_progress", shift_id=sh)

    def fake(text, **k):
        if "merge me" in text:
            return {"action": "merged", "merge_sha": "abc", "tokens": 500, "cost": 0.02, "seconds": 3.0}
        if "no cand" in text:
            return {"action": "no_candidate", "tokens": 120, "cost": 0.006, "seconds": 1.5}
        return {"action": "halted"}                    # a mid-flight STOP for this one worker

    execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    led = {e["role_or_run"]: e for e in s.budget_entries()}
    assert led["developer:m"]["tokens"] == 500 and led["developer:m"]["shift_id"] == sh
    assert led["developer:m"]["cost"] == 0.02 and led["developer:m"]["seconds"] == 3.0
    assert led["developer:n"]["tokens"] == 120 and led["developer:n"]["shift_id"] == sh
    assert "developer:h" not in led                    # halted never ran → not ledgered
    s.close()


def test_rail_ledgers_a_mid_round_halt_that_spent(tmp_path):
    """A STOP tripped DURING the round returns 'halted' AFTER the worker + tests ran, carrying
    real spend (develop_and_merge stamps it). The rail MUST ledger it — dropping it under-counts
    exactly when the operator brakes. A bare pre-dispatch halt (no spend) stays un-ledgered."""
    from factory.orchestrator.develop import execute_claimed_tasks
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db")); s.init_db()
    sh = s.start_shift(token_budget=1)
    for tid, title in [("mid", "spent then halted"), ("pre", "halted before dispatch")]:
        s.add_task(tid, title, source="issue")
        s.set_task_status(tid, "in_progress", shift_id=sh)

    def fake(text, **k):
        if "spent then halted" in text:                # run_code_round re-checked STOP post-work
            return {"action": "halted", "tokens": 700, "cost": 0.03, "seconds": 4.0}
        return {"action": "halted"}                     # STOP already engaged → nothing ran

    execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    led = {e["role_or_run"]: e for e in s.budget_entries()}
    assert led["developer:mid"]["tokens"] == 700 and led["developer:mid"]["seconds"] == 4.0
    assert led["developer:mid"]["notes"] == "halted" and led["developer:mid"]["shift_id"] == sh
    assert "developer:pre" not in led                   # pre-dispatch halt carried no spend
    s.close()


def test_post_dispatch_error_carries_developer_spend(monkeypatch, tmp_path):
    """Task 0.2: a fetch/worktree/grade blow-up AFTER the worker ran still spent it — the error
    result must carry the spend keys so the rail ledgers real tokens, not a 0-token row."""
    ad = FakeAdapter(changed=["src/clive/feature.py"])

    def fetch_boom(*a, **k):
        raise RuntimeError("fatal: could not fetch candidate")

    ad.fetch_candidate = fetch_boom
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": "",
                                                "tokens": 900, "cost": 0.04})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                    task="t", champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "error" and res["stage"] == "merge"
    assert res["tokens"] == 900 and res["cost"] == 0.04 and res["seconds"] >= 0


def test_no_candidate_carries_developer_spend(monkeypatch, tmp_path):
    """Task 0.2: the developer's tokens/cost/seconds ride out on the no_candidate path
    (previously dropped — nothing reached the ledger)."""
    ad = FakeAdapter(changed=["x"])
    ad._has_branch = False                     # worker produced no branch → no_candidate
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": "",
                                                "tokens": 1234, "cost": 0.05})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                    task="t", champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "no_candidate"
    assert res["tokens"] == 1234 and res["cost"] == 0.05 and res["seconds"] >= 0


def test_merged_result_carries_developer_spend(monkeypatch, tmp_path):
    """Task 0.2: the merged result carries the same developer spend keys."""
    ad = FakeAdapter(changed=["src/clive/feature.py", "tests/test_feature.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": "did it",
                                                "tokens": 1234, "cost": 0.05})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "main"),
                                    task="add a thing", champion_scores=CHAMP,
                                    grade_fn=_good_grade, label="factory/cand-x")
    assert res["action"] == "merged"
    assert res["tokens"] == 1234 and res["cost"] == 0.05 and res["seconds"] >= 0
