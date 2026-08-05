"""The self-harness loop (design: docs/plans/2026-08-05-self-harness-loop-design.md,
Components C/D): the harness_proposals schema + store CRUD (this file's first section),
then orchestrator/harness.py's plan/validate/maybe/apply/reject + the CLI (added as later
phases land). Mirrors tests/test_organizer.py's naming/structure and fake-claude_p pattern.
"""
from factory.common.store import Blackboard


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# =========================================================================================
# Component D (schema + store CRUD)
# =========================================================================================
def test_init_db_creates_harness_proposals_table_on_a_fresh_db(tmp_path):
    with _store(tmp_path) as s:
        # IF NOT EXISTS + no _migrate ALTER needed (a brand-new table, same as org_charts/
        # routing_outcomes/task_evidence before it) — a plain SELECT proves it exists.
        assert s._all("SELECT * FROM harness_proposals") == []


def test_add_harness_proposal_round_trips_change_and_evidence_json(tmp_path):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(
            shift_id=None, weakness="stage-failure-no-candidate-refusal", kind="setting",
            target="super_worker.max_parallel", change={"value": 4},
            rationale="cite the fit table", evidence=["task_evidence:1", "task_evidence:2"],
            status="proposed")
        row = s.get_harness_proposal(pid)
        assert row["kind"] == "setting"
        assert row["target"] == "super_worker.max_parallel"
        assert row["change"] == {"value": 4}
        assert row["evidence"] == ["task_evidence:1", "task_evidence:2"]
        assert row["status"] == "proposed"
        assert row["decided_at"] is None and row["applied_at"] is None


def test_add_harness_proposal_defaults_change_and_evidence_to_empty(tmp_path):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="prompt",
                                     target="roles/harness_engineer/prompt.md",
                                     change={})
        row = s.get_harness_proposal(pid)
        assert row["change"] == {} and row["evidence"] == []


def test_get_harness_proposal_unknown_id_is_none(tmp_path):
    with _store(tmp_path) as s:
        assert s.get_harness_proposal(999999) is None


def test_harness_proposals_filters_by_status_newest_first(tmp_path):
    with _store(tmp_path) as s:
        id1 = s.add_harness_proposal(shift_id=None, weakness="w1", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 2},
                                     status="proposed")
        id2 = s.add_harness_proposal(shift_id=None, weakness="w2", kind="setting",
                                     target="super_worker.max_tasks_per_shift",
                                     change={"value": 5}, status="rejected")
        id3 = s.add_harness_proposal(shift_id=None, weakness="w3", kind="setting",
                                     target="super_worker.refill_threshold",
                                     change={"value": 3}, status="proposed")
        proposed = s.harness_proposals(status="proposed")
        assert [r["id"] for r in proposed] == [id3, id1]   # newest first
        rejected = s.harness_proposals(status="rejected")
        assert [r["id"] for r in rejected] == [id2]
        every = s.harness_proposals()
        assert [r["id"] for r in every] == [id3, id2, id1]


def test_latest_harness_proposal_is_the_newest_row_of_any_status(tmp_path):
    with _store(tmp_path) as s:
        assert s.latest_harness_proposal() is None
        s.add_harness_proposal(shift_id=None, weakness="w1", kind="setting",
                               target="super_worker.max_parallel", change={"value": 2},
                               status="rejected")
        id2 = s.add_harness_proposal(shift_id=None, weakness="w2", kind="setting",
                                     target="super_worker.max_tasks_per_shift",
                                     change={"value": 5}, status="proposed")
        assert s.latest_harness_proposal()["id"] == id2


def test_set_harness_proposal_status_stamps_decided_at_and_decided_by(tmp_path):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 2})
        s.set_harness_proposal_status(pid, "approved", decided_by="operator-cli",
                                      result="looked fine")
        row = s.get_harness_proposal(pid)
        assert row["status"] == "approved"
        assert row["decided_by"] == "operator-cli"
        assert row["result"] == "looked fine"
        assert row["decided_at"] is not None
        assert row["applied_at"] is None   # 'approved' is not 'applied'


def test_set_harness_proposal_status_stamps_applied_at_only_when_applied(tmp_path):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 2})
        s.set_harness_proposal_status(pid, "applied", decided_by="operator-cli")
        row = s.get_harness_proposal(pid)
        assert row["applied_at"] is not None
        first_applied_at = row["applied_at"]
        # A later transition (e.g. a superseding replan) must never blank applied_at.
        s.set_harness_proposal_status(pid, "superseded", decided_by="operator-cli")
        row2 = s.get_harness_proposal(pid)
        assert row2["applied_at"] == first_applied_at


# -- recent_task_evidence / count_task_evidence_since (weakness-miner support reads) -------
def test_recent_task_evidence_is_global_newest_first(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "x", source="issue")
        s.add_task("t2", "y", source="issue")
        s.add_task_evidence("t1", action="no_candidate", stage="refusal")
        e2 = s.add_task_evidence("t2", action="error", stage="transport")
        rows = s.recent_task_evidence(limit=10)
        assert rows[0]["id"] == e2   # newest first, across BOTH tasks


def test_count_task_evidence_since_none_counts_everything(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "x", source="issue")
        s.add_task_evidence("t1", action="no_candidate", stage="refusal")
        s.add_task_evidence("t1", action="error", stage="transport")
        assert s.count_task_evidence_since(None) == 2


def test_count_task_evidence_since_a_timestamp_counts_only_newer_rows(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("t1", "x", source="issue")
        s.add_task_evidence("t1", action="no_candidate", stage="refusal")
        from factory.common.store import now_iso
        watermark = now_iso()
        s.add_task_evidence("t1", action="error", stage="transport")
        assert s.count_task_evidence_since(watermark) == 1


# -- all_gate_eval_results (weakness-miner support read) ------------------------------------
def test_all_gate_eval_results_scoped_by_gate_oldest_first(tmp_path):
    with _store(tmp_path) as s:
        s.add_gate_eval_result("scope", "case-1", True)
        s.add_gate_eval_result("scope", "case-1", False)
        s.add_gate_eval_result("other-gate", "case-1", True)
        rows = s.all_gate_eval_results(gate="scope")
        assert len(rows) == 2
        assert [bool(r["ok"]) for r in rows] == [True, False]   # insertion order preserved
