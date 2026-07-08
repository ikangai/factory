"""Human-queue approval execution (Task 5, 2026-07-08 design §3): `execute_approval` turns
an operator's Approve click on a `pending_approvals` row into the REAL
`graduate_and_push`/`promote_to_release` call; `reject_approval` just records the
decision. Trust-boundary hardening (quality review): the row is CLAIMED atomically
(double-click race → one push), the stored card is a PINNED CONSENT artifact (a fresh
dry-run/lag re-derivation must match it or the approval refuses and refreshes the card),
and the push runs under the cross-process repo lock.

Hermetic: config resolution monkeypatched (no real git/gh/target repo),
graduate_fn/promote_fn/lag_fn injected — mirrors the scripted-runner idiom in
tests/test_issue_sync.py, one level up (the git/gh calls themselves are the OTHER
module's concern; here we're only proving execute_approval wires the right kwargs and
claims/verifies/resolves/audits the row correctly)."""
import types

from factory.common import filelock
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


def _grad_fn(calls, *, preview, real=None):
    """A graduate_fn honoring the dry_run contract: `preview` for dry_run=True calls,
    `real` (default a matching 'synced') for the real push."""
    real = real or {"action": "synced", "range": preview.get("range", ""),
                    "n_commits": preview.get("n_commits", 0), "synced": []}

    def fn(**kw):
        calls.append(kw)
        return dict(preview) if kw.get("dry_run") else dict(real)
    return fn


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
    """Happy path: fresh preview matches the pinned card → real push runs with the right
    kwargs, row resolves executing→approved, one 'approve' audit row."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2})
        calls = []
        fn = _grad_fn(calls, preview={"action": "dry_run", "range": "a..b",
                                      "n_commits": 2, "synced": []})
        res = approvals.execute_approval(s, aid, graduate_fn=fn)
        assert res["ok"] is True and res["result"]["action"] == "synced"
        # exactly two calls: the consent re-derivation (dry_run) then the REAL push
        assert len(calls) == 2
        assert calls[0]["dry_run"] is True
        assert "dry_run" not in calls[1]
        for kw in calls:                            # both resolved from config identically
            assert kw["repo"] == "o/r" and kw["root"] == "/troot"
            assert kw["base"] == "basebr" and kw["store"] is s
        assert calls[1]["test_fn"] is not None      # graduation_retest ON by default

        row = s.get_approval(aid)
        assert row["status"] == "approved" and "2" in row["note"]
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve" and actions[0]["item_ref"] == f"approval-{aid}"


def test_execute_approval_graduation_stale_preview_refuses_and_refreshes(tmp_path, monkeypatch):
    """CONSENT PINNING: reality moved since the card was filed (more commits merged) →
    NO push; the card's payload refreshes in place, the row returns to pending, and the
    caller gets preview-stale + the fresh preview to re-render."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2,
                                                    "synced_preview": [], "fetch_failed": False})
        calls = []
        fn = _grad_fn(calls, preview={"action": "dry_run", "range": "a..c",
                                      "n_commits": 5, "synced": []})
        res = approvals.execute_approval(s, aid, graduate_fn=fn)
        assert res["ok"] is False and res["error"] == "preview-stale"
        assert res["fresh"]["n_commits"] == 5
        assert len(calls) == 1 and calls[0]["dry_run"] is True    # the REAL push never ran

        row = s.get_approval(aid)
        assert row["status"] == "pending"                          # re-clickable
        assert row["payload"]["range"] == "a..c" and row["payload"]["n_commits"] == 5
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-stale-refreshed"
        assert "2" in actions[0]["detail"] and "5" in actions[0]["detail"]


def test_execute_approval_graduation_failure_leaves_row_pending(tmp_path, monkeypatch):
    """A real-push skip (e.g. a red retest or rejected push) must NOT resolve the row —
    the operator needs to be able to fix the cause and retry the SAME approval."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2})
        fn = _grad_fn([], preview={"action": "dry_run", "range": "a..b",
                                   "n_commits": 2, "synced": []},
                      real={"action": "skip", "reason": "push-failed"})
        res = approvals.execute_approval(s, aid, graduate_fn=fn)
        assert res["ok"] is False
        assert res["result"]["reason"] == "push-failed"

        row = s.get_approval(aid)
        assert row["status"] == "pending"                  # NOT resolved — retryable
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-failed"
        assert "push-failed" in actions[0]["detail"]


def test_execute_approval_graduation_preview_failure_leaves_row_pending(tmp_path, monkeypatch):
    """The consent re-derivation itself failing (graduate_fn not returning a dry_run
    preview) aborts before any push and reverts the claim."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 2})
        calls = []

        def fn(**kw):
            calls.append(kw)
            return {"action": "skip", "reason": "stop"}

        res = approvals.execute_approval(s, aid, graduate_fn=fn)
        assert res["ok"] is False
        assert len(calls) == 1                             # never proceeded past the preview
        assert s.get_approval(aid)["status"] == "pending"
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-failed" and "preview" in actions[0]["detail"]


def test_execute_approval_graduation_uses_the_test_fn_gate(tmp_path, monkeypatch):
    """autonomy.graduation_retest OFF -> no re-test hook passed, mirroring
    orchestrator._graduation_test_fn's own gate (replicated here — see module docstring)."""
    _fake_config(monkeypatch, graduation_retest=False)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "", "n_commits": 1})
        calls = []
        fn = _grad_fn(calls, preview={"action": "dry_run", "range": "",
                                      "n_commits": 1, "synced": []})
        approvals.execute_approval(s, aid, graduate_fn=fn)
        assert calls[-1]["test_fn"] is None


# -- execute_approval: publication ---------------------------------------------
def test_execute_approval_publication_success(tmp_path, monkeypatch):
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})
        captured = {}
        lag_calls = []

        def lag_fn(**kw):
            lag_calls.append(kw)
            return {"ahead": 5}

        def promote_fn(**kw):
            captured.update(kw)
            return {"action": "promoted", "sha": "abc123456", "n_commits": 5}

        res = approvals.execute_approval(s, aid, promote_fn=promote_fn, lag_fn=lag_fn)
        assert res["ok"] is True
        # the consent re-derivation measured the SAME edge the alarm filed from
        assert lag_calls == [{"root": "/troot", "base": "main"}]
        assert captured == {"root": "/troot", "base": "basebr", "release": "main"}
        row = s.get_approval(aid)
        assert row["status"] == "approved" and "abc123456" in row["note"]


def test_execute_approval_publication_stale_lag_refuses_and_refreshes(tmp_path, monkeypatch):
    """CONSENT PINNING (publication): the lag count moved since the card was filed →
    NO promote; payload refreshes, row back to pending, preview-stale returned."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})
        promoted = {"n": 0}

        def promote_fn(**kw):
            promoted["n"] += 1
            return {"action": "promoted", "sha": "x", "n_commits": 9}

        res = approvals.execute_approval(s, aid, promote_fn=promote_fn,
                                         lag_fn=lambda **kw: {"ahead": 9})
        assert res["ok"] is False and res["error"] == "preview-stale"
        assert res["fresh"] == {"ahead": 9, "release": "main"}
        assert promoted["n"] == 0                          # never promoted
        row = s.get_approval(aid)
        assert row["status"] == "pending"
        assert row["payload"] == {"ahead": 9, "release": "main"}
        assert s.recent_operator_actions()[0]["action"] == "approve-stale-refreshed"


def test_execute_approval_publication_unmeasurable_lag_fails_closed(tmp_path, monkeypatch):
    """Can't verify consent (lag unmeasurable) → no promote, row back to pending."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})
        promoted = {"n": 0}

        def promote_fn(**kw):
            promoted["n"] += 1
            return {"action": "promoted"}

        res = approvals.execute_approval(s, aid, promote_fn=promote_fn,
                                         lag_fn=lambda **kw: {"ahead": None, "error": "no ref"})
        assert res["ok"] is False and promoted["n"] == 0
        assert s.get_approval(aid)["status"] == "pending"
        assert s.recent_operator_actions()[0]["action"] == "approve-failed"


def test_execute_approval_publication_failure_leaves_row_pending(tmp_path, monkeypatch):
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 5, "release": "main"})

        def promote_fn(**kw):
            return {"action": "skip", "reason": "merge-conflict"}

        res = approvals.execute_approval(s, aid, promote_fn=promote_fn,
                                         lag_fn=lambda **kw: {"ahead": 5})
        assert res["ok"] is False
        row = s.get_approval(aid)
        assert row["status"] == "pending"
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-failed"
        assert "merge-conflict" in actions[0]["detail"]


# -- execute_approval: claim races / resolved rows / lock ------------------------
def test_execute_approval_not_found(tmp_path, monkeypatch):
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        res = approvals.execute_approval(s, 999)
        assert res == {"ok": False, "error": "not-found"}


def test_execute_approval_already_resolved_never_re_pushes(tmp_path, monkeypatch):
    """An approval that already resolved (approved/rejected/stale/superseded) must not
    push AGAIN on a stray/duplicate call — the atomic claim refuses it."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "", "n_commits": 1})
        s.resolve_approval(aid, "approved", note="done earlier")
        called = {"n": 0}

        def graduate_fn(**kw):
            called["n"] += 1
            return {"action": "synced"}

        res = approvals.execute_approval(s, aid, graduate_fn=graduate_fn)
        assert res["ok"] is False and "not-pending" in res["error"]
        assert called["n"] == 0


def test_execute_approval_double_execute_race_second_gets_not_pending(tmp_path, monkeypatch):
    """The double-click race, sequentially simulated: a row already claimed 'executing'
    (as if another Approve is mid-push) refuses a second execution outright."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "", "n_commits": 1})
        assert s.claim_approval(aid) is True               # the "first click" holds the claim
        called = {"n": 0}

        def graduate_fn(**kw):
            called["n"] += 1
            return {"action": "synced"}

        res = approvals.execute_approval(s, aid, graduate_fn=graduate_fn)
        assert res["ok"] is False and "not-pending" in res["error"]
        assert called["n"] == 0                            # the loser never pushed
        assert s.get_approval(aid)["status"] == "executing"  # winner's claim untouched


def test_execute_approval_lock_busy_skips_and_reverts_claim(tmp_path, monkeypatch):
    """Another pusher holds the cross-process repo lock → no push, row back to pending,
    the attempt audited as lock-busy."""
    root = str(tmp_path / "repo")                          # no .git → tempdir-fallback lock path
    _fake_config(monkeypatch, root=root)
    monkeypatch.setattr(filelock, "DEFAULT_TIMEOUT_S", 0.05)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "", "n_commits": 1})
        called = {"n": 0}

        def graduate_fn(**kw):
            called["n"] += 1
            return {"action": "dry_run", "range": "", "n_commits": 1, "synced": []}

        with filelock.repo_lock(root):                     # the "other pusher"
            res = approvals.execute_approval(s, aid, graduate_fn=graduate_fn)
        assert res == {"ok": False, "error": "lock-busy"}
        assert called["n"] == 0                            # nothing ran under contention
        assert s.get_approval(aid)["status"] == "pending"
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "approve-failed" and "lock-busy" in actions[0]["detail"]


def test_execute_approval_unknown_kind_is_refused_without_claiming(tmp_path, monkeypatch):
    """A row with an unexpected kind (defensive — the CHECK constraint should prevent it)
    is refused BEFORE the claim, so it can't strand in 'executing'."""
    _fake_config(monkeypatch)
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        # simulate a kind the dispatcher doesn't know (bypasses add's normal usage)
        s.conn.execute("UPDATE pending_approvals SET kind = NULL WHERE id = ?", (aid,))
        s.conn.commit()
        res = approvals.execute_approval(s, aid)
        assert res["ok"] is False and "unknown kind" in res["error"]
        assert s.get_approval(aid)["status"] == "pending"  # never claimed


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
        assert s.resolve_approval(aid, "approved") is True
        res = approvals.reject_approval(s, aid, note="too late")
        assert res == {"ok": False}
        assert s.get_approval(aid)["status"] == "approved"  # first decision stands
        actions = s.recent_operator_actions()
        assert actions[0]["action"] == "reject"            # the attempt is still logged
