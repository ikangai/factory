"""Real behavioral merge-grade (Piece 2) — smoke evaluator + closure.

Hermetic: run_one is injected, so no real clive/LLM runs. Covers the mapping of subset run
results to the code_gate grade contract and the closure that grades the CANDIDATE's source."""
from factory.orchestrator import grade


def test_smoke_scores_all_pass_is_do_no_harm():
    runs = [{"outcome": "pass", "safety_flags": []},
            {"outcome": "pass", "safety_flags": []}]
    s = grade.smoke_scores(runs)
    assert s["working"] == 1.0
    assert s["held_out_measured"] is False       # inline gate never touches held-out (leakage)
    assert s["divergence_alarm"] is False         # deferred to the periodic re-baseline
    assert s["safety_flag"] is False


def test_smoke_scores_working_is_pass_rate():
    runs = [{"outcome": "pass"}, {"outcome": "fail"}, {"outcome": "pass"}, {"outcome": "error"}]
    assert grade.smoke_scores(runs)["working"] == 0.5


def test_smoke_scores_blocking_safety_flag_trips():
    runs = [{"outcome": "pass",
             "safety_flags": [{"kind": "destructive_op", "severity": "critical"}]}]
    assert grade.smoke_scores(runs)["safety_flag"] is True


def test_smoke_scores_low_severity_flag_does_not_trip():
    runs = [{"outcome": "pass", "safety_flags": [{"kind": "note", "severity": "info"}]}]
    assert grade.smoke_scores(runs)["safety_flag"] is False


def test_smoke_scores_empty_runs_fail_closed():
    s = grade.smoke_scores([])
    assert s["working"] == 0.0
    assert s["divergence_alarm"] is True and s["safety_flag"] is True   # nothing measured → BLOCK
    assert s["held_out_measured"] is False


def test_make_real_grade_fn_grades_the_candidate_source():
    calls = []

    def fake_run_one(cid, spec, scenario, model, *, partition, store, clive_root):
        calls.append({"scenario": scenario["id"], "clive_root": clive_root, "partition": partition})
        return {"outcome": "pass", "safety_flags": []}

    class FakeStore:
        def get_scenario(self, sid):
            return {"id": sid, "class": "single"}

    gf = grade.make_real_grade_fn(FakeStore(), scenario_ids=["gate-demo", "hard-invoice-sum"],
                                  spec_path="/champ.yaml", model_entry={"name": "m"},
                                  run_one_fn=fake_run_one)
    result = gf("/cand/repo")
    assert result["working"] == 1.0                                    # both scenarios passed
    assert [c["clive_root"] for c in calls] == ["/cand/repo", "/cand/repo"]  # graded the CANDIDATE
    assert [c["scenario"] for c in calls] == ["gate-demo", "hard-invoice-sum"]
    assert all(c["partition"] == "working" for c in calls)             # working-set only
