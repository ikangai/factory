"""Coverage for the two round-2 gaps the mutation sweep found last: the operator's only
CLI handles (`factory broker …` / `factory broker-receipts` — T5, previously zero tests)
and the two wirings that close the armed-mode loop back to the factory side: F8 (the
receipt's issue results reaching `store.record_issue_sync`) and F7 (armed approvals being
visible on the Queue tab at all).

Everything here is hermetic: tmp dirs for every broker path, an injected fake `gh`/git
runner, never `/Users/Shared`, never the real blackboard, never the network.
"""
from __future__ import annotations

import json
import os

import pytest

from factory.orchestrator import broker, orchestrator
from factory.reporting import approvals, envelope, human_queue
from factory.common import paths


# --------------------------------------------------------------------------------------
# F8 — a 'pushed' receipt's issue results must advance the dedup ledger
# --------------------------------------------------------------------------------------
def _receipt(tmp_path, nonce, **over):
    kw = {"nonce": nonce, "status": "pushed", "receipts_dir": str(tmp_path / "receipts"),
          "receipt_sha": "abc1234def", "detail": "pushed"}
    kw.update(over)
    return envelope.write_receipt(**kw)


def test_pushed_receipt_records_issue_sync_for_every_covered_sha(store, tmp_path):
    """The armed path's `gh` call happens inside the OPERATOR's broker, so the factory can
    only learn what actually posted from the receipt — and the envelope that listed the
    actions is archived operator-side, unreadable from here. Without this the ledger never
    advances and every later envelope re-plans the same already-closed issue."""
    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 1})
    store.claim_approval(approval_id)
    store.update_approval_payload(approval_id, {"range": "a..b", "n_commits": 1,
                                                "broker_nonce": "nonce123abc"})
    _receipt(tmp_path, "nonce123abc",
             issue_results=[{"number": 12, "op": "close", "ok": True,
                             "shas": ["sha1111111", "sha2222222"], "url": "u"}])

    assert store.issue_sync_seen(12, "sha1111111") is False
    approvals.ingest_broker_receipts(store, receipts_dir=str(tmp_path / "receipts"),
                                     done_dir=str(tmp_path / "receipts" / "done"))
    assert store.issue_sync_seen(12, "sha1111111") is True
    assert store.issue_sync_seen(12, "sha2222222") is True


def test_failed_issue_action_is_not_recorded_so_the_next_graduation_retries_it(store, tmp_path):
    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 1})
    store.claim_approval(approval_id)
    store.update_approval_payload(approval_id, {"broker_nonce": "nonce456def"})
    _receipt(tmp_path, "nonce456def",
             issue_results=[{"number": 13, "op": "close", "ok": False,
                             "shas": ["sha3333333"], "detail": "gh exploded"}])

    approvals.ingest_broker_receipts(store, receipts_dir=str(tmp_path / "receipts"),
                                     done_dir=str(tmp_path / "receipts" / "done"))
    assert store.issue_sync_seen(13, "sha3333333") is False


def test_a_rejected_receipt_records_no_issue_sync_at_all(store, tmp_path):
    """Nothing was pushed, so nothing was posted — recording here would silently skip the
    issue comment forever once the real publication finally lands."""
    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 1})
    store.claim_approval(approval_id)
    store.update_approval_payload(approval_id, {"broker_nonce": "nonce789ghi"})
    _receipt(tmp_path, "nonce789ghi", status="rejected", detail="base moved",
             issue_results=[{"number": 14, "op": "close", "ok": True, "shas": ["sha4444444"]}])

    approvals.ingest_broker_receipts(store, receipts_dir=str(tmp_path / "receipts"),
                                     done_dir=str(tmp_path / "receipts" / "done"))
    assert store.issue_sync_seen(14, "sha4444444") is False


def test_malformed_issue_results_are_skipped_not_fatal(store, tmp_path):
    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 1})
    store.claim_approval(approval_id)
    store.update_approval_payload(approval_id, {"broker_nonce": "noncemalformed"})
    _receipt(tmp_path, "noncemalformed",
             issue_results=["not-a-dict", {"number": "twelve", "ok": True, "shas": ["s"]},
                            {"number": 15, "ok": True, "shas": [None, 5]},
                            {"number": 16, "op": "comment", "ok": True, "shas": ["sha5555555"]}])

    out = approvals.ingest_broker_receipts(store, receipts_dir=str(tmp_path / "receipts"),
                                           done_dir=str(tmp_path / "receipts" / "done"))
    assert out and out[0]["status"] == "pushed"          # ingestion survived the garbage
    assert store.issue_sync_seen(16, "sha5555555") is True


# --------------------------------------------------------------------------------------
# F7 — armed-mode approvals are visible on the Queue tab
# --------------------------------------------------------------------------------------
def test_executing_approval_shows_as_awaiting_your_broker_and_is_not_actionable(store, tmp_path):
    """Previously invisible: an armed publication sits 'executing' for the envelope's whole
    TTL, and the operator had no surface saying so."""
    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 4})
    store.claim_approval(approval_id)

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path))
    item = next(i for i in q["items"] if i["type"] == "approval")
    assert item["status"] == "executing"
    assert item["actionable"] is False
    assert "awaiting your broker" in item["summary"]


def test_broker_rejection_surfaces_its_reason_on_the_queue(store, tmp_path):
    """A broker rejection's REASON previously reached no operator surface at all — only the
    row note and an audit table the dashboard never reads."""
    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2})
    store.claim_approval(approval_id)
    store.update_approval_payload(approval_id, {"range": "a..b", "n_commits": 2,
                                                "broker_rejected": True})
    store.resolve_approval(approval_id, "rejected",
                           note="broker rejected: base moved: envelope pinned aaa, live is bbb")

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path))
    item = next(i for i in q["items"] if i["type"] == "approval")
    assert "BROKER REJECTED" in item["summary"] and "base moved" in item["summary"]
    assert item["actionable"] is False


def test_an_operators_own_rejection_is_not_replayed_onto_the_queue(store, tmp_path):
    """Only BROKER rejections are news; a Reject the operator just clicked is a decision
    they already know about and must not clutter the queue."""
    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2})
    store.claim_approval(approval_id)
    store.resolve_approval(approval_id, "rejected", note="operator said no")

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path))
    assert [i for i in q["items"] if i["type"] == "approval"] == []


# --------------------------------------------------------------------------------------
# T5 — the operator's only CLI handles
# --------------------------------------------------------------------------------------
@pytest.fixture()
def broker_paths(tmp_path, monkeypatch):
    """Point every broker path at tmp dirs (the CLI reads them via common.paths)."""
    root = tmp_path / "spool"
    home = tmp_path / "opshome"
    for d in (root / "outbox", root / "receipts", home):
        os.makedirs(d, exist_ok=True)
    monkeypatch.setattr(paths, "broker_outbox_dir", lambda *a, **k: str(root / "outbox"))
    monkeypatch.setattr(paths, "broker_receipts_dir", lambda *a, **k: str(root / "receipts"))
    monkeypatch.setattr(paths, "broker_allowlist_path", lambda *a, **k: str(home / "allow.yaml"))
    monkeypatch.setattr(paths, "broker_spent_path", lambda *a, **k: str(home / "spent"))
    monkeypatch.setattr(paths, "broker_pins_path", lambda *a, **k: str(home / "pins"))
    monkeypatch.setattr(paths, "broker_processed_dir", lambda *a, **k: str(home / "processed"))
    return {"root": root, "home": home}


def test_cmd_broker_pin_then_pins_then_unpin_round_trips(broker_paths, capsys):
    sha = "a" * 40
    orchestrator.cmd_broker("pin", tip_sha=sha, note="reviewed by hand")
    out = orchestrator.cmd_broker("pins")
    assert sha in json.dumps(out) or sha in capsys.readouterr().out

    assert broker.is_pinned(str(broker_paths["home"] / "pins"), sha) is True
    orchestrator.cmd_broker("unpin", tip_sha=sha)
    assert broker.is_pinned(str(broker_paths["home"] / "pins"), sha) is False


def test_cmd_broker_pin_refuses_a_non_sha(broker_paths, capsys):
    """The CLI reports the refusal rather than raising a traceback at the operator — but
    it must genuinely refuse: nothing may end up pinned."""
    orchestrator.cmd_broker("pin", tip_sha="not-a-sha")
    assert "not a sha" in capsys.readouterr().out.lower()
    assert broker.load_pins(str(broker_paths["home"] / "pins")) == {}


def test_cmd_broker_status_reports_the_resolved_paths(broker_paths, capsys):
    """F1's own guard: a factory-side/operator-side spool mismatch is otherwise a silent,
    permanent no-op, so every action prints where it is actually looking."""
    orchestrator.cmd_broker("status")
    printed = capsys.readouterr().out
    assert str(broker_paths["root"] / "outbox") in printed


def test_cmd_broker_watch_refuses_without_unattended(broker_paths, capsys):
    """A persistent poll loop cannot prompt a human, so the choice must be explicit."""
    rc = orchestrator.cmd_broker("watch", unattended=False)
    printed = capsys.readouterr().out
    assert (rc is None or rc == 1) and "unattended" in printed.lower()


def test_cmd_broker_run_once_on_an_empty_outbox_is_a_clean_noop(broker_paths, capsys):
    orchestrator.cmd_broker("run-once", unattended=True)
    printed = capsys.readouterr().out.lower()
    assert "0" in printed or "no envelope" in printed


def test_cmd_broker_receipts_ingests_and_reports(store, broker_paths, capsys):
    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 1})
    store.claim_approval(approval_id)
    store.update_approval_payload(approval_id, {"broker_nonce": "cliNonce01"})
    envelope.write_receipt(nonce="cliNonce01", status="pushed",
                           receipts_dir=str(broker_paths["root"] / "receipts"),
                           receipt_sha="deadbeef12", detail="pushed")

    out = orchestrator.cmd_broker_receipts(store)
    assert [r["status"] for r in out] == ["pushed"]
    assert store.get_approval(approval_id)["status"] == "approved"
    assert "ingested 1 receipt" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# F1 (read half) — both directions must resolve through the SAME configured spool root
# --------------------------------------------------------------------------------------
def test_ingestion_reads_the_config_spool_root_not_the_env_default(store, tmp_path, monkeypatch):
    """The factory WRITES envelopes to `autonomy.broker_spool_root` (config) but the
    `FACTORY_BROKER_SPOOL` env var is set only in the OPERATOR's LaunchAgent plist — never
    in the factory's own environment. If ingestion resolved receipts from the env default
    it would read a directory the broker never writes to, and every armed approval would
    strand in 'executing' until its TTL, with the real receipt sitting unread.
    """
    shared = tmp_path / "shared-spool"
    os.makedirs(shared / "receipts", exist_ok=True)
    monkeypatch.delenv("FACTORY_BROKER_SPOOL", raising=False)
    monkeypatch.setattr(approvals, "_broker_spool_root", lambda: str(shared))

    approval_id = store.add_pending_approval("graduation", {"range": "a..b", "n_commits": 1})
    store.claim_approval(approval_id)
    store.update_approval_payload(approval_id, {"broker_nonce": "sharedRoot1"})
    envelope.write_receipt(nonce="sharedRoot1", status="pushed",
                           receipts_dir=str(shared / "receipts"),
                           receipt_sha="feed1234ab", detail="pushed")

    out = approvals.ingest_broker_receipts(store)
    assert [r["status"] for r in out] == ["pushed"]
    assert store.get_approval(approval_id)["status"] == "approved"
