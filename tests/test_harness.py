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


def test_build_harness_prompt_renders_the_memory_card_exactly_once(tmp_path):
    """Adversarial-review fix round, item 10: the OLD _surface_text() referenced "the
    {MEMORY} section above" — an f-string {{MEMORY}} literal that rendered as the literal
    text "{MEMORY}" inside the SURFACE seam's own text, and build_harness_prompt replaces
    {SURFACE} BEFORE {MEMORY}, so that stray literal got a SECOND pass and the real memory
    card text was spliced into the middle of the SURFACE section's sentence — the card
    appeared TWICE. A plain 'seam marker not in output' check (see the test above) can't
    catch this: after either replace pass the literal "{MEMORY}" text is gone either way.
    This test asserts the actual card CONTENT appears exactly once."""
    with _store(tmp_path) as s:
        s.add_learning("harness_engineer", "a very distinctive marker lesson xyzzy123")
        p = harness.build_harness_prompt(s, mission=None, clusters=[])
        assert p.count("xyzzy123") == 1


def test_build_harness_prompt_raises_when_the_template_is_missing_a_seam(tmp_path,
                                                                          monkeypatch):
    """Adversarial-review fix round, item 10: a prompt edit that silently drops a seam
    (e.g. deleting {BOUNDS}) must raise loudly, not ship a prompt with the authority line
    quietly missing."""
    from factory.roles import common as roles_common
    monkeypatch.setattr(roles_common, "_load_prompt",
                        lambda role: "no seams here at all, just prose")
    with _store(tmp_path) as s:
        try:
            harness.build_harness_prompt(s, mission=None, clusters=[])
            assert False, "expected a ValueError for the missing seams"
        except ValueError as e:
            assert "{BOUNDS}" in str(e) and "{MISSION}" in str(e)


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
        assert any("not among the named cluster" in r.lower() for r in reasons)


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


def test_validate_proposals_rejects_an_int_key_with_no_sane_bounds_entry(tmp_path,
                                                                          monkeypatch):
    """Adversarial-review fix round, item 9: harness_surface's own docstring documents
    "a proposal naming an int key with no [SANE_BOUNDS] entry is out of the editable
    surface" — but nothing enforced it until now. Simulate a future int SETTINGS_SPEC key
    that shipped without a paired SANE_BOUNDS entry."""
    from factory.common import config as config_mod
    fake_spec = dict(config_mod.SETTINGS_SPEC)
    fake_spec["super_worker.future_int_knob"] = int
    monkeypatch.setattr(config_mod, "SETTINGS_SPEC", fake_spec)
    with _store(tmp_path) as s:
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        p["target"] = "super_worker.future_int_knob"
        p["change"] = {"value": 3}
        ok, reasons = harness.validate_proposals([p], store=s, clusters=clusters)
        assert ok is False
        assert any("no SANE_BOUNDS entry" in r for r in reasons)


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
    """A learning_corrective can only ever be evidence-grounded against the BAD-LORE
    cluster (the one cluster kind whose evidence_ids are learning:<id> rows) — and per
    BLOCKER 7 (adversarial-review fix round), the target itself must be among the cited
    evidence, not merely some other row from the right cluster."""
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "a bad lesson")
        s.bump_learning_outcomes([lid], merged=True)
        for _ in range(9):
            s.bump_learning_outcomes([lid], merged=False)
        clusters = weakness_mod.mine_weaknesses(s)
        bad_lore = next(c for c in clusters if c["kind"] == "bad-lore")
        p = {"weakness": bad_lore["id"], "kind": "learning_corrective",
            "target": f"learning:{lid}", "change": {"op": "archive", "corrective": "do X instead"},
            "evidence": [f"learning:{lid}"], "rationale": "r",
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


def test_plan_harness_empty_reply_persists_no_proposal_rows(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s)
        fake = _fake_claude([])
        result = harness.plan_harness(s, claude_fn=fake)
        assert result == []
        assert s.harness_proposals() == []          # no PROPOSAL rows (markers excluded)


def test_plan_harness_invalid_json_records_learning_and_persists_no_proposal_rows(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s)

        def fake(prompt, **k):
            return "not json at all", 10, 0.001

        result = harness.plan_harness(s, claude_fn=fake)
        assert result is None
        assert s.harness_proposals() == []          # no PROPOSAL rows (markers excluded)
        rows = [r for r in s.learnings_for_role("factory") if r["scope"] == "harness_engineer"]
        assert rows and "unparseable" in rows[0]["content"].lower()


# -- watermark marker rows (adversarial-review fix round, BLOCKER 2) ------------------------
def test_plan_harness_empty_reply_persists_one_watermark_marker_row(tmp_path):
    """The evidence-freshness gate's watermark (store.latest_harness_proposal) must
    advance on an HONEST empty batch too, or maybe_plan_harness would re-fire a frontier
    call every single shift forever once its evidence threshold is crossed once."""
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s)
        fake = _fake_claude([])
        harness.plan_harness(s, claude_fn=fake)
        markers = s.harness_proposals(include_markers=True)
        assert len(markers) == 1
        assert markers[0]["kind"] == "none" and markers[0]["status"] == "empty"
        assert s.latest_harness_proposal() is not None
        assert s.latest_harness_proposal()["id"] == markers[0]["id"]


def test_plan_harness_unparseable_reply_persists_one_watermark_marker_row(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s)

        def fake(prompt, **k):
            return "not json at all", 10, 0.001

        harness.plan_harness(s, claude_fn=fake)
        markers = s.harness_proposals(include_markers=True)
        assert len(markers) == 1
        assert markers[0]["kind"] == "none" and markers[0]["status"] == "error"
        assert s.latest_harness_proposal()["id"] == markers[0]["id"]


def test_maybe_plan_harness_empty_batch_advances_the_watermark_no_repeat_call(tmp_path):
    """The exact scenario the adversarial review probed: an honest empty batch, then a
    SECOND maybe_plan_harness call with no NEW evidence, must be a free no-op — not a
    second frontier call (the original bug: 5 shifts of unchanged evidence = 5 calls)."""
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s, n=harness.MIN_NEW_EVIDENCE)
        fake = _fake_claude([])
        first = harness.maybe_plan_harness(s, claude_fn=fake)
        assert first == [] and fake.seen["n"] == 1

        second = harness.maybe_plan_harness(s, claude_fn=fake)
        assert second is None and fake.seen["n"] == 1   # NOT called again


def test_maybe_plan_harness_unparseable_reply_advances_the_watermark_no_repeat_call(
        tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        _seed_stage_failure(s, n=harness.MIN_NEW_EVIDENCE)
        seen = {"n": 0}

        def fake(prompt, **k):
            seen["n"] += 1
            return "not json at all", 10, 0.001

        first = harness.maybe_plan_harness(s, claude_fn=fake)
        assert first is None and seen["n"] == 1

        second = harness.maybe_plan_harness(s, claude_fn=fake)
        assert second is None and seen["n"] == 1        # NOT called again


def test_harness_proposals_excludes_markers_by_default_and_includes_on_request(tmp_path):
    with _store(tmp_path) as s:
        s.add_harness_proposal(shift_id=None, weakness="", kind="none", target="",
                               change={}, status="empty")
        s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                               target="super_worker.max_parallel", change={"value": 2},
                               status="proposed")
        assert len(s.harness_proposals()) == 1
        assert s.harness_proposals()[0]["kind"] == "setting"
        assert len(s.harness_proposals(include_markers=True)) == 2


def test_harness_proposal_counts_excludes_markers(tmp_path):
    with _store(tmp_path) as s:
        s.add_harness_proposal(shift_id=None, weakness="", kind="none", target="",
                               change={}, status="empty")
        s.add_harness_proposal(shift_id=None, weakness="", kind="none", target="",
                               change={}, status="error")
        s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                               target="super_worker.max_parallel", change={"value": 2},
                               status="proposed")
        counts = s.harness_proposal_counts()
        assert counts == {"proposed": 1}


def test_latest_harness_proposal_includes_marker_rows(tmp_path):
    with _store(tmp_path) as s:
        s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                               target="super_worker.max_parallel", change={"value": 2},
                               status="proposed")
        marker_id = s.add_harness_proposal(shift_id=None, weakness="", kind="none",
                                           target="", change={}, status="empty")
        assert s.latest_harness_proposal()["id"] == marker_id


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


# -- raw-vs-cast apply (adversarial-review fix round, BLOCKER 1) ----------------------------
def test_apply_proposal_setting_stores_the_canonical_cast_value_not_the_raw_json(tmp_path):
    """A validated {"value": 2.0} for an int knob must store the CANONICAL cast string
    '2' — never the raw JSON value '2.0', which would brick every subsequent
    resolve_setting/_cast_setting read (config._cast_setting's int(...) on the string
    '2.0' raises ValueError)."""
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 2.0})
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is True
        assert s.get_setting("super_worker.max_parallel") == "2"   # canonical, not '2.0'
        # And the round-trip through resolve_setting must not raise.
        from factory.common import config as config_mod
        val, source = config_mod.resolve_setting(s, "super_worker.max_parallel", None)
        assert val == 2 and source == "override"


def test_apply_proposal_setting_refuses_when_value_drifted_out_of_bounds_since_propose(
        tmp_path, monkeypatch):
    """A value that validated clean at propose time but would now fail SANE_BOUNDS (e.g.
    the bounds table itself changed between propose and apply) must refuse at apply time
    too, exactly like the frozen-target re-check."""
    from factory.common import harness_surface as hs_mod
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 7})
        # Narrow the bounds AFTER the proposal was filed (simulates drift).
        narrowed = dict(hs_mod.SANE_BOUNDS)
        narrowed["super_worker.max_parallel"] = (1, 4)
        monkeypatch.setattr(hs_mod, "SANE_BOUNDS", narrowed)
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is False
        assert "SANE_BOUNDS" in res["reason"]
        row = s.get_harness_proposal(pid)
        assert row["status"] == "rejected"
        assert s.get_setting("super_worker.max_parallel") is None   # never written


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


# -- operator-gate visibility (adversarial-review fix round, BLOCKER 6) ---------------------
def test_cmd_harness_show_list_carries_an_inline_change_summary(tmp_path, capsys):
    """A `require_test=false` setting must never look indistinguishable from a benign
    retune — the list view must show the concrete change, not just kind+target."""
    with _store(tmp_path) as s:
        s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                               target="super_worker.max_parallel", change={"value": 5})
        harness.cmd_harness(s, "show")
        out = capsys.readouterr().out
        assert "value=5" in out


def test_cmd_harness_show_with_id_prints_full_detail(tmp_path, capsys):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(
            shift_id=None, weakness="stage-failure-x", kind="learning_corrective",
            target="learning:7",
            change={"op": "archive", "corrective": "do X instead"},
            rationale="the rationale text\nExpected effect: fewer failures\nRisk: none",
            evidence=["learning:7"])
        harness.cmd_harness(s, "show", target_id=str(pid))
        out = capsys.readouterr().out
        assert f"proposal #{pid}" in out
        assert "learning_corrective" in out
        assert "learning:7" in out
        assert "archive" in out and "do X instead" in out
        assert "the rationale text" in out
        assert "Expected effect: fewer failures" in out
        assert "Risk: none" in out


def test_cmd_harness_show_with_unknown_id_prints_a_clear_message(tmp_path, capsys):
    with _store(tmp_path) as s:
        harness.cmd_harness(s, "show", target_id="999999")
        out = capsys.readouterr().out
        assert "no proposal #999999" in out.lower()


def test_cmd_harness_show_with_a_non_integer_id_prints_a_clear_message(tmp_path, capsys):
    with _store(tmp_path) as s:
        harness.cmd_harness(s, "show", target_id="not-an-int")
        out = capsys.readouterr().out
        assert "integer" in out.lower()


def test_apply_proposal_setting_prints_before_doing(tmp_path, capsys):
    with _store(tmp_path) as s:
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 5})
        harness.apply_proposal(s, pid)
        out = capsys.readouterr().out
        assert "applying proposal" in out.lower()
        assert "5" in out


def test_apply_proposal_learning_corrective_prints_before_doing(tmp_path, capsys):
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "a bad lesson")
        pid = s.add_harness_proposal(
            shift_id=None, weakness="w", kind="learning_corrective",
            target=f"learning:{lid}", change={"op": "archive", "corrective": ""})
        harness.apply_proposal(s, pid)
        out = capsys.readouterr().out
        assert "applying proposal" in out.lower()
        assert f"learning #{lid}" in out


# -- persisted expected_effect/risk (adversarial-review fix round, BLOCKER 6b) --------------
def test_plan_harness_persists_expected_effect_and_risk_not_just_rationale(tmp_path):
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        ids = _seed_stage_failure(s)
        clusters = weakness_mod.mine_weaknesses(s)
        p = _valid_setting_proposal(clusters[0]["id"], clusters[0]["evidence_ids"])
        p["expected_effect"] = "distinctive-expected-effect-marker"
        p["risk"] = "distinctive-risk-marker"
        fake = _fake_claude([p])
        result = harness.plan_harness(s, claude_fn=fake)
        assert result and "distinctive-expected-effect-marker" in result[0]["rationale"]
        assert "distinctive-risk-marker" in result[0]["rationale"]


# -- apply-time pinned/counterproductive re-checks (adversarial-review fix round, 5c) -------
def test_apply_proposal_learning_corrective_refuses_when_pinned_after_propose(tmp_path):
    """An operator pin made AFTER a proposal was filed must win — never be silently
    overridden by the proposal's own archive/pin action."""
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "a lesson")
        pid = s.add_harness_proposal(
            shift_id=None, weakness="w", kind="learning_corrective",
            target=f"learning:{lid}", change={"op": "archive", "corrective": ""})
        s.pin_learning(lid)                          # the operator pins it AFTER filing
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is False
        assert "pinned" in res["reason"].lower()
        assert s.get_learning(lid)["archived"] == 0   # never archived
        row = s.get_harness_proposal(pid)
        assert row["status"] == "rejected"


def test_apply_proposal_learning_corrective_refuses_pin_on_a_now_counterproductive_row(
        tmp_path):
    """A learning that became proven-counterproductive BETWEEN propose and apply must
    never be pinned — the exact self-poisoning failure this loop exists to fix."""
    with _store(tmp_path) as s:
        lid = s.add_learning("developer", "a lesson")
        pid = s.add_harness_proposal(
            shift_id=None, weakness="w", kind="learning_corrective",
            target=f"learning:{lid}", change={"op": "pin", "corrective": ""})
        for _ in range(10):
            s.bump_learning_outcomes([lid], merged=False)   # now proven counterproductive
        res = harness.apply_proposal(s, pid)
        assert res["ok"] is False
        assert "counterproductive" in res["reason"].lower()
        assert s.get_learning(lid)["pinned"] == 0
        row = s.get_harness_proposal(pid)
        assert row["status"] == "rejected"


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


# =========================================================================================
# Component E (CLI argparse wiring + shift-end hook + config knob)
# =========================================================================================
def test_orchestrator_argparse_harness_mine_through_main(tmp_path, monkeypatch, capsys):
    """`factory harness mine` end-to-end through main()'s argparse — the hermetic pattern
    tests/test_factory_memory.py's own `learn` CLI tests use (that file is off-limits to
    edit, so this file carries its own copy)."""
    from factory.orchestrator import orchestrator as orch
    db = str(tmp_path / "f.db")
    monkeypatch.setattr(orch, "Blackboard", lambda *a, **k: Blackboard(db))
    with Blackboard(db) as s:
        s.init_db()
        _seed_stage_failure(s)
    assert orch.main(["harness", "mine"]) == 0
    out = capsys.readouterr().out
    assert "stage-failure-no-candidate-refusal" in out


def test_orchestrator_argparse_harness_apply_through_main(tmp_path, monkeypatch, capsys):
    from factory.orchestrator import orchestrator as orch
    db = str(tmp_path / "f.db")
    monkeypatch.setattr(orch, "Blackboard", lambda *a, **k: Blackboard(db))
    with Blackboard(db) as s:
        s.init_db()
        pid = s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                                     target="super_worker.max_parallel", change={"value": 6})
    assert orch.main(["harness", "apply", str(pid)]) == 0
    with Blackboard(db) as s:
        assert s.get_setting("super_worker.max_parallel") == "6"
        assert s.get_harness_proposal(pid)["status"] == "applied"


def test_orchestrator_argparse_harness_show_through_main_with_no_proposals(tmp_path, monkeypatch,
                                                                            capsys):
    from factory.orchestrator import orchestrator as orch
    db = str(tmp_path / "f.db")
    monkeypatch.setattr(orch, "Blackboard", lambda *a, **k: Blackboard(db))
    assert orch.main(["harness", "show"]) == 0
    out = capsys.readouterr().out
    assert "no proposals" in out.lower()


# -- the shift-END hook: run_shift's `harness_planner` injection seam (adversarial-review
# fix round, BLOCKER 4 — moved OUT of a post-run_shift hook in cmd_run and INTO run_shift
# itself, mirroring org_planner's own seam exactly, so it (a) ledgers spend BEFORE the
# tokens_used rollup, (b) never fires after a tripped budget/timeout/halt brake, and (c)
# is directly testable without threading exceptions through cmd_run's own try/except).
# -----------------------------------------------------------------------------------------
def _completed(store, *, shift_id, mission, token_budget, wall_clock_s):
    return {"status": "completed", "report": "did 2 tasks", "resume_note": "t9 blocked"}


def _spender(spend, resume_note="planned t1; blocked on t9"):
    """A conductor that ledgers `spend` tokens against its shift, then plans normally
    (mirrors tests/test_shift_harness.py's own `_spender` — that file is off-limits to
    edit, so this file carries its own copy)."""
    def cond(store, *, shift_id, mission, token_budget, wall_clock_s):
        store.add_budget("conductor", spend, shift_id=shift_id)
        return {"status": "completed", "resume_note": resume_note}
    return cond


def test_run_shift_calls_harness_planner_after_the_stop_check_with_the_new_shift_id(
        tmp_path, monkeypatch):
    from factory.orchestrator import shift as shiftmod
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        seen = {}

        def harness_planner(store, *, shift_id):
            seen["shift_id"] = shift_id
            seen["mission"] = store.active_mission()["statement"]

        res = shiftmod.run_shift(s, token_budget=1000, conductor=_completed,
                                 harness_planner=harness_planner)
        assert seen["mission"] == "ship it"
        assert seen["shift_id"] == res["shift_id"] and res["shift_id"] is not None


def test_run_shift_omitted_harness_planner_defaults_to_no_call(tmp_path, monkeypatch):
    """harness_planner=None (the default) is a pure no-op — every EXISTING run_shift test
    (none of which pass it) stays byte-identical."""
    from factory.orchestrator import shift as shiftmod
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        res = shiftmod.run_shift(s, token_budget=1000, conductor=_completed)
        assert res["action"] == "completed"        # no crash, no behavior change


def test_run_shift_harness_planner_blowup_does_not_sink_the_shift(tmp_path, monkeypatch, capsys):
    from factory.orchestrator import shift as shiftmod
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("ship it")

        def boom(store, *, shift_id):
            raise RuntimeError("harness engineer blew up")

        res = shiftmod.run_shift(s, token_budget=1000, conductor=_completed,
                                 harness_planner=boom)
        assert res["action"] == "completed"        # the harness engineer's own failure
        out = capsys.readouterr().out               # never sinks the shift
        assert "[harness]" in out and "harness engineer blew up" in out
        learn = [r for r in s.learnings_for_role("factory") if r["scope"] == "harness_engineer"]
        assert learn and "harness engineer blew up" in learn[0]["content"]


def test_run_shift_never_calls_harness_planner_when_halted(tmp_path, monkeypatch):
    from factory.orchestrator import shift as shiftmod
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: True)
    with _store(tmp_path) as s:
        s.set_mission("ship it")
        called = {"n": 0}
        shiftmod.run_shift(s, token_budget=1000, conductor=_completed,
                           harness_planner=lambda store, **k: called.__setitem__(
                               "n", called["n"] + 1))
        assert called["n"] == 0


def test_run_shift_skips_harness_planner_when_budget_exhausted(tmp_path, monkeypatch):
    """A tripped brake must not spend MORE frontier tokens trying to improve the very
    loop that tripped it (adversarial-review fix round, item 4b)."""
    from factory.orchestrator import shift as shiftmod
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config",
                        lambda: {"autonomy": {"enforce_shift_budget": True}})
    with _store(tmp_path) as s:
        s.set_mission("x")
        called = {"n": 0}

        def executor(store, *, shift_id):
            return 0

        res = shiftmod.run_shift(
            s, token_budget=1000, conductor=_spender(1500), executor=executor,
            harness_planner=lambda store, **k: called.__setitem__("n", called["n"] + 1))
        assert res["action"] == "budget_exhausted"
        assert called["n"] == 0


def test_run_shift_skips_harness_planner_when_halted_mid_shift(tmp_path, monkeypatch):
    """A STOP that trips DURING the shift (after the top-of-shift check) must also skip
    the harness planner — mirrors the budget_exhausted skip's rationale."""
    from factory.orchestrator import shift as shiftmod
    calls = {"n": 0}

    def flips_halted_after_first_check():
        calls["n"] += 1
        return calls["n"] > 1   # False the first time (shift starts), True from then on

    monkeypatch.setattr(shiftmod.killswitch, "is_halted", flips_halted_after_first_check)
    with _store(tmp_path) as s:
        s.set_mission("x")
        planner_called = {"n": 0}
        res = shiftmod.run_shift(
            s, token_budget=1000, conductor=_completed,
            harness_planner=lambda store, **k: planner_called.__setitem__(
                "n", planner_called["n"] + 1))
        assert res["action"] == "halted"
        assert planner_called["n"] == 0


def test_run_shift_harness_planner_spend_lands_before_tokens_used_rollup(tmp_path, monkeypatch):
    """Item 4a: a post-run_shift hook (the ORIGINAL, now-removed placement) ledgered its
    spend too LATE for tokens_used/the loop's cumulative brake to ever see it. Prove the
    fix: a harness_planner that spends tokens via store.add_budget(shift_id=...) has that
    spend INCLUDED in run_shift's own returned tokens_used (and thus in shift_spend, which
    the unattended loop's cumulative ceiling reads)."""
    from factory.orchestrator import shift as shiftmod
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    with _store(tmp_path) as s:
        s.set_mission("ship it")

        def harness_planner(store, *, shift_id):
            store.add_budget("harness_engineer", 777, notes="harness_engineer",
                             shift_id=shift_id)

        res = shiftmod.run_shift(s, token_budget=100000, conductor=_completed,
                                 harness_planner=harness_planner)
        assert res["tokens_used"] >= 777
        assert s.shift_spend(res["shift_id"])["tokens"] == res["tokens_used"]


# -- cmd_run's wiring of harness_planner (nested under "executor is None", mirroring
# org_planner exactly — a caller-supplied executor bypasses the DEFAULT-building block
# entirely, so it must opt into harness_planner explicitly) ------------------------------
def _hermetic_cmd_run(monkeypatch):
    """The same hermetic stubs test_run_cli.py's autouse fixture applies (mirrors
    test_organizer.py's own local copy — that file is off-limits to edit too)."""
    from factory.orchestrator import orchestrator
    from factory.orchestrator import shift as shiftmod
    from factory.roles import research_feed
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(research_feed, "propose_directions", lambda store, **k: [])
    monkeypatch.setattr(orchestrator, "_read_mission_md", lambda: None)
    monkeypatch.setattr(orchestrator, "_write_mission_md", lambda statement: None)
    monkeypatch.setattr(orchestrator, "_seed_staffing", lambda store: [])
    return orchestrator


def _config_with_harness_engineer_on(monkeypatch):
    import copy
    from factory.common import config
    cfg = copy.deepcopy(config.load_config())
    cfg.setdefault("super_worker", {})["harness_engineer"] = True
    monkeypatch.setattr(config, "load_config", lambda: cfg)


def test_cmd_run_wires_harness_planner_when_the_config_knob_is_on(tmp_path, monkeypatch):
    """With super_worker.harness_engineer: true, cmd_run's DEFAULT executor-building path
    (no caller-supplied executor — mirrors test_organizer.py's own org_planner test) also
    wires maybe_plan_harness as the shift-end harness_planner — the real production
    trigger. Hermetic: monkeypatch harness.maybe_plan_harness itself."""
    orchestrator = _hermetic_cmd_run(monkeypatch)
    _config_with_harness_engineer_on(monkeypatch)
    called = {}
    monkeypatch.setattr(harness, "maybe_plan_harness",
                        lambda store, **k: called.update(k) or None)
    with _store(tmp_path) as s:
        s.set_mission("ship it")

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            return {"status": "completed"}

        res = orchestrator.cmd_run(s, conductor=conductor, token_budget=100, wall_clock_s=5)
        assert res["action"] == "completed"
        assert called.get("shift_id") == res["shift_id"]


def test_cmd_run_never_calls_maybe_plan_harness_when_the_knob_is_off_by_default(tmp_path,
                                                                                 monkeypatch):
    """The knob ships FALSE (config.yaml) — a default cmd_run must NOT wire
    maybe_plan_harness at all, the same posture as every other LLM-spending stage.

    Sentinel dict (adversarial-review fix round, item 11) — NOT an exception-raising
    `boom`: a probe showed that an AssertionError raised inside the OLD post-run_shift
    hook's own try/except got silently SWALLOWED, so an exception-based guard test stayed
    green even with the knob-check deleted. A sentinel only gets populated if the
    callable is genuinely invoked, independent of whether the caller wraps it in
    try/except."""
    orchestrator = _hermetic_cmd_run(monkeypatch)
    called = {}
    monkeypatch.setattr(harness, "maybe_plan_harness",
                        lambda store, **k: called.update(k) or None)
    with _store(tmp_path) as s:
        s.set_mission("ship it")

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            return {"status": "completed"}

        res = orchestrator.cmd_run(s, conductor=conductor, token_budget=100, wall_clock_s=5)
        assert res["action"] == "completed"
        assert called == {}


def test_cmd_run_custom_executor_never_triggers_the_default_harness_planner(tmp_path,
                                                                             monkeypatch):
    """Even with the knob ON, a caller-supplied executor bypasses cmd_run's DEFAULT-
    building block entirely — harness_planner stays None unless the caller also supplies
    one (mirrors test_organizer.py's analogous org_planner test)."""
    orchestrator = _hermetic_cmd_run(monkeypatch)
    _config_with_harness_engineer_on(monkeypatch)

    def boom(store, **k):
        raise AssertionError("must not call maybe_plan_harness with a custom executor")

    monkeypatch.setattr(harness, "maybe_plan_harness", boom)
    with _store(tmp_path) as s:
        s.set_mission("ship it")

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            return {"status": "completed"}

        def executor(store, *, shift_id):
            return 0

        res = orchestrator.cmd_run(s, conductor=conductor, executor=executor,
                                   token_budget=100, wall_clock_s=5)
        assert res["action"] == "completed"


# -- the config knob itself -----------------------------------------------------------------
def test_harness_engineer_knob_is_config_only_never_in_settings_spec():
    """Mirrors test_organizer.py's own test_organizer_knob_is_config_only_never_in_
    settings_spec: the trigger gate must stay OUT of SETTINGS_SPEC (binding rule 7) and
    ships false (off by default)."""
    from factory.common.config import SETTINGS_SPEC, load_config
    assert "super_worker.harness_engineer" not in SETTINGS_SPEC
    assert (load_config().get("super_worker") or {}).get("harness_engineer") is False


# =========================================================================================
# Component E (dashboard visibility): reporting/fleet_viz.harness_state /
# _harness_section_html / derive_queue. Kept in this file (not a fourth test file) since
# it's part of the harness feature's own surface — mirrors tests/test_org_viz.py's
# coverage of org_state/_org_section_html for the analogous org-chart feature.
# =========================================================================================
def test_harness_state_is_an_explicit_zero_state_never_a_missing_key(tmp_path):
    from factory.reporting import fleet_viz
    with _store(tmp_path) as s:
        state = fleet_viz.harness_state(s)
        assert state == {"proposed": 0, "approved": 0, "harness_engineer_on": False,
                         "newest": []}


def test_harness_state_counts_proposed_and_approved_and_lists_newest_five(tmp_path):
    from factory.reporting import fleet_viz
    with _store(tmp_path) as s:
        for i in range(7):
            s.add_harness_proposal(shift_id=None, weakness=f"w{i}", kind="setting",
                                   target="super_worker.max_parallel", change={"value": i},
                                   status="proposed" if i % 2 == 0 else "approved")
        state = fleet_viz.harness_state(s)
        assert state["proposed"] == 4 and state["approved"] == 3
        assert len(state["newest"]) == 5
        assert state["newest"][0]["weakness"] == "w6"   # newest first


def test_harness_state_reports_the_harness_engineer_knob(tmp_path, monkeypatch):
    import copy

    from factory.common import config
    from factory.reporting import fleet_viz
    cfg = copy.deepcopy(config.load_config())
    cfg.setdefault("super_worker", {})["harness_engineer"] = True
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    with _store(tmp_path) as s:
        assert fleet_viz.harness_state(s)["harness_engineer_on"] is True


def test_harness_state_never_raises_on_a_broken_store():
    from factory.reporting import fleet_viz

    class Boom:
        def harness_proposals(self, *a, **k):
            raise RuntimeError("db is gone")

    state = fleet_viz.harness_state(Boom())
    assert state == {"proposed": 0, "approved": 0, "harness_engineer_on": False, "newest": []}


def test_harness_section_html_renders_the_empty_state():
    from factory.reporting import fleet_viz
    html = fleet_viz._harness_section_html(None)
    assert "Harness proposals" in html
    assert "no proposals yet" in html
    assert "harness engineer knob: off" in html


def test_harness_section_html_renders_proposals_and_the_knob(tmp_path):
    from factory.reporting import fleet_viz
    with _store(tmp_path) as s:
        s.add_harness_proposal(shift_id=None, weakness="stage-failure-x", kind="setting",
                               target="super_worker.max_parallel", change={"value": 4},
                               status="proposed")
        state = fleet_viz.harness_state(s)
    state["harness_engineer_on"] = True
    html = fleet_viz._harness_section_html(state)
    assert "super_worker.max_parallel" in html
    assert "stage-failure-x" in html
    assert "harness engineer knob: on" in html


def test_harness_state_newest_entries_carry_a_change_summary(tmp_path):
    """Adversarial-review fix round, BLOCKER 6d: an operator gate must never be asked to
    approve a proposal it can't see the substance of at a glance."""
    from factory.reporting import fleet_viz
    with _store(tmp_path) as s:
        s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                               target="super_worker.max_parallel", change={"value": 4},
                               status="proposed")
        state = fleet_viz.harness_state(s)
        assert state["newest"][0]["change_summary"] == "value=4"


def test_harness_section_html_renders_the_change_summary(tmp_path):
    from factory.reporting import fleet_viz
    with _store(tmp_path) as s:
        s.add_harness_proposal(shift_id=None, weakness="w", kind="setting",
                               target="super_worker.max_parallel", change={"value": 4},
                               status="proposed")
        state = fleet_viz.harness_state(s)
    html = fleet_viz._harness_section_html(state)
    assert "value=4" in html


def test_fleet_json_carries_the_harness_key(tmp_path):
    from factory.reporting import fleet_viz
    with _store(tmp_path) as s:
        payload = fleet_viz.fleet_json(s)
        assert "harness" in payload
        assert payload["harness"]["proposed"] == 0


def test_render_fleet_html_includes_the_harness_section(tmp_path):
    from factory.reporting import fleet_viz
    with _store(tmp_path) as s:
        state = fleet_viz.build_fleet_state(s)
        harness = fleet_viz.harness_state(s)
        html = fleet_viz.render_fleet_html(state, harness=harness)
        assert "Harness proposals" in html


def test_derive_queue_surfaces_harness_proposals_on_the_resources_tab():
    from factory.reporting import fleet_viz
    payload = {"mission": "ship it", "harness": {"proposed": 2}}
    q = fleet_viz.derive_queue(payload)
    item = next(i for i in q if i["id"] == "harness_proposals")
    assert item["tab"] == "resources"
    assert "2 harness proposal" in item["title"]


def test_derive_queue_silent_when_no_proposals_are_awaiting():
    from factory.reporting import fleet_viz
    payload = {"mission": "ship it", "harness": {"proposed": 0}}
    q = fleet_viz.derive_queue(payload)
    assert not [i for i in q if i["id"] == "harness_proposals"]
