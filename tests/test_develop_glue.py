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

    def run_named_test(self, cwd, ref, **k):
        self.calls.append(("named", ref)); return getattr(self, "_named", ("passed", "acc ok"))

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


def test_reviewer_approves_and_merges(monkeypatch, tmp_path):
    """Phase 8: with the reviewer ON, an approve verdict merges and the review spend rides out."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"], "reply": "did it"})
    monkeypatch.setattr(common, "claude_p",
                        lambda prompt, **k: ('```json\n{"approve": true, "reason": "fits"}\n```', 40, 0.01))
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="add x",
                                    champion_scores=CHAMP, grade_fn=_good_grade, reviewer=True)
    assert res["action"] == "merged" and res["review_tokens"] == 40


def test_reviewer_rejects_and_discards(monkeypatch, tmp_path):
    """Phase 8: an explicit reject discards the candidate (stage 'review' → a blocked-task lesson)
    and NEVER merges; the review spend still rides out for ledgering."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    monkeypatch.setattr(common, "claude_p",
                        lambda prompt, **k: ('{"approve": false, "reason": "off scope"}', 30, 0.0))
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="t",
                                    champion_scores=CHAMP, grade_fn=_good_grade, reviewer=True)
    assert res["action"] == "discarded" and res["stage"] == "review" and "off scope" in res["error"]
    assert res["review_tokens"] == 30
    assert "merge" not in [c[0] for c in ad.calls]           # never merged a rejected candidate


def test_reviewer_fails_open_on_transport_failure(monkeypatch, tmp_path):
    """Phase 8: an unparseable/failed review is FAIL-OPEN — a reviewer hiccup never blocks a merge."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    monkeypatch.setattr(common, "claude_p", lambda prompt, **k: ("[claude -p unavailable]", 0, 0.0))
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="t",
                                    champion_scores=CHAMP, grade_fn=_good_grade, reviewer=True)
    assert res["action"] == "merged"                         # fail-open → merged despite no verdict


def test_reviewer_missing_approve_key_fails_open(monkeypatch, tmp_path):
    """Review #2 (Phase 8): a parseable verdict that OMITS/nulls the approve key is a reviewer
    output hiccup, not a rejection — it must fail open (merge), not discard a good candidate."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    monkeypatch.setattr(common, "claude_p",
                        lambda prompt, **k: ('{"reason": "looks good"}', 20, 0.0))   # no approve key
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="t",
                                    champion_scores=CHAMP, grade_fn=_good_grade, reviewer=True)
    assert res["action"] == "merged"                         # missing key → approve, not reject


def test_reviewer_off_by_default_never_runs(monkeypatch, tmp_path):
    """Phase 8 is config-gated OFF: with reviewer unset, the review transport is never invoked."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})

    def boom(*a, **k):
        raise AssertionError("the reviewer must not run when off")

    monkeypatch.setattr(common, "claude_p", boom)
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="t",
                                    champion_scores=CHAMP, grade_fn=_good_grade)   # reviewer defaults False
    assert res["action"] == "merged" and "review_tokens" not in res


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


# ============================================================================
# Task 0.1 (P11): split the empty-handed-worker collapse — a no-branch run is
# classified by its reply (timeout / worker_failed / transport / refusal) BEFORE
# collapsing to no_candidate, so infrastructure failures and refusals stop
# masquerading as "brief too big" evidence.
# ============================================================================

def _no_branch_result(monkeypatch, tmp_path, reply):
    """Run develop_and_merge with a worker that produced NO branch and `reply`."""
    ad = FakeAdapter(changed=["x"])
    ad._has_branch = False
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": reply,
                                                "tokens": 7, "cost": 0.001})
    return develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                     task="t", champion_scores=CHAMP, grade_fn=_good_grade)


def test_timeout_sentinel_classifies_error_timeout(monkeypatch, tmp_path):
    """A worker killed at the 30-min wall is the strongest 'task too big' evidence —
    it must surface as error(timeout), not a generic no_candidate."""
    res = _no_branch_result(
        monkeypatch, tmp_path,
        "[claude -p unavailable: Command '['claude', '-p']' timed out after 1800 seconds]")
    assert res["action"] == "error" and res["stage"] == "timeout"
    # every new error result carries the learnings/spend keys no_candidate carries
    assert "learnings" in res
    assert res["tokens"] == 7 and res["cost"] == 0.001 and res["seconds"] >= 0


def test_rc_sentinel_classifies_error_worker_failed(monkeypatch, tmp_path):
    """A non-zero rc (incl. max-turns exhaustion) is a worker failure, not scope evidence."""
    res = _no_branch_result(monkeypatch, tmp_path, "[claude -p super-worker failed: rc=1]")
    assert res["action"] == "error" and res["stage"] == "worker_failed"
    assert "learnings" in res
    assert res["tokens"] == 7 and res["cost"] == 0.001 and res["seconds"] >= 0


def test_unavailable_sentinel_classifies_error_transport(monkeypatch, tmp_path):
    """A FileNotFoundError-shaped sentinel (no 'timed out') means the brief was NEVER
    attempted — error(transport), decompose-suppressed downstream."""
    res = _no_branch_result(
        monkeypatch, tmp_path,
        "[claude -p unavailable: [Errno 2] No such file or directory: 'claude']")
    assert res["action"] == "error" and res["stage"] == "transport"
    assert "learnings" in res
    assert res["tokens"] == 7 and res["cost"] == 0.001 and res["seconds"] >= 0


def test_short_refusal_reply_classifies_error_refusal(monkeypatch, tmp_path):
    """A short reply with a refusal marker near the start + no branch = a refusal —
    the refusal text rides out in `error` for the blocked reason."""
    res = _no_branch_result(monkeypatch, tmp_path,
                            "I can't help with modifying this safety-critical code.")
    assert res["action"] == "error" and res["stage"] == "refusal"
    assert res["error"].startswith("I can't help")
    assert "learnings" in res
    assert res["tokens"] == 7 and res["cost"] == 0.001 and res["seconds"] >= 0


def test_long_reply_without_markers_stays_no_candidate(monkeypatch, tmp_path):
    """A genuinely empty-handed worker (long real-work reply, no branch) is today's path."""
    res = _no_branch_result(monkeypatch, tmp_path,
                            "I explored the codebase. " + "analysis " * 100)
    assert res["action"] == "no_candidate"


def test_long_reply_with_refusal_words_stays_no_candidate(monkeypatch, tmp_path):
    """A refusal marker buried in a LONG reply is real work, not a refusal."""
    res = _no_branch_result(monkeypatch, tmp_path,
                            ("detailed analysis " * 40) + " I can't help further here.")
    assert res["action"] == "no_candidate"


def test_honest_unable_reply_stays_no_candidate(monkeypatch, tmp_path):
    """Fix 0.1b: prompt.md tells workers committing nothing is VALID when they 'cannot
    make a safe, test-passing change' — an honest short 'I'm unable to …' report is
    genuine no_candidate (decompose-eligible), NOT a refusal. Capability statements
    ('unable to' without a refusal verb) must not be refusal markers."""
    res = _no_branch_result(
        monkeypatch, tmp_path,
        "I'm unable to make a safe, test-passing change — the brief spans three modules.")
    assert res["action"] == "no_candidate"


def test_must_decline_reply_classifies_error_refusal(monkeypatch, tmp_path):
    """Fix 0.1b: 'I must decline' is explicit refusal phrasing — error(refusal)."""
    res = _no_branch_result(monkeypatch, tmp_path,
                            "I must decline this brief; it asks me to weaken safety checks.")
    assert res["action"] == "error" and res["stage"] == "refusal"


def test_wont_help_reply_classifies_error_refusal(monkeypatch, tmp_path):
    """Fix 0.1b: 'I won't help' is explicit refusal phrasing — error(refusal)."""
    res = _no_branch_result(monkeypatch, tmp_path,
                            "I won't help with disabling the killswitch.")
    assert res["action"] == "error" and res["stage"] == "refusal"


def _exec_one(tmp_path, res_dict, *, decomposer=None):
    """Run one claimed task through execute_claimed_tasks with a fixed worker result."""
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1)
    s.add_task("task-1", "big brief", source="issue")
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    develop.execute_claimed_tasks(s, sh, develop_fn=lambda text, **k: dict(res_dict),
                                  decomposer=decomposer)
    t = s.get_task("task-1")
    s.close()
    return t


def test_error_timeout_is_decompose_eligible(tmp_path):
    """Task 0.1: the decompose trigger extends to error(timeout)."""
    calls = []

    def dec(task):
        calls.append(task["id"])
        return {"subtasks": [{"title": "slice one"}, {"title": "slice two"}]}

    t = _exec_one(tmp_path, {"action": "error", "stage": "timeout",
                             "error": "[claude -p unavailable: ... timed out after 1800 seconds]"},
                  decomposer=dec)
    assert calls == ["task-1"]
    assert t["status"] == "blocked" and "decomposed into 2" in t["result"]


def test_error_worker_failed_is_decompose_eligible(tmp_path):
    """Task 0.1: the decompose trigger extends to error(worker_failed)."""
    calls = []

    def dec(task):
        calls.append(task["id"])
        return {"subtasks": [{"title": "slice one"}]}

    t = _exec_one(tmp_path, {"action": "error", "stage": "worker_failed",
                             "error": "[claude -p super-worker failed: rc=1]"},
                  decomposer=dec)
    assert calls == ["task-1"]
    assert t["status"] == "blocked" and "decomposed into 1" in t["result"]


def test_error_transport_suppresses_decompose(tmp_path):
    """A dead transport never attempted the brief — decomposing it is pure spend."""
    def dec(task):
        raise AssertionError("the decomposer must not run for a transport failure")

    t = _exec_one(tmp_path, {"action": "error", "stage": "transport",
                             "error": "[claude -p unavailable: [Errno 2] No such file: 'claude']"},
                  decomposer=dec)
    assert t["status"] == "blocked" and "(transport)" in t["result"]


def test_error_refusal_suppresses_decompose_and_persists_the_reason(tmp_path):
    """A refusal is not scope evidence: decompose suppressed, and the refusal's first
    ~300 chars land in the close-out result (180 would clip the diagnosis)."""
    refusal = "I can't help with this brief. " + "z" * 300      # 330 chars

    def dec(task):
        raise AssertionError("the decomposer must not run for a refusal")

    t = _exec_one(tmp_path, {"action": "error", "stage": "refusal", "error": refusal},
                  decomposer=dec)
    assert t["status"] == "blocked" and "(refusal)" in t["result"]
    assert "z" * 270 in t["result"]          # exactly the first 300 chars persisted…
    assert "z" * 271 not in t["result"]      # …and no more


def test_error_without_stage_still_blocks_with_reason(tmp_path):
    """The pre-existing dispatch-error path (no stage) keeps its shape."""
    t = _exec_one(tmp_path, {"action": "error", "error": "git worktree add failed"})
    assert t["status"] == "blocked"
    assert t["result"].startswith("error:") and "git worktree" in t["result"]


# ============================================================================
# Task 2.3 (reviewer + scope-check calibration): gate-outcome scoring.
# Slice 1 — carry the reviewer verdict out (res['review']) + a reviewer-MISS
# learning when an APPROVED candidate ends auto_reverted.
# Slice 2 — a no_candidate whose task carries a scope-check-passed spec scores a
# scope_check calibration learning (the mirror of the merged-side spec-creep feedback).
# ============================================================================

def test_reviewer_approve_verdict_rides_out(monkeypatch, tmp_path):
    """Slice 1: an APPROVE verdict rides out on the merged result as res['review']."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": "did it"})
    monkeypatch.setattr(common, "claude_p",
                        lambda prompt, **k: ('{"approve": true, "reason": "fits"}', 40, 0.01))
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="add x",
                                    champion_scores=CHAMP, grade_fn=_good_grade, reviewer=True)
    assert res["action"] == "merged"
    assert res["review"] == {"approved": True, "reason": "fits"}


def test_reviewer_reject_verdict_rides_out(monkeypatch, tmp_path):
    """Slice 1: a REJECT verdict rides out on the discarded result as res['review']."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    monkeypatch.setattr(common, "claude_p",
                        lambda prompt, **k: ('{"approve": false, "reason": "off scope"}', 30, 0.0))
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="t",
                                    champion_scores=CHAMP, grade_fn=_good_grade, reviewer=True)
    assert res["action"] == "discarded" and res["stage"] == "review"
    assert res["review"] == {"approved": False, "reason": "off scope"}


def test_fail_open_review_carries_no_verdict(monkeypatch, tmp_path):
    """Slice 1: a fail-open review (no parseable verdict) carries NO 'review' key — there is
    no verdict to score, so a reviewer hiccup never fabricates a calibration signal."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    monkeypatch.setattr(common, "claude_p", lambda prompt, **k: ("[claude -p unavailable]", 0, 0.0))
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="t",
                                    champion_scores=CHAMP, grade_fn=_good_grade, reviewer=True)
    assert res["action"] == "merged" and "review" not in res


def test_reviewer_miss_learning_on_approved_auto_revert(tmp_path):
    """Slice 1: an APPROVED candidate that then auto-reverts is a reviewer MISS — recorded as
    a scope='reviewer_calibration' factory learning (a counter/ledger note, never a settings key)."""
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1000)
    s.add_task("task-1", "do x", source="issue")
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    dev_fn = lambda text, **k: {"action": "auto_reverted", "merge_sha": "m", "revert_sha": "r",
                                "review": {"approved": True, "reason": "lgtm"}}
    develop.execute_claimed_tasks(s, sh, develop_fn=dev_fn)
    assert s.get_task("task-1")["status"] == "blocked"
    miss = [r for r in s.learnings_for_role("factory") if r["scope"] == "reviewer_calibration"]
    assert len(miss) == 1 and "auto-revert" in miss[0]["content"].lower()
    s.close()


def test_no_reviewer_miss_when_no_review_verdict(tmp_path):
    """Slice 1: an auto_reverted candidate WITHOUT a reviewer verdict (reviewer off / fail-open)
    records NO reviewer_calibration learning — a miss requires the reviewer to have approved it."""
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1000)
    s.add_task("task-1", "do x", source="issue")
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    dev_fn = lambda text, **k: {"action": "auto_reverted", "merge_sha": "m", "revert_sha": "r"}
    develop.execute_claimed_tasks(s, sh, develop_fn=dev_fn)
    assert not any(r["scope"] == "reviewer_calibration" for r in s.learnings_for_role("factory"))
    s.close()


def test_no_reviewer_miss_when_approved_candidate_merges(tmp_path):
    """Slice 1: an APPROVED candidate that MERGES cleanly is not a miss — only auto_reverted is."""
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1000)
    s.add_task("task-1", "do x", source="issue")
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    dev_fn = lambda text, **k: {"action": "merged", "merge_sha": "m",
                                "review": {"approved": True, "reason": "lgtm"}}
    develop.execute_claimed_tasks(s, sh, develop_fn=dev_fn)
    assert s.get_task("task-1")["status"] == "done"
    assert not any(r["scope"] == "reviewer_calibration" for r in s.learnings_for_role("factory"))
    s.close()


def test_scope_miss_recorded_on_no_candidate_with_spec(tmp_path):
    """Slice 2: a no_candidate whose task carries a spec the scope check PASSED (attached) scores
    a scope='scope_check' calibration learning — the judge under-rejected a brief too big to land."""
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1000)
    s.add_task("task-1", "do x", source="issue")
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    scope = lambda t: {"decision": "pass",
                       "spec": {"target_surface": "llm.py", "acceptance": "a test"}}
    dev_fn = lambda text, **k: {"action": "no_candidate"}
    develop.execute_claimed_tasks(s, sh, develop_fn=dev_fn, scope_judge=scope)
    assert s.get_task("task-1")["status"] == "blocked"
    miss = [r for r in s.learnings_for_role("factory")
            if r["scope"] == "scope_check" and "no_candidate" in r["content"]]
    assert len(miss) == 1
    s.close()


def test_no_scope_miss_on_no_candidate_without_spec(tmp_path):
    """Slice 2: a no_candidate with NO spec (nothing proves the scope check passed it) records
    no scope_check calibration learning — only a passed-spec brief scores the miss."""
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1000)
    s.add_task("task-1", "do x", source="issue")
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    dev_fn = lambda text, **k: {"action": "no_candidate"}
    develop.execute_claimed_tasks(s, sh, develop_fn=dev_fn)
    assert not any(r["scope"] == "scope_check" for r in s.learnings_for_role("factory"))
    s.close()


# ============================================================================
# Task 3.1: execute the spec's named acceptance test (stage='acceptance').
#   - develop_and_merge threads acceptance_ref into run_code_round (adapter seam);
#   - execute_claimed_tasks extracts the ref ON THE MAIN THREAD from each task's
#     spec (only when the acceptance_exec gate is on), surfaces it as a HARD CONTRACT
#     line in the brief, and counts acceptance_skipped as telemetry at close-out.
# ============================================================================

def test_develop_and_merge_runs_acceptance_ref(monkeypatch, tmp_path):
    """acceptance_ref threads through develop_and_merge → run_code_round → adapter.run_named_test."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate",
                        lambda clone_dir, **k: {"branch": k["branch"], "reply": "did it"})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="do x",
                                    champion_scores=CHAMP, grade_fn=_good_grade,
                                    acceptance_ref="tests/test_x.py::test_it")
    assert res["action"] == "merged"
    assert ("named", "tests/test_x.py::test_it") in ad.calls


def test_develop_and_merge_acceptance_red_discards(monkeypatch, tmp_path):
    """A red named acceptance test discards the candidate at stage 'acceptance' (never merges)."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    ad._named = ("failed", "E assert False")
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="do x",
                                    champion_scores=CHAMP, grade_fn=_good_grade,
                                    acceptance_ref="tests/test_x.py::test_it")
    assert res["action"] == "discarded" and res["stage"] == "acceptance"
    assert "assert False" in res["tests_report"]
    assert "merge" not in [c[0] for c in ad.calls]


def test_develop_and_merge_no_ref_never_runs_named_test(monkeypatch, tmp_path):
    """No acceptance_ref (gate off / prose spec) → run_named_test is never invoked (today's path)."""
    ad = FakeAdapter(changed=["src/x.py", "tests/test_x.py"], tests_passed=True)
    monkeypatch.setattr(common, "develop_candidate", lambda clone_dir, **k: {"branch": k["branch"]})
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"), task="do x",
                                    champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "merged" and "named" not in [c[0] for c in ad.calls]


def _acc_store(tmp_path, spec):
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=1000)
    s.add_task("task-1", "do x", source="issue")
    if spec is not None:
        s.set_task_spec("task-1", spec)
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    return s, sh


def test_execute_extracts_ref_and_adds_contract_when_gate_on(tmp_path):
    """Correction (a): with acceptance_exec ON, the rail extracts the spec's ref on the MAIN
    THREAD, threads it as acceptance_ref, AND surfaces it in the brief as a hard contract line."""
    s, sh = _acc_store(tmp_path, {"target_surface": "llm.py",
                                  "acceptance": "prove: tests/test_x.py::test_it"})
    seen = {}

    def fake(text, **k):
        seen["text"] = text
        seen["k"] = dict(k)
        return {"action": "merged", "merge_sha": "z"}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake, acceptance_exec=True)
    assert seen["k"]["acceptance_ref"] == "tests/test_x.py::test_it"
    assert "ACCEPTANCE CONTRACT" in seen["text"] and "tests/test_x.py::test_it" in seen["text"]
    s.close()


def test_execute_no_ref_when_gate_off(tmp_path):
    """Gate OFF (default): no ref is extracted or threaded, no contract line — byte-for-byte today."""
    s, sh = _acc_store(tmp_path, {"target_surface": "llm.py",
                                  "acceptance": "tests/test_x.py::test_it"})
    seen = {}

    def fake(text, **k):
        seen["text"] = text
        seen["k"] = dict(k)
        return {"action": "merged", "merge_sha": "z"}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake)      # acceptance_exec defaults off
    assert seen["k"].get("acceptance_ref") is None
    assert "ACCEPTANCE CONTRACT" not in seen["text"]
    s.close()


def test_execute_prose_acceptance_yields_no_ref(tmp_path):
    """Gate ON but the spec's acceptance is prose (no runnable ref) → acceptance_ref None, no
    contract line — fail-open (the gate simply doesn't run)."""
    s, sh = _acc_store(tmp_path, {"target_surface": "llm.py", "acceptance": "a retry test passes"})
    seen = {}

    def fake(text, **k):
        seen["text"] = text
        seen["k"] = dict(k)
        return {"action": "merged", "merge_sha": "z"}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake, acceptance_exec=True)
    assert seen["k"].get("acceptance_ref") is None
    assert "ACCEPTANCE CONTRACT" not in seen["text"]
    s.close()


def test_execute_counts_acceptance_skipped_telemetry(tmp_path):
    """Correction (b): a candidate whose result carries acceptance_skipped records a per-shift
    'acceptance' factory learning (telemetry-first) — recurrence is counted via the hits column."""
    s, sh = _acc_store(tmp_path, {"target_surface": "llm.py",
                                  "acceptance": "tests/test_x.py::test_it"})
    dev_fn = lambda text, **k: {"action": "merged", "merge_sha": "z",
                                "acceptance_skipped": "tests/test_x.py::test_it"}
    develop.execute_claimed_tasks(s, sh, develop_fn=dev_fn, acceptance_exec=True)
    skips = [r for r in s.learnings_for_role("factory") if r["scope"] == "acceptance"]
    assert len(skips) == 1
    assert s.get_task("task-1")["status"] == "done"      # skipped != discarded — it still merged
    s.close()


def test_acceptance_discard_gets_stage_lesson(tmp_path):
    """A discarded(acceptance) result blocks the task with the acceptance-specific canned lesson."""
    from factory.reporting import factory_memory
    s, sh = _acc_store(tmp_path, None)
    dev_fn = lambda text, **k: {"action": "discarded", "stage": "acceptance",
                                "tests_report": "E assert False"}
    develop.execute_claimed_tasks(s, sh, develop_fn=dev_fn, acceptance_exec=True)
    assert s.get_task("task-1")["status"] == "blocked"
    assert factory_memory.lesson_for_block("discarded", "acceptance") is not None
    lessons = [r["content"] for r in s.learnings_for_role("factory") if r["scope"] == "blocked"]
    assert any("acceptance" in c.lower() for c in lessons)
    s.close()


# ============================================================================
# Task 3.2: one informed retry on a gradeable gate-discard (maker→grader→retry).
#   Gate super_worker.retry_on_discard (default OFF, board-toggleable). Attempt 1
#   discarded at stage in {tests,no_test,acceptance} (NEVER frozen) → retry EXACTLY
#   once with the failure evidence appended, worded honestly (a prior INDEPENDENT
#   attempt, a CLEAN base — the prior code is NOT visible). retry_budget_ok is computed
#   on the MAIN THREAD (shift spend vs the shift's token_budget); tokens/cost/seconds
#   sum into the SINGLE ledger write.
# ============================================================================

def _retry_store(tmp_path, *, budget=1000, on=True, pre_spend=0):
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=budget)
    if on:
        s.set_setting("super_worker.retry_on_discard", "true")   # board toggle → store override
    if pre_spend:
        s.add_budget("conductor", pre_spend, shift_id=sh)        # prior shift spend (pre-dispatch)
    s.add_task("task-1", "big brief", source="issue")
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    return s, sh


def test_retry_off_by_default_never_retries(tmp_path):
    """Gate OFF (default): a discarded(tests) result is NOT retried — one dispatch, blocked."""
    s, sh = _retry_store(tmp_path, on=False)
    calls = []

    def fake(text, **k):
        calls.append(text)
        return {"action": "discarded", "stage": "tests", "tests_report": "E assert False"}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    assert len(calls) == 1                                   # no retry when the gate is off
    assert s.get_task("task-1")["status"] == "blocked"
    s.close()


def test_retry_on_discard_retries_once_and_can_merge(tmp_path):
    """Gate ON + budget headroom: attempt 1 discarded(tests), attempt 2 merges → done; the
    worker is dispatched EXACTLY twice (the retry is one-shot, never a loop)."""
    s, sh = _retry_store(tmp_path)
    calls = []

    def fake(text, **k):
        calls.append(text)
        if len(calls) == 1:
            return {"action": "discarded", "stage": "tests", "tests_report": "E assert 1 == 2"}
        return {"action": "merged", "merge_sha": "sha2"}

    shipped = develop.execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    assert shipped == 1 and len(calls) == 2
    assert s.get_task("task-1")["status"] == "done" and s.get_task("task-1")["result"] == "sha2"
    s.close()


def test_retry_brief_is_honest_about_the_clean_base(tmp_path):
    """The retry suffix is worded honestly: an INDEPENDENT prior attempt, its stage + evidence,
    and a CLEAN base (per operator memory — never imply the prior code is visible)."""
    s, sh = _retry_store(tmp_path)
    calls = []

    def fake(text, **k):
        calls.append(text)
        if len(calls) == 1:
            return {"action": "discarded", "stage": "acceptance",
                    "tests_report": "E   assert retry_ran is True"}
        return {"action": "merged", "merge_sha": "s"}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    retry_brief = calls[1]
    assert "independent attempt" in retry_brief.lower()
    assert "stage=acceptance" in retry_brief
    assert "clean base" in retry_brief.lower()
    assert "assert retry_ran is True" in retry_brief          # the failure evidence rides along
    s.close()


def test_retry_never_fires_for_frozen(tmp_path):
    """A frozen-surface discard is a structural safety veto — NEVER retried (a retry can only
    re-violate it), even with the gate ON."""
    s, sh = _retry_store(tmp_path)
    calls = []

    def fake(text, **k):
        calls.append(text)
        return {"action": "discarded", "stage": "frozen", "violations": ["selfmod/gate.py"]}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    assert len(calls) == 1                                     # frozen is never retried
    assert s.get_task("task-1")["status"] == "blocked"
    s.close()


def test_retry_suppressed_when_budget_exhausted(tmp_path):
    """Brake-honest: retry_budget_ok is computed on the MAIN THREAD from the shift's ledgered
    spend vs its token_budget — once spend has reached budget, no retry (composes with Task 0.2)."""
    s, sh = _retry_store(tmp_path, budget=100, pre_spend=200)   # already over budget at dispatch
    calls = []

    def fake(text, **k):
        calls.append(text)
        return {"action": "discarded", "stage": "tests", "tests_report": "red"}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    assert len(calls) == 1                                       # no second attempt over budget
    assert s.get_task("task-1")["status"] == "blocked"
    s.close()


def test_retry_sums_spend_into_one_ledger_write(tmp_path):
    """Both attempts' tokens/cost/seconds sum into the SINGLE developer ledger row for the task."""
    s, sh = _retry_store(tmp_path)
    calls = []

    def fake(text, **k):
        calls.append(text)
        if len(calls) == 1:
            return {"action": "discarded", "stage": "tests", "tests_report": "red",
                    "tokens": 300, "cost": 0.01, "seconds": 2.0}
        return {"action": "merged", "merge_sha": "s", "tokens": 500, "cost": 0.02, "seconds": 3.0}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    rows = [e for e in s.budget_entries() if e["role_or_run"] == "developer:task-1"]
    assert len(rows) == 1                                        # ONE ledger write, not two
    assert rows[0]["tokens"] == 800 and abs(rows[0]["cost"] - 0.03) < 1e-9
    assert rows[0]["seconds"] == 5.0
    s.close()


def test_retry_no_test_stage_is_retry_eligible(tmp_path):
    """A no_test discard (source changed, no test shipped) is gradeable → retry-eligible."""
    s, sh = _retry_store(tmp_path)
    calls = []

    def fake(text, **k):
        calls.append(text)
        if len(calls) == 1:
            return {"action": "discarded", "stage": "no_test", "why": "changed src, no test"}
        return {"action": "merged", "merge_sha": "s"}

    develop.execute_claimed_tasks(s, sh, develop_fn=fake, max_parallel=1)
    assert len(calls) == 2 and s.get_task("task-1")["status"] == "done"
    s.close()


def test_retry_on_discard_is_board_toggleable():
    """The gate is an operator DIAL (a trial gate, not a brake) → it belongs in SETTINGS_SPEC
    and ships OFF in config.yaml."""
    from factory.common.config import SETTINGS_SPEC, load_config
    assert SETTINGS_SPEC.get("super_worker.retry_on_discard") is bool
    assert (load_config().get("super_worker") or {}).get("retry_on_discard") is False


# ============================================================================
# Task 5.2 — Bounded second-wave dispatch (same-shift loop-until-dry, 2 waves max).
#   Gate super_worker.dispatch_waves (1 default = today; 2 = one more pass). AFTER close-out,
#   if any no_candidate DECOMPOSED this shift AND STOP clear AND headroom under max_tasks AND
#   shift spend < token_budget AND an EXPLICIT TIME GUARD fits (elapsed + waves×1800s ≤ the
#   loop-deadline share — the executor has NO wall-clock, only per-worker 1800s timeouts, and
#   the loop deadline is checked only BETWEEN shifts), claim the new sub-task ids and run ONE
#   more identical pass with decomposer=None (HARD recursion stop, no wave 3). Wave-2 sub-tasks
#   inherit the parent's milestone_id so EVM/timesheet attribution survives the rail claiming
#   the tasks itself. Depends HARD on Task 0.2's enforced shift budget.
# ============================================================================

def _wave_store(tmp_path, *, waves=2, budget=1_000_000):
    from factory.common.store import Blackboard
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    sh = s.start_shift(token_budget=budget)
    if waves is not None:
        s.set_setting("super_worker.dispatch_waves", str(waves))   # board toggle → store override
    s.add_task("task-1", "big brief", source="human")
    s.set_task_status("task-1", "in_progress", shift_id=sh)
    return s, sh


def test_second_wave_dispatches_decomposed_subtasks(tmp_path):
    """dispatch_waves=2 + time/budget headroom: a no_candidate decomposed into 2 sub-tasks →
    the rail CLAIMS them and runs a second wave in the SAME shift; both ship."""
    import time
    s, sh = _wave_store(tmp_path)
    seen, dec_calls = [], []

    def dev_fn(text, **k):
        seen.append(text)
        return {"action": "no_candidate"} if "big brief" in text else {"action": "merged", "merge_sha": "s"}

    def dec(task):
        dec_calls.append(task["id"])
        return {"subtasks": [{"title": "slice one"}, {"title": "slice two"}]}

    shipped = develop.execute_claimed_tasks(
        s, sh, develop_fn=dev_fn, decomposer=dec, max_parallel=1,
        shift_started=time.monotonic(), loop_deadline_s=100_000)

    assert any("slice one" in t for t in seen) and any("slice two" in t for t in seen)
    assert shipped == 2                                   # both wave-2 sub-tasks merged
    subs = [t for t in s.list_tasks() if t["title"] in ("slice one", "slice two")]
    assert len(subs) == 2 and all(t["status"] == "done" for t in subs)
    assert dec_calls == ["task-1"]                        # decomposer ran ONLY in wave 1
    s.close()


def test_second_wave_runs_with_no_decomposer_hard_stop(tmp_path):
    """Recursion HARD stop: the second wave runs with decomposer=None — a wave-2 sub-task that
    itself returns no_candidate is NOT decomposed again (no wave 3)."""
    import time
    s, sh = _wave_store(tmp_path)
    dec_calls = []

    def dev_fn(text, **k):
        return {"action": "no_candidate"}                 # EVERY task comes back empty-handed

    def dec(task):
        dec_calls.append(task["id"])
        return {"subtasks": [{"title": "slice one"}]}

    develop.execute_claimed_tasks(
        s, sh, develop_fn=dev_fn, decomposer=dec, max_parallel=1,
        shift_started=time.monotonic(), loop_deadline_s=100_000)

    assert dec_calls == ["task-1"]                        # decomposer NEVER runs in wave 2
    sub = [t for t in s.list_tasks() if t["title"] == "slice one"][0]
    assert sub["status"] == "blocked"                     # wave-2 sub-task blocked, not re-split
    assert len(s.list_tasks()) == 2                       # parent + 1 sub-task; no wave-3 spawn
    s.close()


def test_dispatch_waves_default_one_no_second_wave(tmp_path):
    """Default (dispatch_waves=1): the no_candidate is still decomposed, but the sub-task is NOT
    claimed/dispatched — it sits open for a future shift (today's behavior)."""
    import time
    s, sh = _wave_store(tmp_path, waves=None)              # no override → config default (1)
    seen = []

    def dev_fn(text, **k):
        seen.append(text)
        return {"action": "no_candidate"}

    develop.execute_claimed_tasks(
        s, sh, develop_fn=dev_fn, decomposer=lambda t: {"subtasks": [{"title": "slice one"}]},
        max_parallel=1, shift_started=time.monotonic(), loop_deadline_s=100_000)

    assert seen == ["big brief"]                           # only the wave-1 parent dispatched
    assert [t for t in s.list_tasks() if t["title"] == "slice one"][0]["status"] == "open"
    s.close()


def test_second_wave_skipped_without_time_guard(tmp_path):
    """The EXPLICIT time guard fails CLOSED: with no loop_deadline_s threaded in, wave 2 never
    runs (the executor must not silently overrun a deadline it can't see)."""
    import time
    s, sh = _wave_store(tmp_path)
    seen = []

    def dev_fn(text, **k):
        seen.append(text)
        return {"action": "no_candidate"}

    develop.execute_claimed_tasks(
        s, sh, develop_fn=dev_fn, decomposer=lambda t: {"subtasks": [{"title": "slice one"}]},
        max_parallel=1, shift_started=time.monotonic(), loop_deadline_s=None)

    assert seen == ["big brief"]
    assert [t for t in s.list_tasks() if t["title"] == "slice one"][0]["status"] == "open"
    s.close()


def test_second_wave_skipped_when_deadline_headroom_insufficient(tmp_path):
    """Time guard math: elapsed + waves×1800 must fit the loop-deadline share, else skip. Here
    waves=2 reserves 3600s and the deadline is 3600s with 100s already elapsed → 3700 > 3600."""
    import time
    s, sh = _wave_store(tmp_path)
    seen = []

    def dev_fn(text, **k):
        seen.append(text)
        return {"action": "no_candidate"}

    develop.execute_claimed_tasks(
        s, sh, develop_fn=dev_fn, decomposer=lambda t: {"subtasks": [{"title": "slice one"}]},
        max_parallel=1, shift_started=time.monotonic() - 100, loop_deadline_s=3600)

    assert seen == ["big brief"]
    assert [t for t in s.list_tasks() if t["title"] == "slice one"][0]["status"] == "open"
    s.close()


def test_second_wave_skipped_when_budget_exhausted(tmp_path):
    """Brake-honest (composes with Task 0.2): once the shift's ledgered spend has reached its
    token_budget, no second wave — the same guard that gates the retry."""
    import time
    s, sh = _wave_store(tmp_path, budget=100)
    s.add_budget("conductor", 200, shift_id=sh)           # already over budget before wave 2
    seen = []

    def dev_fn(text, **k):
        seen.append(text)
        return {"action": "no_candidate"}

    develop.execute_claimed_tasks(
        s, sh, develop_fn=dev_fn, decomposer=lambda t: {"subtasks": [{"title": "slice one"}]},
        max_parallel=1, shift_started=time.monotonic(), loop_deadline_s=100_000)

    assert seen == ["big brief"]
    assert [t for t in s.list_tasks() if t["title"] == "slice one"][0]["status"] == "open"
    s.close()


def test_second_wave_vetoed_by_stop(tmp_path):
    """STOP vetoes everything, including the second wave — tripped on the MAIN thread during
    close-out (the decomposer engages it), the wave-2 block re-checks and skips."""
    import time
    from factory.common import killswitch
    s, sh = _wave_store(tmp_path)
    seen = []

    def dev_fn(text, **k):
        seen.append(text)
        return {"action": "no_candidate"}

    def dec(task):
        killswitch.engage("test-stop")                    # trip STOP mid close-out (main thread)
        return {"subtasks": [{"title": "slice one"}]}

    try:
        develop.execute_claimed_tasks(
            s, sh, develop_fn=dev_fn, decomposer=dec, max_parallel=1,
            shift_started=time.monotonic(), loop_deadline_s=100_000)
        assert seen == ["big brief"]                       # wave 1 ran; wave 2 vetoed by STOP
        assert [t for t in s.list_tasks() if t["title"] == "slice one"][0]["status"] == "open"
    finally:
        killswitch.release()
    s.close()


def test_second_wave_respects_max_tasks_headroom(tmp_path):
    """Headroom under max_tasks_per_shift: wave 1 used 1 of max_tasks=2 slots, so wave 2 may
    claim only 1 of the 3 decomposed sub-tasks; the other two stay open."""
    import time
    s, sh = _wave_store(tmp_path)
    seen = []

    def dev_fn(text, **k):
        seen.append(text)
        return {"action": "no_candidate"} if "big brief" in text else {"action": "merged", "merge_sha": "s"}

    def dec(task):
        return {"subtasks": [{"title": "sub a"}, {"title": "sub b"}, {"title": "sub c"}]}

    develop.execute_claimed_tasks(
        s, sh, develop_fn=dev_fn, decomposer=dec, max_parallel=1, max_tasks=2,
        shift_started=time.monotonic(), loop_deadline_s=100_000)

    done = [t for t in s.list_tasks() if t["status"] == "done"]
    opened = [t for t in s.list_tasks() if t["title"] in ("sub a", "sub b", "sub c") and t["status"] == "open"]
    assert len(done) == 1 and len(opened) == 2             # only ONE sub-task fit the wave-2 headroom
    s.close()


def test_second_wave_subtasks_inherit_parent_milestone(tmp_path):
    """Plan-link: wave-2 sub-tasks inherit the parent's milestone_id so EVM/timesheet attribution
    survives the rail claiming the tasks itself."""
    import time
    s, sh = _wave_store(tmp_path)
    mid = s.add_milestone("M1")
    s.set_task_milestone("task-1", mid)

    def dev_fn(text, **k):
        return {"action": "no_candidate"} if "big brief" in text else {"action": "merged", "merge_sha": "s"}

    develop.execute_claimed_tasks(
        s, sh, develop_fn=dev_fn, decomposer=lambda t: {"subtasks": [{"title": "slice one"}]},
        max_parallel=1, shift_started=time.monotonic(), loop_deadline_s=100_000)

    sub = [t for t in s.list_tasks() if t["title"] == "slice one"][0]
    assert sub["milestone_id"] == mid                      # inherited the parent's plan link
    s.close()


def test_dispatch_waves_is_board_toggleable():
    """The gate is an operator DIAL (a trial gate, not a brake) → it belongs in SETTINGS_SPEC as
    an int and ships at 1 (= today) in config.yaml."""
    from factory.common.config import SETTINGS_SPEC, load_config
    assert SETTINGS_SPEC.get("super_worker.dispatch_waves") is int
    assert (load_config().get("super_worker") or {}).get("dispatch_waves") == 1
