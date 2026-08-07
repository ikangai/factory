"""F5 (round-2 integration fix, docs/plans/2026-08-06-publication-broker-design.md): a
BROKER rejection (payload['broker_rejected'] = True, set by `reporting.approvals.
ingest_broker_receipts`) must never be reported to the conductor as "operator rejected"
— that is false attribution — and must never suppress re-proposal of an unchanged-
looking graduation/publication, since the underlying cause (a moved branch, an unpinned
tip) is often transient. Mirrors tests/test_conductor.py's own idiom (a new file — that
one is untouched)."""
from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch
from factory.reporting import issue_sync
from factory.roles import conductor


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- roles/conductor.py: _append_rejection_feedback / the {RESUME} seam --------------------
def test_conductor_prompt_labels_a_broker_rejection_distinctly(tmp_path, monkeypatch):
    from factory.roles import research_feed
    monkeypatch.setattr(research_feed, "fetch_issues", lambda repo, **k: "")
    with _store(tmp_path) as s:
        m = s.set_mission("x", target_repo="o/r")
        aid = s.add_pending_approval("graduation", {"n_commits": 3})
        assert s.claim_approval(aid)
        s.update_approval_payload(aid, {"n_commits": 3, "broker_rejected": True})
        s.resolve_approval(aid, "rejected", note="base moved")
        cur = s.start_shift(token_budget=1, mission_id=m)
        p = conductor.build_conductor_prompt(s, s.active_mission(), shift_id=cur, token_budget=1)
    assert 'operator rejected the last graduation proposal' not in p    # NOT false attribution
    assert "broker rejected the last graduation" in p
    assert "base moved" in p


def test_conductor_prompt_still_labels_a_real_human_rejection_as_such(tmp_path, monkeypatch):
    """Regression safety: a genuine operator Reject (no broker_rejected marker) keeps its
    existing wording exactly — test_conductor.py's own coverage, re-proven here."""
    from factory.roles import research_feed
    monkeypatch.setattr(research_feed, "fetch_issues", lambda repo, **k: "")
    with _store(tmp_path) as s:
        m = s.set_mission("x", target_repo="o/r")
        aid = s.add_pending_approval("graduation", {"n_commits": 3})
        s.resolve_approval(aid, "rejected", note="wait for the release window")
        cur = s.start_shift(token_budget=1, mission_id=m)
        p = conductor.build_conductor_prompt(s, s.active_mission(), shift_id=cur, token_budget=1)
    assert 'operator rejected the last graduation proposal: "wait for the release window"' in p
    assert "broker rejected" not in p


def test_append_rejection_feedback_unit(tmp_path):
    """Direct unit test of the helper (no full prompt assembly / research_feed needed)."""
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5})
        assert s.claim_approval(aid)
        s.update_approval_payload(aid, {"ahead": 5, "broker_rejected": True})
        s.resolve_approval(aid, "rejected", note="tip not pinned by the operator")
        out = conductor._append_rejection_feedback(s, "RESUME BASE")
    assert out.startswith("RESUME BASE\n")
    assert "broker rejected the last publication" in out
    assert "operator rejected the last publication" not in out


# -- orchestrator.py: _is_human_rejection + the suppression call sites ---------------------
def test_is_human_rejection_true_for_a_plain_rejected_row():
    assert orch._is_human_rejection({"status": "rejected", "payload": {}}) is True


def test_is_human_rejection_false_for_a_broker_marked_row():
    assert orch._is_human_rejection(
        {"status": "rejected", "payload": {"broker_rejected": True}}) is False


def test_is_human_rejection_false_for_none():
    assert orch._is_human_rejection(None) is False


def test_graduate_after_shift_reproposes_after_a_broker_rejection_even_if_unchanged(tmp_path, monkeypatch):
    """The exact F5 regression: WITHOUT the fix, _same_graduation would see an identical
    preview and suppress forever. A broker-caused rejection must never suppress."""
    with _store(tmp_path) as s:
        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"autonomy": {"push_approval": True,
                                                  "publication_broker": True}})
        aid = s.add_pending_approval(
            "graduation", {"range": "a..b", "n_commits": 3, "base_sha": "b0", "tip_sha": "t0"})
        assert s.claim_approval(aid)
        s.update_approval_payload(
            aid, {"range": "a..b", "n_commits": 3, "base_sha": "b0", "tip_sha": "t0",
                 "broker_rejected": True})
        s.resolve_approval(aid, "rejected", note="tip not pinned by the operator")

        def graduate_fn(**kw):
            return {"action": "dry_run", "range": "a..b", "n_commits": 3,
                   "base_sha": "b0", "tip_sha": "t0", "synced": []}

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=graduate_fn,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "proposed"              # NOT suppressed


def test_graduate_after_shift_still_suppresses_after_a_real_human_rejection(tmp_path, monkeypatch):
    """Regression safety: a genuine operator rejection of an UNCHANGED graduation still
    suppresses re-proposal exactly as before this fix."""
    with _store(tmp_path) as s:
        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"autonomy": {"push_approval": True,
                                                  "publication_broker": False}})
        aid = s.add_pending_approval(
            "graduation", {"range": "a..b", "n_commits": 3, "base_sha": "b0", "tip_sha": "t0"})
        s.resolve_approval(aid, "rejected", note="not now")

        def graduate_fn(**kw):
            return {"action": "dry_run", "range": "a..b", "n_commits": 3,
                   "base_sha": "b0", "tip_sha": "t0", "synced": []}

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=graduate_fn,
                                         repo="o/r", root="/x", base="base")
        assert res == {"action": "skip", "reason": "rejected-unchanged"}


def test_cmd_graduate_reproposes_after_a_broker_rejection_even_if_unchanged(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        monkeypatch.setattr(orch.config, "target_repo_slug", lambda: "o/r")
        monkeypatch.setattr(orch.config, "target_config", lambda: {"base_branch": "basebr"})

        class _Ad:
            def entry(self):
                return ("/troot", "/troot/clive.py")

        monkeypatch.setattr(orch.config, "get_adapter", lambda: _Ad())
        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"autonomy": {"push_approval": True,
                                                  "publication_broker": True}})
        aid = s.add_pending_approval(
            "graduation", {"range": "origin/basebr..factory/auto", "n_commits": 3,
                          "base_sha": "b0", "tip_sha": "t0"})
        assert s.claim_approval(aid)
        s.update_approval_payload(
            aid, {"range": "origin/basebr..factory/auto", "n_commits": 3, "base_sha": "b0",
                 "tip_sha": "t0", "broker_rejected": True})
        s.resolve_approval(aid, "rejected", note="base moved")

        def fake_grad(**kw):
            return {"action": "dry_run", "range": "origin/basebr..factory/auto",
                   "n_commits": 3, "base_sha": "b0", "tip_sha": "t0", "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        res = orch.cmd_graduate(s)
        assert res["action"] == "proposed"
