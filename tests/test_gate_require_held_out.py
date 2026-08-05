"""common/code_gate.py's `require_held_out` knob + orchestrator/code_round.py's own
default for it (adversarial-review fix round, 2026-08-05, item 12 — the follow-up to
056f96f "feat(gate): scope held-out to rebaseline; enable the real merge grade", which
landed on this branch mid-flight from a separate process).

056f96f's OWN fix (auto_merge_eligible gains `require_held_out`, default True — "every
OTHER caller stays exactly as strict") was correct, but `run_code_round`'s OWN parameter
kept a `require_held_out: bool = False` default — the ONE real production caller
(orchestrator/develop.py) relied on that default silently, rather than opting into the
per-merge held-out scope-out at its own call site. This file proves the restored
fail-closed default (`run_code_round`'s own default is now `True`, matching
`auto_merge_eligible`'s) and the explicit opt-out `orchestrator/develop.py` now states.

New file (a `git grep require_held_out tests/` before this fix found ZERO existing
references — no prior test exercised this knob at all)."""
from factory.common import code_gate
from factory.orchestrator import code_round


# =========================================================================================
# common/code_gate.auto_merge_eligible — the `require_held_out` knob itself
# =========================================================================================
def test_auto_merge_eligible_require_held_out_false_and_unmeasured_is_still_eligible():
    """The bug 056f96f fixed: grade.mode: smoke honestly reports held_out_measured=False
    (it does not sample the held-out set, by design — that's factory rebaseline's job).
    With require_held_out=False, a flawless candidate must be ELIGIBLE despite that."""
    verdict = code_gate.auto_merge_eligible(
        tests_passed=True, frozen_ok=True, working_delta=0.01,
        held_out_delta=0.0, held_out_measured=False,
        divergence_alarm=False, safety_flag=False,
        require_held_out=False)
    assert verdict["eligible"] is True
    assert verdict["checks"]["held_out_measured"] is True   # vacuously satisfied
    assert not verdict["failed"]


def test_auto_merge_eligible_require_held_out_false_still_blocks_a_measured_regression():
    """require_held_out=False scopes OUT the "must be measured" demand — it must NEVER
    scope out an ACTUAL regression a caller DID measure and report. no_held_out_regression
    stays live regardless of require_held_out (only held_out_measured's OWN vacuous-pass
    behavior is conditioned on it)."""
    verdict = code_gate.auto_merge_eligible(
        tests_passed=True, frozen_ok=True, working_delta=0.01,
        held_out_delta=-0.5, held_out_measured=True,   # a REAL, measured regression
        divergence_alarm=False, safety_flag=False,
        require_held_out=False)
    assert verdict["eligible"] is False
    assert "no_held_out_regression" in verdict["failed"]


def test_auto_merge_eligible_require_held_out_true_blocks_an_unmeasured_candidate():
    """The pre-056f96f, still-default-for-every-OTHER-caller posture: an unmeasured
    held-out sample fails closed."""
    verdict = code_gate.auto_merge_eligible(
        tests_passed=True, frozen_ok=True, working_delta=0.01,
        held_out_delta=0.0, held_out_measured=False,
        divergence_alarm=False, safety_flag=False,
        require_held_out=True)
    assert verdict["eligible"] is False
    assert "held_out_measured" in verdict["failed"]


def test_auto_merge_eligible_require_held_out_defaults_true():
    """auto_merge_eligible's OWN default was never touched by this fix round (056f96f
    already got it right) — reconfirm it stays fail-closed by default."""
    verdict = code_gate.auto_merge_eligible(
        tests_passed=True, frozen_ok=True, working_delta=0.01,
        held_out_delta=0.0, held_out_measured=False,
        divergence_alarm=False, safety_flag=False)
    assert verdict["eligible"] is False
    assert "held_out_measured" in verdict["failed"]


# =========================================================================================
# orchestrator/code_round.run_code_round — its OWN require_held_out default (THIS fix)
# =========================================================================================
class _FakeAdapter:
    def __init__(self, *, frozen=(), tests_passed=True):
        self._frozen = list(frozen)
        self._tests_passed = tests_passed
        self.calls = []

    def frozen_paths(self):
        return self._frozen

    def run_tests(self, repo, **k):
        self.calls.append("run_tests")
        return (self._tests_passed, "report")

    def merge_branch(self, repo, branch, message=None, **k):
        self.calls.append(("merge", branch))
        return "MERGESHA"

    def revert_commit(self, repo, sha):
        self.calls.append(("revert", sha))
        return "REVERTSHA"


_CHAMP = {"working": 0.8, "held_out": 0.7}
_DIFF = ("diff --git a/src/clive/feature.py b/src/clive/feature.py\n"
        "--- a/src/clive/feature.py\n+++ b/src/clive/feature.py\n")


def _unmeasured_grade(working=0.85):
    """A grade_fn whose result honestly reports held_out_measured=False (the smoke-mode
    shape 056f96f's own commit message describes) — used for BOTH the candidate grade
    and the re-baseline grade, so a 'merged' outcome doesn't trip on the re-baseline call."""
    def grade(repo):
        return {"working": working, "held_out": 0.0, "held_out_measured": False,
               "divergence_alarm": False, "safety_flag": False}
    return grade


def test_run_code_round_default_demands_held_out_measured_and_discards_when_unmeasured():
    """THIS fix: run_code_round's OWN default flipped from False to True — a caller that
    does NOT pass require_held_out (the historical default every test/caller relied on
    implicitly) now gets the FAIL-CLOSED behavior, matching auto_merge_eligible's own
    default. An honestly-unmeasured grade_fn (no require_held_out kwarg at all) discards."""
    ad = _FakeAdapter(tests_passed=True)
    res = code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo="/cand", branch="cand",
        diff_text=_DIFF, champion_scores=_CHAMP, grade_fn=_unmeasured_grade(), label="cand")
    assert res["action"] == "discarded" and res["stage"] == "gate"
    assert "held_out_measured" in res["failed"]
    assert not any(isinstance(c, tuple) and c[0] == "merge" for c in ad.calls)


def test_develop_py_call_site_explicit_require_held_out_false_still_merges_a_clean_candidate():
    """The ONE real production caller (orchestrator/develop.py:748) now passes
    require_held_out=False EXPLICITLY — proving that explicit opt-out still lets an
    honestly-unmeasured (grade.mode: smoke) but otherwise CLEAN candidate merge, exactly
    as before 056f96f/this fix (the whole point of the original 056f96f commit: smoke
    mode must not block every merge). champion_scores' held_out is 0.0 here (matching the
    grade_fn's own honestly-unmeasured 0.0) so the POST-merge re-baseline comparison
    (which run_code_round always does, regardless of require_held_out) sees no drop —
    an unrelated re-baseline "regression" from a mismatched champion fixture would
    auto_revert and mask what this test actually checks."""
    ad = _FakeAdapter(tests_passed=True)
    champ = {"working": 0.8, "held_out": 0.0}
    res = code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo="/cand", branch="cand",
        diff_text=_DIFF, champion_scores=champ, grade_fn=_unmeasured_grade(),
        label="cand", require_held_out=False)   # mirrors develop.py's own explicit call
    assert res["action"] == "merged" and res["merge_sha"] == "MERGESHA"
    assert ("merge", "cand") in ad.calls


def test_develop_py_module_passes_require_held_out_false_explicitly_at_its_call_site():
    """A static check that the ACTUAL production call site (not just a test mirroring
    it) states require_held_out=False explicitly — so the opt-out is a visible call-site
    decision, not a silently-inherited default (the exact bug this fix closes). There is
    exactly ONE run_code_round call in the whole module, so a whole-module substring
    check is unambiguous."""
    import inspect

    from factory.orchestrator import develop
    src = inspect.getsource(develop)
    assert src.count("code_round.run_code_round(") == 1
    assert "require_held_out=False" in src
