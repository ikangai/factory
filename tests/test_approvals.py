"""Human-queue approval execution (Task 5, 2026-07-08 design §3): `execute_approval` turns
an operator's Approve click on a `pending_approvals` row into the REAL
`graduate_and_push`/`promote_to_release` call; `reject_approval` just records the
decision. Hermetic: config resolution monkeypatched (no real git/gh/target repo),
graduate_fn/promote_fn injected — mirrors the scripted-runner idiom in
tests/test_issue_sync.py, one level up (the git/gh calls themselves are the OTHER
module's concern; here we're only proving execute_approval wires the right kwargs and
resolves/audits the row correctly)."""
import types

from factory.common.store import Blackboard
from factory.reporting import approvals


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _fake_config(monkeypatch, *, repo="o/r", root="/troot", base="basebr", release="main",
                 graduation_retest=True):
    monkeypatch.setattr(approvals.config, "target_repo_slug", lambda: repo)
    monkeypatch.setattr(approvals.config, "get_adapter",
                        lambda: types.SimpleNamespace(entry=lambda: (root, root + "/x"),
                                                      run_tests=lambda cwd, **k: (True, "ok")))
    monkeypatch.setattr(approvals.config, "target_config",
                        lambda: {"base_branch": base, "release_branch": release})
    monkeypatch.setattr(approvals.config, "load_config",
                        lambda: {"autonomy": {"graduation_retest": graduation_retest}})


# -- propose_graduation --------------------------------------------------------
def test_propose_graduation_files_a_thin_payload(tmp_path):
    with _store(tmp_path) as s:
        preview = {"action": "dry_run", "range": "a..b", "n_commits": 3,
                  "synced": [{"issue": 9, "action": "comment"}], "fetch_failed": True}
        aid = approvals.propose_graduation(s, preview=preview)
        row = s.get_approval(aid)
        assert row["kind"] == "graduation" and row["status"] == "pending"
        assert row["payload"] == {"range": "a..b", "n_commits": 3,
                                  "synced_preview": [{"issue": 9, "action": "comment"}],
                                  "fetch_failed": True}


def test_propose_graduation_defaults_missing_preview_fields(tmp_path):
    """A minimal preview (no 'synced'/'fetch_failed' keys) still files a well-shaped row —
    the payload builder must not KeyError on an unusual dry_run result."""
    with _store(tmp_path) as s:
        aid = approvals.propose_graduation(s, preview={"action": "dry_run", "n_commits": 0})
        row = s.get_approval(aid)
        assert row["payload"] == {"range": "", "n_commits": 0, "synced_preview": [],
                                  "fetch_failed": False}


# -- execute_approval: graduation ----------------------------------------------
def test_execute_approval_graduation_success_resolves_and_audits(tmp_path, monkeypatch):
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2})
        captured = {}

        def graduate_fn(**kw):
            captured.update(kw)
            return {"action": "synced", "range": "a..b", "n_commits": 2, "synced": []}

        res = approvals.execute_approval(s, aid, graduate_fn=graduate_fn)
        assert res == {"ok": True, "result": {"action": "synced", "range": "a..b",
                                              "n_commits": 2, "synced": []}}
        # the REAL kwargs — repo/root/base resolved from config, no dry_run this time
        assert captured["repo"] == "o/r" and captured["root"] == "/troot"
        assert captured["base"] == "basebr" and captured["store"] is s
        assert "dry_run" not in captured
        assert captured["test_fn"] is not None            # graduation_retest ON by default

        row = s.get_approval(aid)
        assert row["status"] == "approved" and "2" in row["note"]
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve" and actions[0]["item_ref"] == f"approval-{aid}"


def test_execute_approval_graduation_failure_leaves_row_pending(tmp_path, monkeypatch):
    """A skip (e.g. upstream drift since the preview, or a red retest) must NOT resolve the
    row — the operator needs to be able to fix the cause and retry the SAME approval."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2})

        def graduate_fn(**kw):
            return {"action": "skip", "reason": "push-failed"}

        res = approvals.execute_approval(s, aid, graduate_fn=graduate_fn)
        assert res["ok"] is False
        assert res["result"]["reason"] == "push-failed"

        row = s.get_approval(aid)
        assert row["status"] == "pending"                  # NOT resolved — retryable
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-failed"
        assert "push-failed" in actions[0]["detail"]


def test_execute_approval_graduation_uses_the_test_fn_gate(tmp_path, monkeypatch):
    """autonomy.graduation_retest OFF -> no re-test hook passed, mirroring
    orchestrator._graduation_test_fn's own gate (replicated here — see module docstring)."""
    _fake_config(monkeypatch, graduation_retest=False)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        captured = {}

        def graduate_fn(**kw):
            captured.update(kw)
            return {"action": "synced", "n_commits": 1, "synced": [], "range": ""}

        approvals.execute_approval(s, aid, graduate_fn=graduate_fn)
        assert captured["test_fn"] is None


# -- execute_approval: publication ---------------------------------------------
def test_execute_approval_publication_success(tmp_path, monkeypatch):
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})
        captured = {}

        def promote_fn(**kw):
            captured.update(kw)
            return {"action": "promoted", "sha": "abc123456", "n_commits": 5}

        res = approvals.execute_approval(s, aid, promote_fn=promote_fn)
        assert res["ok"] is True
        assert captured == {"root": "/troot", "base": "basebr", "release": "main"}
        row = s.get_approval(aid)
        assert row["status"] == "approved" and "abc123456" in row["note"]


def test_execute_approval_publication_failure_leaves_row_pending(tmp_path, monkeypatch):
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})

        def promote_fn(**kw):
            return {"action": "skip", "reason": "merge-conflict"}

        res = approvals.execute_approval(s, aid, promote_fn=promote_fn)
        assert res["ok"] is False
        row = s.get_approval(aid)
        assert row["status"] == "pending"
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-failed"
        assert "merge-conflict" in actions[0]["detail"]


# -- execute_approval: not-found / already-resolved -----------------------------
def test_execute_approval_not_found(tmp_path, monkeypatch):
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        res = approvals.execute_approval(s, 999)
        assert res == {"ok": False, "error": "not-found"}


def test_execute_approval_already_resolved_never_re_pushes(tmp_path, monkeypatch):
    """The correctness-critical guard for this seam: an approval that already resolved
    (approved/rejected/stale/superseded) must not push AGAIN on a stray/duplicate call."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        s.resolve_approval(aid, "approved", note="done earlier")
        called = {"n": 0}

        def graduate_fn(**kw):
            called["n"] += 1
            return {"action": "synced"}

        res = approvals.execute_approval(s, aid, graduate_fn=graduate_fn)
        assert res["ok"] is False
        assert called["n"] == 0


# -- reject_approval -------------------------------------------------------------
def test_reject_approval_resolves_and_audits(tmp_path):
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        res = approvals.reject_approval(s, aid, note="upstream moved on")
        assert res == {"ok": True}
        row = s.get_approval(aid)
        assert row["status"] == "rejected" and row["note"] == "upstream moved on"
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "reject" and actions[0]["detail"] == "upstream moved on"


def test_reject_approval_on_a_resolved_row_reports_false_but_still_audits(tmp_path):
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        s.resolve_approval(aid, "approved")
        res = approvals.reject_approval(s, aid, note="too late")
        assert res == {"ok": False}
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "reject"            # the attempt is still logged
