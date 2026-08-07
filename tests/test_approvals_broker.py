"""reporting/approvals.py — the publication-broker additions (Component D,
docs/plans/2026-08-06-publication-broker-design.md): the broker-mode `execute_approval`
path (`autonomy.publication_broker: true`), the `synced_preview` consent-compare join
(mapped gap #6), and `ingest_broker_receipts`. Mirrors tests/test_approvals.py's own
hermetic idiom (config resolution monkeypatched, prepare/graduate/promote fns injected) —
that file is left untouched; this is new coverage in a new file, per the design's binding
rules.
"""
import types

from factory.common.store import Blackboard
from factory.reporting import approvals, envelope


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _fake_config(monkeypatch, *, repo="o/r", root="/troot", base="basebr", release="main",
                 graduation_retest=True, publication_broker=False, broker_spool_root=""):
    monkeypatch.setattr(approvals.config, "target_repo_slug", lambda: repo)
    monkeypatch.setattr(approvals.config, "get_adapter",
                        lambda: types.SimpleNamespace(entry=lambda: (root, root + "/x"),
                                                      run_tests=lambda cwd, **k: (True, "ok")))
    monkeypatch.setattr(approvals.config, "target_config",
                        lambda: {"base_branch": base, "release_branch": release})
    monkeypatch.setattr(approvals.config, "load_config",
                        lambda: {"autonomy": {"graduation_retest": graduation_retest,
                                              "publication_broker": publication_broker,
                                              "broker_spool_root": broker_spool_root}})


def _grad_fn(calls, *, preview, real=None):
    real = real or {"action": "synced", "range": preview.get("range", ""),
                    "n_commits": preview.get("n_commits", 0), "synced": []}

    def fn(**kw):
        calls.append(kw)
        return dict(preview) if kw.get("dry_run") else dict(real)
    return fn


def _prepare_fn(calls, *, result):
    def fn(**kw):
        calls.append(kw)
        return dict(result)
    return fn


# -- broker-mode graduation ------------------------------------------------------------
def test_execute_approval_graduation_broker_mode_prepares_and_stays_executing(tmp_path, monkeypatch):
    _fake_config(monkeypatch, publication_broker=True)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "origin/basebr..factory/auto",
                                                    "n_commits": 2, "base_sha": "b0", "tip_sha": "t0",
                                                    "synced_preview": []})
        preview_calls = []
        graduate_fn = _grad_fn(preview_calls,
                               preview={"action": "dry_run", "range": "origin/basebr..factory/auto",
                                        "n_commits": 2, "base_sha": "b0", "tip_sha": "t0",
                                        "synced": []})
        prep_calls = []
        prepare_fn = _prepare_fn(prep_calls, result={"action": "prepared", "nonce": "nonce-abc",
                                                     "envelope": {"nonce": "nonce-abc"}})
        res = approvals.execute_approval(s, aid, graduate_fn=graduate_fn,
                                         prepare_graduate_fn=prepare_fn)
        assert res == {"ok": True, "broker": True,
                       "result": {"action": "prepared", "nonce": "nonce-abc",
                                 "envelope": {"nonce": "nonce-abc"}}}
        # graduate_fn is used ONLY for the dry-run consent re-derivation, never for a real push
        assert len(preview_calls) == 1 and preview_calls[0]["dry_run"] is True
        assert len(prep_calls) == 1 and "dry_run" not in prep_calls[0]
        assert prep_calls[0]["approval_id"] == aid

        row = s.get_approval(aid)
        assert row["status"] == "executing"               # NOT resolved
        assert row["payload"]["broker_nonce"] == "nonce-abc"
        assert row["payload"]["n_commits"] == 2            # the rest of the pinned card survives
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-broker-prepared"
        assert "nonce-abc"[:8] in actions[0]["detail"]


# F1 (round-2 integration fix): autonomy.broker_spool_root, when set, must flow into the
# prepare_fn call — without this the factory writes envelopes wherever paths.py's OWN
# default resolves to, which is NOT where a real deployment's shared spool lives.
def test_execute_approval_graduation_broker_mode_threads_configured_spool_root(tmp_path, monkeypatch):
    _fake_config(monkeypatch, publication_broker=True,
                broker_spool_root="/Users/Shared/factory-broker")
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2,
                                                    "base_sha": "b0", "tip_sha": "t0",
                                                    "synced_preview": []})
        fn = _grad_fn([], preview={"action": "dry_run", "range": "a..b", "n_commits": 2,
                                   "base_sha": "b0", "tip_sha": "t0", "synced": []})
        prep_calls = []
        prepare_fn = _prepare_fn(prep_calls, result={"action": "prepared", "nonce": "n1"})
        approvals.execute_approval(s, aid, graduate_fn=fn, prepare_graduate_fn=prepare_fn)
        assert prep_calls[0]["spool_root"] == "/Users/Shared/factory-broker"


def test_execute_approval_graduation_broker_mode_empty_spool_root_config_is_none(tmp_path, monkeypatch):
    """Empty/absent config preserves the pre-existing default-resolution behavior
    byte-for-byte — spool_root=None, not an empty string (which paths.py would treat as
    falsy-but-present differently in some callers)."""
    _fake_config(monkeypatch, publication_broker=True, broker_spool_root="")
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2,
                                                    "base_sha": "b0", "tip_sha": "t0",
                                                    "synced_preview": []})
        fn = _grad_fn([], preview={"action": "dry_run", "range": "a..b", "n_commits": 2,
                                   "base_sha": "b0", "tip_sha": "t0", "synced": []})
        prep_calls = []
        prepare_fn = _prepare_fn(prep_calls, result={"action": "prepared", "nonce": "n1"})
        approvals.execute_approval(s, aid, graduate_fn=fn, prepare_graduate_fn=prepare_fn)
        assert prep_calls[0]["spool_root"] is None


def test_execute_approval_graduation_broker_off_never_calls_prepare_fn(tmp_path, monkeypatch):
    """publication_broker defaults False — the real push path runs exactly as before, and
    the injected prepare_fn (if any) is never even called."""
    _fake_config(monkeypatch, publication_broker=False)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2,
                                                    "base_sha": "b0", "tip_sha": "t0",
                                                    "synced_preview": []})
        graduate_fn = _grad_fn([], preview={"action": "dry_run", "range": "a..b", "n_commits": 2,
                                            "base_sha": "b0", "tip_sha": "t0", "synced": []})
        prep_calls = []
        res = approvals.execute_approval(
            s, aid, graduate_fn=graduate_fn,
            prepare_graduate_fn=_prepare_fn(prep_calls, result={"action": "prepared"}))
        assert res["ok"] is True and res["result"]["action"] == "synced"
        assert prep_calls == []
        assert s.get_approval(aid)["status"] == "approved"


# -- synced_preview consent-compare (mapped gap #6) --------------------------------------
def test_execute_approval_graduation_stale_when_only_synced_preview_differs(tmp_path, monkeypatch):
    """Endpoints (range/n_commits/base_sha/tip_sha) are UNCHANGED, but the issue-sync plan
    the fresh preview would post differs from what the operator approved — e.g. another
    process already recorded a (issue, sha) pair as synced in between. Must trip
    preview-stale exactly like a moved sha."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval(
            "graduation", {"range": "a..b", "n_commits": 2, "base_sha": "b0", "tip_sha": "t0",
                           "synced_preview": [{"issue": 9, "action": "close", "commits": ["c1"]}]})
        calls = []
        fn = _grad_fn(calls, preview={"action": "dry_run", "range": "a..b", "n_commits": 2,
                                      "base_sha": "b0", "tip_sha": "t0", "synced": []})
        res = approvals.execute_approval(s, aid, graduate_fn=fn)
        assert res["ok"] is False and res["error"] == "preview-stale"
        assert len(calls) == 1 and calls[0]["dry_run"] is True   # the REAL push never ran
        row = s.get_approval(aid)
        assert row["status"] == "pending"
        assert row["payload"]["synced_preview"] == []


def test_execute_approval_graduation_matching_synced_preview_proceeds(tmp_path, monkeypatch):
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        synced = [{"issue": 9, "action": "close", "commits": ["c1"]}]
        aid = s.add_pending_approval(
            "graduation", {"range": "a..b", "n_commits": 2, "base_sha": "b0", "tip_sha": "t0",
                           "synced_preview": synced})
        calls = []
        fn = _grad_fn(calls, preview={"action": "dry_run", "range": "a..b", "n_commits": 2,
                                      "base_sha": "b0", "tip_sha": "t0", "synced": synced})
        res = approvals.execute_approval(s, aid, graduate_fn=fn)
        assert res["ok"] is True
        assert s.get_approval(aid)["status"] == "approved"


# -- broker-mode publication --------------------------------------------------------------
def test_execute_approval_publication_broker_mode_prepares_and_stays_executing(tmp_path, monkeypatch):
    _fake_config(monkeypatch, publication_broker=True)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})
        lag_calls = []

        def lag_fn(**kw):
            lag_calls.append(kw)
            return {"ahead": 5}

        prep_calls = []
        prepare_fn = _prepare_fn(prep_calls, result={"action": "prepared", "nonce": "nonce-pub"})
        res = approvals.execute_approval(s, aid, lag_fn=lag_fn, prepare_promote_fn=prepare_fn)
        assert res == {"ok": True, "broker": True, "result": {"action": "prepared", "nonce": "nonce-pub"}}
        assert len(prep_calls) == 1 and prep_calls[0]["release"] == "main"
        assert prep_calls[0]["repo"] == "o/r"           # config.target_repo_slug()
        row = s.get_approval(aid)
        assert row["status"] == "executing"
        assert row["payload"]["broker_nonce"] == "nonce-pub"
        assert row["payload"]["ahead"] == 5


def test_execute_approval_publication_broker_off_uses_promote_fn(tmp_path, monkeypatch):
    _fake_config(monkeypatch, publication_broker=False)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})

        def lag_fn(**kw):
            return {"ahead": 5}

        def promote_fn(**kw):
            return {"action": "promoted", "sha": "deadbeef", "n_commits": 5}

        res = approvals.execute_approval(s, aid, lag_fn=lag_fn, promote_fn=promote_fn)
        assert res["ok"] is True and res["result"]["action"] == "promoted"
        assert s.get_approval(aid)["status"] == "approved"


# -- ingest_broker_receipts ---------------------------------------------------------------
def _executing_row(s, kind, payload):
    aid = s.add_pending_approval(kind, payload)
    assert s.claim_approval(aid)
    return aid


def test_ingest_broker_receipts_pushed_resolves_approved(tmp_path):
    with _store(tmp_path) as s:
        aid = _executing_row(s, "graduation", {"n_commits": 2, "broker_nonce": "n1"})
        receipts_dir = tmp_path / "receipts"
        envelope.write_receipt(nonce="n1", status="pushed", receipts_dir=str(receipts_dir),
                               receipt_sha="cafef00d")
        results = approvals.ingest_broker_receipts(s, receipts_dir=str(receipts_dir),
                                                    done_dir=str(tmp_path / "done"))
        assert results == [{"nonce": "n1", "approval_id": aid, "status": "pushed"}]
        row = s.get_approval(aid)
        assert row["status"] == "approved"
        assert "cafef00d"[:9] in row["note"]
        assert s.recent_operator_actions()[0]["action"] == "broker-pushed"
        # archived out of the live receipts dir
        assert envelope.read_receipt(str(receipts_dir), "n1") is None
        assert envelope.read_receipt(str(tmp_path / "done"), "n1") is not None


def test_ingest_broker_receipts_rejected_resolves_rejected(tmp_path):
    with _store(tmp_path) as s:
        aid = _executing_row(s, "graduation", {"n_commits": 2, "broker_nonce": "n1"})
        receipts_dir = tmp_path / "receipts"
        envelope.write_receipt(nonce="n1", status="rejected", receipts_dir=str(receipts_dir),
                               detail="base moved")
        approvals.ingest_broker_receipts(s, receipts_dir=str(receipts_dir),
                                         done_dir=str(tmp_path / "done"))
        row = s.get_approval(aid)
        assert row["status"] == "rejected"
        assert "base moved" in row["note"]
        assert s.recent_operator_actions()[0]["action"] == "broker-rejected"


def test_ingest_broker_receipts_expired_resolves_rejected(tmp_path):
    with _store(tmp_path) as s:
        aid = _executing_row(s, "graduation", {"n_commits": 2, "broker_nonce": "n1"})
        receipts_dir = tmp_path / "receipts"
        envelope.write_receipt(nonce="n1", status="expired", receipts_dir=str(receipts_dir),
                               detail="envelope expired")
        approvals.ingest_broker_receipts(s, receipts_dir=str(receipts_dir),
                                         done_dir=str(tmp_path / "done"))
        assert s.get_approval(aid)["status"] == "rejected"


def test_ingest_broker_receipts_orphan_receipt_archives_without_error(tmp_path):
    """No 'executing' row pins this nonce (a duplicate envelope, a stale nonce from a
    prior factory instance) — must not crash, and must still archive so it's not
    reprocessed forever."""
    with _store(tmp_path) as s:
        receipts_dir = tmp_path / "receipts"
        envelope.write_receipt(nonce="orphan", status="pushed", receipts_dir=str(receipts_dir))
        results = approvals.ingest_broker_receipts(s, receipts_dir=str(receipts_dir),
                                                    done_dir=str(tmp_path / "done"))
        assert results == [{"nonce": "orphan", "approval_id": None, "status": "pushed"}]
        assert envelope.read_receipt(str(receipts_dir), "orphan") is None


def test_ingest_broker_receipts_only_resolves_the_matching_nonce(tmp_path):
    with _store(tmp_path) as s:
        aid1 = _executing_row(s, "graduation", {"broker_nonce": "n1"})
        aid2 = _executing_row(s, "publication", {"broker_nonce": "n2"})
        receipts_dir = tmp_path / "receipts"
        envelope.write_receipt(nonce="n1", status="pushed", receipts_dir=str(receipts_dir))
        approvals.ingest_broker_receipts(s, receipts_dir=str(receipts_dir),
                                         done_dir=str(tmp_path / "done"))
        assert s.get_approval(aid1)["status"] == "approved"
        assert s.get_approval(aid2)["status"] == "executing"   # untouched — no receipt for n2


def test_ingest_broker_receipts_empty_when_no_receipts(tmp_path):
    with _store(tmp_path) as s:
        assert approvals.ingest_broker_receipts(
            s, receipts_dir=str(tmp_path / "nope"), done_dir=str(tmp_path / "done")) == []


# F12 (round-2 integration fix): a prepare_fn that RAISES (e.g. an OSError writing the
# envelope) must never strand the row 'executing' with no nonce — it must revert to
# 'pending' (retryable), audited, exactly like any other failed push attempt.
def test_execute_approval_graduation_prepare_fn_raising_reverts_to_pending(tmp_path, monkeypatch):
    _fake_config(monkeypatch, publication_broker=True)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2,
                                                    "base_sha": "b0", "tip_sha": "t0",
                                                    "synced_preview": []})
        fn = _grad_fn([], preview={"action": "dry_run", "range": "a..b", "n_commits": 2,
                                   "base_sha": "b0", "tip_sha": "t0", "synced": []})

        def boom(**kw):
            raise OSError("No space left on device")

        res = approvals.execute_approval(s, aid, graduate_fn=fn, prepare_graduate_fn=boom)
        assert res["ok"] is False
        assert "No space left on device" in res["result"]["error"]
        row = s.get_approval(aid)
        assert row["status"] == "pending"           # NOT stranded 'executing'
        assert "broker_nonce" not in row["payload"]  # nothing partial recorded
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-failed"
        assert "No space left on device" in actions[0]["detail"]


def test_execute_approval_publication_prepare_fn_raising_reverts_to_pending(tmp_path, monkeypatch):
    _fake_config(monkeypatch, publication_broker=True)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})

        def lag_fn(**kw):
            return {"ahead": 5}

        def boom(**kw):
            raise RuntimeError("worktree add failed")

        res = approvals.execute_approval(s, aid, lag_fn=lag_fn, prepare_promote_fn=boom)
        assert res["ok"] is False
        assert "worktree add failed" in res["result"]["error"]
        assert s.get_approval(aid)["status"] == "pending"
