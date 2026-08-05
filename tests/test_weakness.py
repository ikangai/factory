"""reporting/weakness.py — deterministic failure-cluster miner (design: docs/plans/
2026-08-05-self-harness-loop-design.md, Component B). Read-only, zero LLM. Mirrors
tests/test_organizer.py's naming/structure (test_<fn>_<behavior>).
"""
from factory.common.store import Blackboard
from factory.reporting import weakness


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- render_weakness_table: empty state ---------------------------------------------------
def test_render_weakness_table_empty_state_is_explicit():
    text = weakness.render_weakness_table([])
    assert "no weaknesses" in text.lower()


def test_mine_weaknesses_on_a_fresh_store_is_empty(tmp_path):
    with _store(tmp_path) as s:
        assert weakness.mine_weaknesses(s) == []


# -- stage-failure -------------------------------------------------------------------------
def test_stage_failure_clusters_group_by_action_and_stage(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "x", source="issue")
        for _ in range(3):
            s.add_task_evidence("t1", action="no_candidate", stage="refusal")
        s.add_task_evidence("t1", action="error", stage="transport")   # below MIN, excluded
        clusters = weakness.mine_weaknesses(s)
        hit = [c for c in clusters if c["kind"] == "stage-failure"]
        assert len(hit) == 1
        assert hit[0]["count"] == 3
        assert hit[0]["id"] == "stage-failure-no-candidate-refusal"
        assert len(hit[0]["evidence_ids"]) == 3
        assert all(eid.startswith("task_evidence:") for eid in hit[0]["evidence_ids"])


def test_stage_failure_clusters_respect_the_window(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "x", source="issue")
        for _ in range(5):
            s.add_task_evidence("t1", action="discarded", stage="tests")
        clusters = weakness.mine_weaknesses(s, window=2)
        hit = [c for c in clusters if c["kind"] == "stage-failure"]
        assert hit and hit[0]["count"] == 2   # only the newest 2 rows are in-window


# -- class-misroute --------------------------------------------------------------------------
def test_class_misroute_cluster_fires_when_a_tier_blocks_much_more_than_a_sibling(tmp_path):
    with _store(tmp_path) as s:
        for i in range(6):
            s.add_routing_outcome(f"t{i}", shift_id=None, org_class="risky-core",
                                  profile="", tier="fast",
                                  outcome="blocked" if i < 5 else "done", stage="tests")
        for i in range(6):
            s.add_routing_outcome(f"u{i}", shift_id=None, org_class="risky-core",
                                  profile="", tier="standard",
                                  outcome="done", stage="")
        clusters = weakness.mine_weaknesses(s)
        hit = [c for c in clusters if c["kind"] == "class-misroute"]
        assert len(hit) == 1
        assert "risky-core" in hit[0]["id"] and "fast" in hit[0]["id"]
        assert hit[0]["count"] == 6
        assert any(eid.startswith("fit:risky-core/fast") for eid in hit[0]["evidence_ids"])
        assert any(eid.startswith("fit:risky-core/standard") for eid in hit[0]["evidence_ids"])


def test_class_misroute_cluster_silent_when_only_one_tier_has_evidence(tmp_path):
    with _store(tmp_path) as s:
        for i in range(6):
            s.add_routing_outcome(f"t{i}", shift_id=None, org_class="solo-class",
                                  profile="", tier="fast", outcome="blocked", stage="tests")
        clusters = weakness.mine_weaknesses(s)
        assert not [c for c in clusters if c["kind"] == "class-misroute"]


def test_class_misroute_cluster_silent_when_blocked_rates_are_close(tmp_path):
    with _store(tmp_path) as s:
        for i in range(6):
            s.add_routing_outcome(f"t{i}", shift_id=None, org_class="even-class",
                                  profile="", tier="fast",
                                  outcome="blocked" if i < 3 else "done", stage="tests")
        for i in range(6):
            s.add_routing_outcome(f"u{i}", shift_id=None, org_class="even-class",
                                  profile="", tier="standard",
                                  outcome="blocked" if i < 2 else "done", stage="tests")
        clusters = weakness.mine_weaknesses(s)
        assert not [c for c in clusters if c["kind"] == "class-misroute"]


# -- scope-churn -------------------------------------------------------------------------
def test_scope_churn_cluster_groups_by_milestone(tmp_path):
    with _store(tmp_path) as s:
        ms = s.add_milestone("m1")
        s.add_task("t1", "x", source="issue")
        s.set_task_status("t1", "blocked", result="scope-reject: too broad")
        s.set_task_milestone("t1", ms)
        s.add_task("t2", "y", source="issue")
        s.set_task_status("t2", "blocked", result="scope-split: needs decomposing")
        s.set_task_milestone("t2", ms)
        s.add_task("t3", "z", source="issue")
        s.set_task_status("t3", "done", result="deadbeef")   # not a scope verdict
        clusters = weakness.mine_weaknesses(s)
        hit = [c for c in clusters if c["kind"] == "scope-churn"]
        assert len(hit) == 1 and hit[0]["count"] == 2
        assert f"task:t1" in hit[0]["evidence_ids"] and "task:t2" in hit[0]["evidence_ids"]


def test_scope_churn_cluster_below_min_is_excluded(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "x", source="issue")
        s.set_task_status("t1", "blocked", result="scope-reject: too broad")
        clusters = weakness.mine_weaknesses(s)
        assert not [c for c in clusters if c["kind"] == "scope-churn"]


# -- bad-lore ------------------------------------------------------------------------------
def test_bad_lore_cluster_fires_for_counterproductive_learnings(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "a proven-bad lesson")
        # 10 attributions, only 1 merged (10% share) — well under the 25% suppression floor.
        s.bump_learning_outcomes([lid], merged=True)
        for _ in range(9):
            s.bump_learning_outcomes([lid], merged=False)
        clusters = weakness.mine_weaknesses(s)
        hit = [c for c in clusters if c["kind"] == "bad-lore"]
        assert len(hit) == 1 and hit[0]["count"] == 1
        assert hit[0]["evidence_ids"] == [f"learning:{lid}"]


def test_bad_lore_cluster_silent_for_a_healthy_learning(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "a genuinely good lesson")
        for _ in range(10):
            s.bump_learning_outcomes([lid], merged=True)
        clusters = weakness.mine_weaknesses(s)
        assert not [c for c in clusters if c["kind"] == "bad-lore"]


# -- gate-flip -----------------------------------------------------------------------------
def test_gate_flip_cluster_fires_on_an_ok_to_fail_regression(tmp_path):
    with _store(tmp_path) as s:
        s.add_gate_eval_result("scope", "case-1", True, verdict="pass")
        s.add_gate_eval_result("scope", "case-1", False, verdict="reject")
        clusters = weakness.mine_weaknesses(s)
        hit = [c for c in clusters if c["kind"] == "gate-flip"]
        assert len(hit) == 1 and hit[0]["count"] == 1


def test_gate_flip_cluster_silent_when_still_passing(tmp_path):
    with _store(tmp_path) as s:
        s.add_gate_eval_result("scope", "case-1", True, verdict="pass")
        s.add_gate_eval_result("scope", "case-1", True, verdict="pass")
        clusters = weakness.mine_weaknesses(s)
        assert not [c for c in clusters if c["kind"] == "gate-flip"]


# -- shift-attrition -----------------------------------------------------------------------
def test_shift_attrition_cluster_fires_above_the_rate_threshold(tmp_path):
    with _store(tmp_path) as s:
        for status in ("halted", "timed_out", "completed", "completed", "completed"):
            sh = s.start_shift(token_budget=100)
            s.end_shift(sh, status=status)
        clusters = weakness.mine_weaknesses(s)
        hit = [c for c in clusters if c["kind"] == "shift-attrition"]
        assert len(hit) == 1 and hit[0]["count"] == 2


def test_shift_attrition_cluster_silent_below_the_shift_floor(tmp_path):
    with _store(tmp_path) as s:
        for status in ("halted", "timed_out"):
            sh = s.start_shift(token_budget=100)
            s.end_shift(sh, status=status)
        clusters = weakness.mine_weaknesses(s)
        assert not [c for c in clusters if c["kind"] == "shift-attrition"]


# -- mine_weaknesses: aggregation / ordering -------------------------------------------------
def test_mine_weaknesses_sorts_biggest_cluster_first(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "x", source="issue")
        for _ in range(2):
            s.add_task_evidence("t1", action="error", stage="transport")
        s.add_task("t2", "y", source="issue")
        for _ in range(5):
            s.add_task_evidence("t2", action="discarded", stage="tests")
        clusters = weakness.mine_weaknesses(s)
        counts = [c["count"] for c in clusters]
        assert counts == sorted(counts, reverse=True)


def test_render_weakness_table_lists_every_cluster_id(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "x", source="issue")
        for _ in range(3):
            s.add_task_evidence("t1", action="no_candidate", stage="refusal")
        clusters = weakness.mine_weaknesses(s)
        text = weakness.render_weakness_table(clusters)
        for c in clusters:
            assert c["id"] in text
            assert c["summary"] in text
