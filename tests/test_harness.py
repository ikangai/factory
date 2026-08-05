"""The self-harness loop (design: docs/plans/2026-08-05-self-harness-loop-design.md,
Components C/D): the harness_proposals schema + store CRUD (this file's first section),
then orchestrator/harness.py's plan/validate/maybe/apply/reject + the CLI (added as later
phases land). Mirrors tests/test_organizer.py's naming/structure and fake-claude_p pattern.
"""
import json

from factory.common.store import Blackboard
from factory.orchestrator import harness
from factory.reporting import weakness as weakness_mod


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


# =========================================================================================
# Component C (role prompt + plan/validate/maybe)
# =========================================================================================
def _seed_stage_failure(store, *, n: int = 3):
    """A minimal, deterministic weakness: n×no_candidate/refusal on one task — mine_weaknesses
    yields exactly one stage-failure cluster, id 'stage-failure-no-candidate-refusal', with
    evidence ids task_evidence:1..n (fresh-DB autoincrement is deterministic)."""
    store.add_task("t1", "fix a typo", source="issue")
    ids = [store.add_task_evidence("t1", action="no_candidate", stage="refusal")
          for _ in range(n)]
    return ids


def _fake_claude(reply_obj):
    """The repo's established fake-claude_p pattern (mirrors test_organizer.py's
    _fake_claude): returns (text, tokens, cost) and records what it was called with."""
    seen = {}

    def fake(prompt, *, model="", **k):
        seen["prompt"] = prompt
        seen["model"] = model
        seen["n"] = seen.get("n", 0) + 1
        return json.dumps(reply_obj), 50, 0.01

    fake.seen = seen
    return fake


# -- the authority line + seams --------------------------------------------------------------
def test_authority_line_matches_the_design_doc_verbatim():
    text = harness._bounds_text()
    assert text == harness.AUTHORITY_LINE
    assert "The harness engineer PROPOSES; it never applies." in text
    assert ("Its proposals may touch only the declared editable surface: SETTINGS_SPEC "
           "knobs, role prompt files, and learnings rows.") in text
    assert "rejected wholesale" in text
    assert "Every proposal cites the evidence rows that motivated it." in text
    assert "Application is an operator action." in text
    assert ("The trigger knob is config-only and outside SETTINGS_SPEC, so the loop can "
           "never widen or re-arm itself.") in text


def test_surface_text_lists_editable_settings_and_frozen_surfaces():
    text = harness._surface_text()
    for key in ("super_worker.max_parallel", "super_worker.reviewer"):
        assert key in text
    for pattern in harness_surface_frozen():
        assert pattern in text
    assert "autonomy.*" in text and "grade.*" in text
    assert "harness_engineer" in text.lower()


def harness_surface_frozen():
    from factory.common import harness_surface
    return harness_surface.FROZEN_SURFACES


def test_settings_text_shows_every_settings_spec_key_and_its_source(tmp_path):
    with _store(tmp_path) as s:
        s.set_setting("super_worker.max_parallel", "5")
        text = harness._settings_text(s)
        assert "super_worker.max_parallel = 5 (override" in text
        assert "super_worker.reviewer" in text
        assert "(config" in text or "(default" in text


def test_build_harness_prompt_fills_every_seam(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = harness.build_harness_prompt(s, mission=s.active_mission(), clusters=clusters)
        assert "ship it" in p
        assert "stage-failure-no-candidate-refusal" in p
        assert "super_worker.max_parallel" in p
        for seam in ("{MISSION}", "{WEAKNESS}", "{SURFACE}", "{SETTINGS}", "{MEMORY}", "{BOUNDS}"):
            assert seam not in p


def test_build_harness_prompt_with_no_mission_still_fills_every_seam(tmp_path):
    with _store(tmp_path) as s:
        p = harness.build_harness_prompt(s, mission=None, clusters=[])
        assert "{MISSION}" not in p
        assert "no active mission" in p.lower()
        assert "no weaknesses" in p.lower()


# -- validate_proposals -----------------------------------------------------------------------
def _valid_setting_proposal(weakness_id, evidence_ids):
    return {"weakness": weakness_id, "kind": "setting",
           "target": "super_worker.max_tasks_per_shift", "change": {"value": 2},
           "rationale": "narrow the fan-out", "evidence": evidence_ids[:1],
           "expected_effect": "fewer no_candidate closes", "risk": "slower throughput"}


def test_validate_proposals_accepts_a_well_formed_empty_batch(tmp_path):
    with _store(tmp_path) as s:
        ok, reasons = harness.validate_proposals([], store=s, clusters=[])
        assert ok is True and reasons == []


def test_validate_proposals_rejects_a_non_list_reply(tmp_path):
    with _store(tmp_path) as s:
        ok, reasons = harness.validate_proposals({"not": "a list"}, store=s, clusters=[])
        assert ok is False and reasons


def test_validate_proposals_rejects_more_than_max_proposals(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        wid = clusters[0]["id"]
        evidence = clusters[0]["evidence_ids"]
        props = [_valid_setting_proposal(wid, evidence) for _ in range(harness.MAX_PROPOSALS + 1)]
        # de-dup would ALSO reject these (same target repeated) — use distinct valid targets
        for i, p in enumerate(props):
            p["target"] = ["super_worker.max_tasks_per_shift", "super_worker.max_parallel",
                           "super_worker.refill_threshold", "super_worker.max_profiles",
                           "super_worker.dispatch_waves", "super_worker.scope_check"][i % 6]
            p["change"] = {"value": True} if p["target"] == "super_worker.scope_check" else {"value": 1}
        ok, reasons = harness.validate_proposals(props, store=s, clusters=clusters)
        assert ok is False
        assert any("MAX_PROPOSALS" in r for r in reasons)


def test_validate_proposals_rejects_unknown_kind(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        p["kind"] = "delete_everything"
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("kind" in r for r in reasons)


def test_validate_proposals_rejects_weakness_not_in_current_report(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal("made-up-cluster-slug", clusters[0]["evidence_ids"])
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("does not name a cluster" in r for r in reasons)


def test_validate_proposals_rejects_invented_evidence_id(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], ["task_evidence:999999"])
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("not in the weakness" in r for r in reasons)


def test_validate_proposals_rejects_empty_evidence_list(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], [])
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("non-empty list" in r for r in reasons)


def test_validate_proposals_rejects_frozen_setting_target(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        p["target"] = "super_worker.organizer"
        p["kind"] = "setting"
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("FROZEN" in r or "not a SETTINGS_SPEC key" in r for r in reasons)


def test_validate_proposals_rejects_setting_value_out_of_sane_bounds(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        p["target"] = "super_worker.max_parallel"
        p["change"] = {"value": 999}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("SANE_BOUNDS" in r for r in reasons)


def test_validate_proposals_accepts_setting_value_within_sane_bounds(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        p["target"] = "super_worker.max_parallel"
        p["change"] = {"value": 4}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is True and reasons == []


def test_validate_proposals_rejects_setting_value_that_does_not_cast(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        p["target"] = "super_worker.max_parallel"
        p["change"] = {"value": "not-a-number"}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("does not cast" in r for r in reasons)


def test_validate_proposals_rejects_prompt_change_without_a_patch(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = {"weakness": clusters[0]["id"], "kind": "prompt",
            "target": "roles/harness_engineer/prompt.md", "change": {"summary": "x"},
            "evidence": clusters[0]["evidence_ids"][:1], "rationale": "r"}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("patch" in r for r in reasons)


def test_validate_proposals_rejects_prompt_targeting_a_frozen_surface(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = {"weakness": clusters[0]["id"], "kind": "prompt", "target": "common/store.py",
            "change": {"summary": "x", "patch": "y"},
            "evidence": clusters[0]["evidence_ids"][:1], "rationale": "r"}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("roles/" in r for r in reasons)


def test_validate_proposals_accepts_a_well_formed_prompt_proposal(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = {"weakness": clusters[0]["id"], "kind": "prompt",
            "target": "roles/organizer/prompt.md",
            "change": {"summary": "tighten the bench cap wording", "patch": "- old\n+ new"},
            "evidence": clusters[0]["evidence_ids"][:1], "rationale": "r",
            "expected_effect": "clearer wording", "risk": "none"}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is True and reasons == []


def test_validate_proposals_rejects_learning_corrective_for_a_missing_learning(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = {"weakness": clusters[0]["id"], "kind": "learning_corrective",
            "target": "learning:99999", "change": {"op": "archive", "corrective": ""},
            "evidence": clusters[0]["evidence_ids"][:1], "rationale": "r"}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("does not exist" in r for r in reasons)


def test_validate_proposals_rejects_learning_corrective_for_a_pinned_learning(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        lid = s.add_learning("developer", "a pinned lesson")
        s.pin_learning(lid)
        clusters = weakness_mod.mine_weaknesses(s)
        p = {"weakness": clusters[0]["id"], "kind": "learning_corrective",
            "target": f"learning:{lid}", "change": {"op": "archive", "corrective": ""},
            "evidence": clusters[0]["evidence_ids"][:1], "rationale": "r"}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("pinned" in r for r in reasons)


def test_validate_proposals_accepts_a_well_formed_learning_corrective(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        lid = s.add_learning("developer", "a bad lesson")
        clusters = weakness_mod.mine_weaknesses(s)
        p = {"weakness": clusters[0]["id"], "kind": "learning_corrective",
            "target": f"learning:{lid}", "change": {"op": "archive", "corrective": "do X instead"},
            "evidence": clusters[0]["evidence_ids"][:1], "rationale": "r",
            "expected_effect": "e", "risk": "r"}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is True and reasons == []


def test_validate_proposals_rejects_duplicate_kind_target_pair(tmp_path):
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p1 = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        p2 = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        ok, reasons = harness.validate_proposals([p1, p2], store=s, clusters=clusters)
        assert ok is False
        assert any("duplicate" in r for r in reasons)


# -- plan_harness: propose / validate / persist, fail-closed ----------------------------------
def test_plan_harness_happy_path_persists_proposed_and_ledgers_spend(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        reply = [_valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])]
        fake = _fake_claude(reply)
        result = harness.plan_harness(s, claude_fn=fake)

        assert result is not None and len(result) == 1
        assert fake.seen["n"] == 1
        assert fake.seen["model"] == harness.config.resolve_model("")
        assert result[0]["status"] == "proposed"
        rows = s.harness_proposals(status="proposed")
        assert len(rows) == 1 and rows[0]["target"] == "super_worker.max_tasks_per_shift"
        ledger = s._all("SELECT * FROM budget_ledger WHERE notes = 'harness_engineer'")
        assert ledger and ledger[0]["tokens"] == 50


def test_plan_harness_empty_reply_persists_nothing_but_is_not_none(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s)
        fake = _fake_claude([])
        result = harness.plan_harness(s, claude_fn=fake)
        assert result == []
        assert s.harness_proposals() == []


def test_plan_harness_invalid_json_records_learning_and_persists_nothing(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s)

        def fake(prompt, **k):
            return "not json at all", 10, 0.001

        result = harness.plan_harness(s, claude_fn=fake)
        assert result is None
        assert s.harness_proposals() == []
        rows = [r for r in s.learnings_for_role("factory") if r["scope"] == "harness_engineer"]
        assert rows and "unparseable" in rows[0]["content"].lower()


def test_plan_harness_validation_failure_persists_rejected_rows_and_learning(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        bad = [{"weakness": clusters[0]["id"], "kind": "setting",
               "target": "autonomy.push_approval", "change": {"value": False},
               "evidence": clusters[0]["evidence_ids"][:1], "rationale": "r"}]
        fake = _fake_claude(bad)
        result = harness.plan_harness(s, claude_fn=fake)
        assert result is None
        rows = s.harness_proposals()
        assert len(rows) == 1 and rows[0]["status"] == "rejected"
        assert rows[0]["target"] == "autonomy.push_approval"
        learn = [r for r in s.learnings_for_role("factory") if r["scope"] == "harness_engineer"]
        assert learn and "validation" in learn[0]["content"].lower()


def test_plan_harness_stop_engaged_no_claude_call(tmp_path, monkeypatch):
    from factory.common import killswitch
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s)
        fake = _fake_claude([])
        result = harness.plan_harness(s, claude_fn=fake)
        assert result is None and fake.seen == {}
        assert s.harness_proposals() == [] and s._all("SELECT * FROM budget_ledger") == []


def test_plan_harness_ledgers_spend_with_shift_id_from_the_hook(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        sh = s.start_shift(token_budget=1000)
        reply = [_valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])]
        fake = _fake_claude(reply)
        harness.plan_harness(s, claude_fn=fake, shift_id=sh)
        rows = [r for r in s.ledger_rows(shift_id=sh) if r["notes"] == "harness_engineer"]
        assert rows and rows[0]["tokens"] == 50
        assert s.shift_spend(sh)["tokens"] == 50


def test_plan_harness_ledgers_spend_without_shift_id_when_cli_invoked(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        reply = [_valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])]
        fake = _fake_claude(reply)
        harness.plan_harness(s, claude_fn=fake)
        rows = s._all("SELECT * FROM budget_ledger WHERE notes = 'harness_engineer'")
        assert rows and rows[0]["shift_id"] is None


# -- maybe_plan_harness: the gated automatic trigger --------------------------------------------
def test_maybe_plan_harness_skips_when_evidence_is_below_the_minimum(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s, n=harness.MIN_NEW_EVIDENCE - 1)
        fake = _fake_claude([])
        result = harness.maybe_plan_harness(s, claude_fn=fake)
        assert result is None and fake.seen == {}


def test_maybe_plan_harness_calls_when_evidence_meets_the_minimum(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s, n=harness.MIN_NEW_EVIDENCE)
        clusters = weakness_mod.mine_weaknesses(s)
        reply = [_valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])]
        fake = _fake_claude(reply)
        result = harness.maybe_plan_harness(s, claude_fn=fake)
        assert result is not None and fake.seen["n"] == 1


def test_maybe_plan_harness_gate_uses_the_watermark_after_a_prior_batch(tmp_path):
    """Once a batch has run, the gate counts only task_evidence rows NEWER than that
    batch's created_at — re-mining the SAME old evidence never re-triggers the call."""
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s, n=harness.MIN_NEW_EVIDENCE)
        clusters = weakness_mod.mine_weaknesses(s)
        reply = [_valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])]
        fake = _fake_claude(reply)
        first = harness.maybe_plan_harness(s, claude_fn=fake)
        assert first is not None and fake.seen["n"] == 1

        second = harness.maybe_plan_harness(s, claude_fn=fake)
        assert second is None and fake.seen["n"] == 1   # NOT called again — no new evidence


def test_maybe_plan_harness_stop_no_call(tmp_path, monkeypatch):
    from factory.common import killswitch
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s, n=harness.MIN_NEW_EVIDENCE)
        fake = _fake_claude([])
        result = harness.maybe_plan_harness(s, claude_fn=fake)
        assert result is None and fake.seen == {}


# -- apply_proposal / reject_proposal: operator-gated, asymmetric per kind -----------------------
def test_apply_proposal_setting_writes_the_store_override(tmp_path):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 5})
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is True
        assert s.get_setting("super_worker.max_parallel") == "5"
        row = s.get_harness_proposal(pid)
        assert row["status"] == "applied" and row["decided_by"] == "operator-cli"
        assert row["applied_at"] is not None


def test_apply_proposal_learning_corrective_archives_and_records_provenance(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "a bad lesson")
        pid = s.add_harness_proposal(
            shift_id=None, weakness="w", kind="learning_corrective",
            target=f"learning:{lid}",
            change={"op": "archive", "corrective": "do X instead"})
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is True
        assert s.get_learning(lid)["archived"] == 1
        rows = [r for r in s.learnings_for_role("developer") if r["scope"] == "harness-corrective"]
        assert rows and "do X instead" in rows[0]["content"]
        assert f"proposal #{pid}" in rows[0]["content"]
        row = s.get_harness_proposal(pid)
        assert row["status"] == "applied"


def test_apply_proposal_learning_corrective_pin_without_a_corrective_text(tmp_path):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "a good but under-cited lesson")
        pid = s.add_harness_proposal(
            shift_id=None, weakness="w", kind="learning_corrective",
            target=f"learning:{lid}", change={"op": "pin", "corrective": ""})
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is True
        assert s.get_learning(lid)["pinned"] == 1
        assert not [r for r in s.learnings_for_role("developer") if r["scope"] == "harness-corrective"]


def test_apply_proposal_prompt_never_writes_a_file_and_marks_approved(tmp_path, capsys):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(
            shift_id=None, weakness="w", kind="prompt",
            target="roles/organizer/prompt.md",
            change={"summary": "tighten wording", "patch": "- old\n+ new"})
        import os

        from factory.common import paths
        prompt_path = os.path.join(paths.ROLES_DIR, "organizer", "prompt.md")
        before = os.path.getmtime(prompt_path)
        res = harness.apply_proposal(s, pid)
        after = os.path.getmtime(prompt_path)
        assert before == after   # NEVER writes a file
        assert res["ok"] is True and res.get("approved_only") is True
        row = s.get_harness_proposal(pid)
        assert row["status"] == "approved"   # not 'applied'
        out = capsys.readouterr().out
        assert "land it by hand" in out.lower() or "land by hand" in out.lower()


def test_apply_proposal_refuses_unknown_id(tmp_path):
    with _store(tmp_path) as s:
        res = harness.apply_proposal(s, 999999)
        assert res["ok"] is False


def test_apply_proposal_refuses_a_non_live_status(tmp_path):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 5},
                                     status="applied")
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is False
        assert "not live" in res["reason"]


def test_apply_proposal_rechecks_the_surface_at_apply_time(tmp_path):
    """A frozen target somehow persisted (e.g. a manual DB edit) must still refuse at
    apply time, not just at propose time."""
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.organizer", change={"value": True},
                                     status="proposed")
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is False
        row = s.get_harness_proposal(pid)
        assert row["status"] == "rejected"


def test_reject_proposal_marks_rejected_with_note(tmp_path):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 5})
        res = harness.reject_proposal(s, pid, note="not worth the risk")
        assert res["ok"] is True
        row = s.get_harness_proposal(pid)
        assert row["status"] == "rejected" and row["result"] == "not worth the risk"


def test_reject_proposal_refuses_a_non_live_status(tmp_path):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 5},
                                     status="rejected")
        res = harness.reject_proposal(s, pid)
        assert res["ok"] is False


# -- cmd_harness: the CLI surface ----------------------------------------------------------------
def test_cmd_harness_mine_prints_the_weakness_table(tmp_path, capsys):
    with _store(tmp_path) as s:
        _seed_stage_failure(s)
        harness.cmd_harness(s, "mine")
        out = capsys.readouterr().out
        assert "stage-failure-no-candidate-refusal" in out


def test_cmd_harness_plan_prints_proposals_and_persists_them(tmp_path, capsys):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        reply = [_valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])]
        fake = _fake_claude(reply)
        harness.cmd_harness(s, "plan", claude_fn=fake)
        out = capsys.readouterr().out
        assert "proposed 1" in out.lower()
        assert s.harness_proposals(status="proposed")


def test_cmd_harness_show_lists_every_proposal(tmp_path, capsys):
    with _store(tmp_path) as s:
        s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                               target="super_worker.max_parallel", change={"value": 5})
        harness.cmd_harness(s, "show")
        out = capsys.readouterr().out
        assert "super_worker.max_parallel" in out


def test_cmd_harness_show_with_no_proposals_prints_a_clear_message(tmp_path, capsys):
    with _store(tmp_path) as s:
        harness.cmd_harness(s, "show")
        out = capsys.readouterr().out
        assert "no proposals" in out.lower()


def test_cmd_harness_apply_and_reject_by_cli_id(tmp_path, capsys):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 4})
        harness.cmd_harness(s, "apply", target_id=str(pid))
        out = capsys.readouterr().out
        assert "ok" in out.lower()
        assert s.get_setting("super_worker.max_parallel") == "4"

        pid2 = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                      target="super_worker.max_tasks_per_shift",
                                      change={"value": 4})
        harness.cmd_harness(s, "reject", target_id=str(pid2))
        assert s.get_harness_proposal(pid2)["status"] == "rejected"


def test_cmd_harness_apply_with_a_non_integer_id_prints_a_clear_message(tmp_path, capsys):
    with _store(tmp_path) as s:
        harness.cmd_harness(s, "apply", target_id="not-an-int")
        out = capsys.readouterr().out
        assert "integer" in out.lower()


def test_cmd_harness_unknown_action_prints_usage(tmp_path, capsys):
    with _store(tmp_path) as s:
        harness.cmd_harness(s, "bogus")
        out = capsys.readouterr().out
        assert "usage" in out.lower()
