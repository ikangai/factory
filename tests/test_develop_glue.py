"""The develop→grade→auto-merge glue (orchestrator/develop.py): one full turn of the
autonomous code loop. Hermetic — a fake adapter (no real git) + an injected
develop_candidate + grade_fn exercise the WHOLE chain (clone → develop → fetch →
worktree → run_code_round → cleanup) without a live worker or real repos.
"""
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


def test_halted_kill_switch(monkeypatch, tmp_path):
    monkeypatch.setattr(develop.killswitch, "is_halted", lambda: True)
    ad = FakeAdapter()
    res = develop.develop_and_merge(adapter=ad, main_repo=str(tmp_path / "m"),
                                    task="t", champion_scores=CHAMP, grade_fn=_good_grade)
    assert res["action"] == "halted" and ad.calls == []
