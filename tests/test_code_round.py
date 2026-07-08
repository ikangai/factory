"""The code-candidate ROUND — the orchestration that wires the full-auto cores into one
flow (design: docs/plans/2026-06-25-autonomous-code-factory.md):

  kill-switch → frozen-check → tests → scenario-eval → auto-merge gate →
  merge-or-discard → re-baseline → auto-revert self-heal

Live execution (git + the target's tests + the scenario eval) is INJECTED via a fake
adapter and a `grade_fn`, so the DECISION FLOW is tested without running the target.
"""
from factory.common import killswitch
from factory.orchestrator import code_round


class FakeAdapter:
    def __init__(self, *, frozen=(), tests_passed=True, merge_raises=False):
        self._frozen = list(frozen)
        self._tests_passed = tests_passed
        self._merge_raises = merge_raises
        self.calls = []
        self.merge_messages = []

    def frozen_paths(self):
        return self._frozen

    def run_tests(self, repo, **k):
        self.calls.append("run_tests")
        return (self._tests_passed, "report")

    def merge_branch(self, repo, branch, message=None, **k):
        self.calls.append(("merge", branch))
        self.merge_messages.append(message)
        if self._merge_raises:
            raise RuntimeError("merge conflict (aborted)")
        return "MERGESHA"

    def revert_commit(self, repo, sha):
        self.calls.append(("revert", sha))
        return "REVERTSHA"

    def current_commit(self, repo):
        return "HEAD"


def _grade(*values):
    it = iter(values)
    return lambda repo: next(it)


def g(working, held_out=0.7):
    """A fully-graded candidate result — supplies the fail-closed signals the gate
    now requires (held_out_measured + the divergence/safety flags)."""
    return {"working": working, "held_out": held_out, "held_out_measured": True,
            "divergence_alarm": False, "safety_flag": False}


CHAMP = {"working": 0.8, "held_out": 0.7}
CLEAN_DIFF = ("diff --git a/src/clive/feature.py b/src/clive/feature.py\n"
              "--- a/src/clive/feature.py\n+++ b/src/clive/feature.py\n")


def _run(ad, grade_fn, diff=CLEAN_DIFF):
    return code_round.run_code_round(adapter=ad, main_repo="/main", cand_repo="/cand",
                                     branch="cand", diff_text=diff,
                                     champion_scores=CHAMP, grade_fn=grade_fn, label="cand")


def test_merges_when_all_gates_pass():
    ad = FakeAdapter(tests_passed=True)
    res = _run(ad, _grade(g(0.85), g(0.85)))  # re-baseline (no regression)
    assert res["action"] == "merged" and res["merge_sha"] == "MERGESHA"
    assert ("merge", "cand") in ad.calls


def test_discards_on_frozen_violation_before_grading():
    ad = FakeAdapter(frozen=["src/clive/selfmod/"])
    bad = ("diff --git a/src/clive/selfmod/gate.py b/src/clive/selfmod/gate.py\n"
           "--- a/src/clive/selfmod/gate.py\n+++ b/src/clive/selfmod/gate.py\n")
    res = _run(ad, _grade(g(1.0)), diff=bad)
    assert res["action"] == "discarded" and res["stage"] == "frozen"
    assert "src/clive/selfmod/gate.py" in res["violations"]
    assert "run_tests" not in ad.calls           # frozen check short-circuits BEFORE grading


def test_discards_on_red_tests_without_scenario_eval():
    ad = FakeAdapter(tests_passed=False)
    res = _run(ad, _grade())   # grade_fn must NOT be called when tests are red
    assert res["action"] == "discarded" and "tests_passed" in res["failed"]
    assert not any(isinstance(c, tuple) and c[0] == "merge" for c in ad.calls)


def test_discards_on_scenario_regression():
    ad = FakeAdapter(tests_passed=True)
    res = _run(ad, _grade({"working": 0.5, "held_out": 0.7}))    # worse than champion
    assert res["action"] == "discarded" and "no_working_regression" in res["failed"]


def test_auto_reverts_on_post_merge_regression():
    ad = FakeAdapter(tests_passed=True)
    res = _run(ad, _grade(g(0.85), g(0.6)))    # re-baseline reveals a regression
    assert res["action"] == "auto_reverted"
    assert ("revert", "MERGESHA") in ad.calls and res["revert_sha"] == "REVERTSHA"


def test_halted_kill_switch_aborts(monkeypatch):
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    ad = FakeAdapter()
    res = _run(ad, _grade(g(1.0)))
    assert res["action"] == "halted"
    assert ad.calls == []   # nothing touched while halted


def test_kill_switch_dropped_during_grading_blocks_the_merge(monkeypatch):
    """The brake is re-checked right before the merge: a STOP dropped while grading
    must NOT result in a merge."""
    state = {"n": 0}

    def flip():
        state["n"] += 1
        return state["n"] >= 2   # not halted at round start; halted by the pre-merge check

    monkeypatch.setattr(killswitch, "is_halted", flip)
    ad = FakeAdapter(tests_passed=True)
    res = _run(ad, _grade(g(0.85)))
    assert res["action"] == "halted" and res.get("stage") == "pre_merge"
    assert not any(isinstance(c, tuple) and c[0] == "merge" for c in ad.calls)


def test_merge_failure_is_a_clean_discard_not_a_crash():
    ad = FakeAdapter(tests_passed=True, merge_raises=True)
    res = _run(ad, _grade(g(0.85)))      # gate passes, merge raises (conflict)
    assert res["action"] == "discarded" and res["stage"] == "merge"   # not an exception


def test_post_merge_grade_failure_triggers_auto_revert():
    """If the re-baseline raises AFTER the merge, the merge must be auto-reverted —
    never leave an ungraded merge in the repo."""
    state = {"n": 0}

    def grade(repo):
        state["n"] += 1
        if state["n"] == 1:
            return g(0.85)            # candidate grade → gate passes → merge
        raise RuntimeError("re-baseline boom")

    ad = FakeAdapter(tests_passed=True)
    res = code_round.run_code_round(adapter=ad, main_repo="/main", cand_repo="/cand",
                                    branch="cand", diff_text=CLEAN_DIFF,
                                    champion_scores=CHAMP, grade_fn=grade, label="cand")
    assert res["action"] == "auto_reverted"
    assert ("revert", "MERGESHA") in ad.calls


def test_merge_message_carries_task_trailer():
    """Blindspot fix (2026-07-07): the sha→task chain must survive WITHOUT the
    blackboard — a task_ref rides into the merge commit as a Factory-Task trailer."""
    ad = FakeAdapter(tests_passed=True)
    res = code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo="/cand", branch="factory/cand-ab12cd34",
        diff_text=CLEAN_DIFF, champion_scores=CHAMP, grade_fn=_grade(g(0.85), g(0.85)),
        label="factory/cand-ab12cd34",
        task_ref="task-d242f07a: guard KeyError in execute_plan")
    assert res["action"] == "merged"
    assert ad.merge_messages == [
        "factory: factory/cand-ab12cd34\n\n"
        "Factory-Task: task-d242f07a: guard KeyError in execute_plan"
    ]


def test_merge_message_unchanged_without_task_ref():
    ad = FakeAdapter(tests_passed=True)
    res = code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo="/cand", branch="factory/cand-ab12cd34",
        diff_text=CLEAN_DIFF, champion_scores=CHAMP, grade_fn=_grade(g(0.85), g(0.85)),
        label="factory/cand-ab12cd34")
    assert res["action"] == "merged"
    assert ad.merge_messages == ["factory: factory/cand-ab12cd34"]


def test_merge_message_task_ref_cannot_forge_a_second_trailer():
    """63035a2 review (Critical 1): task titles are free/LLM-authored text — an embedded
    newline block ("\\n\\nFactory-Task: …") must NOT become a second, fabricated trailer
    that git interpret-trailers would resolve. The ref is sanitized to ONE printable line."""
    ad = FakeAdapter(tests_passed=True)
    res = code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo="/cand", branch="factory/cand-ab12cd34",
        diff_text=CLEAN_DIFF, champion_scores=CHAMP, grade_fn=_grade(g(0.85), g(0.85)),
        label="factory/cand-ab12cd34",
        task_ref="task-real123: evil\n\nFactory-Task: task-fake999: forged")
    assert res["action"] == "merged"
    msg = ad.merge_messages[0]
    trailer_lines = [l for l in msg.splitlines() if l.startswith("Factory-Task:")]
    assert len(trailer_lines) == 1                       # EXACTLY ONE trailer line
    assert msg.splitlines()[-1] == trailer_lines[0]      # …and it is the LAST line
    assert "\n" not in msg.split("Factory-Task:", 1)[1]  # single-line value — no forged block
    # the injected text survives only as inert inline words INSIDE the one trailer's value
    assert "task-fake999: forged" in trailer_lines[0]
    assert msg.startswith("factory: factory/cand-ab12cd34\n\nFactory-Task: task-real123:")
