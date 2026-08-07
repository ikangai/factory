"""F4 (round-2 integration fix, docs/plans/2026-08-06-publication-broker-design.md):
`run_shift` used to reap orphaned approvals BEFORE ingesting broker receipts — a receipt
saying `pushed <sha>` that arrived while the operator's broker was slow could be
discarded by the SAME shift-start call that should have resolved it, permanently
recording a successful publication as 'stale — the push may or may not have reached
origin'. Ingestion must run FIRST. Hermetic: FACTORY_BROKER_SPOOL points at a tmp dir
(monkeypatched via env var — the real default resolution path, `common.paths`), no real
git/network.
"""
from datetime import datetime, timedelta, timezone

from factory.common.store import Blackboard
from factory.orchestrator import shift as shiftmod
from factory.reporting import envelope


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _backdate_claim(store, approval_id, hours_ago):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")
    store.conn.execute("UPDATE pending_approvals SET claimed_at = ? WHERE id = ?",
                       (ts, approval_id))
    store.conn.commit()


def _completed(store, *, shift_id, mission, token_budget, wall_clock_s):
    return {"status": "completed"}


def test_run_shift_ingests_a_waiting_receipt_before_it_would_be_reaped(tmp_path, monkeypatch):
    """The exact F4 regression: an approval claimed long enough ago to be reaped under
    the widened TTL grace, but with a 'pushed' receipt ALREADY sitting in the spool, must
    resolve 'approved' — not get swept to 'stale' first."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config",
                        lambda: {"autonomy": {"publication_broker": True,
                                              "envelope_ttl_hours": 1}})
    spool = tmp_path / "spool"
    monkeypatch.setenv("FACTORY_BROKER_SPOOL", str(spool))

    with _store(tmp_path) as s:
        s.set_mission("x")
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        assert s.claim_approval(aid)
        s.update_approval_payload(aid, {"n_commits": 1, "broker_nonce": "nonce1234"})
        # older than envelope_ttl_hours(1) + the 1h grace = 2h -> WOULD be reaped if the
        # reaper ran before ingestion
        _backdate_claim(s, aid, hours_ago=3)

        envelope.write_receipt(nonce="nonce1234", status="pushed",
                               receipts_dir=str(spool / "receipts"), receipt_sha="cafef00d")

        res = shiftmod.run_shift(s, token_budget=500000, conductor=_completed)
        assert res["action"] == "completed"

        row = s.get_approval(aid)
        assert row["status"] == "approved"             # NOT 'stale'
        assert "cafef00d"[:9] in row["note"]


def test_run_shift_still_reaps_a_genuinely_orphaned_approval_with_no_receipt(tmp_path, monkeypatch):
    """Regression safety: an approval with NO waiting receipt, claimed long ago, is still
    reaped to 'stale' — the ordering fix must not disable the reaper."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(shiftmod.config, "load_config",
                        lambda: {"autonomy": {"publication_broker": True,
                                              "envelope_ttl_hours": 1}})
    spool = tmp_path / "spool"
    monkeypatch.setenv("FACTORY_BROKER_SPOOL", str(spool))

    with _store(tmp_path) as s:
        s.set_mission("x")
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        assert s.claim_approval(aid)
        s.update_approval_payload(aid, {"n_commits": 1, "broker_nonce": "nonceORPHAN"})
        _backdate_claim(s, aid, hours_ago=3)

        shiftmod.run_shift(s, token_budget=500000, conductor=_completed)

        assert s.get_approval(aid)["status"] == "stale"
