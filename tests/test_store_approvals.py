"""The human queue's decision ledger (common/store.py): pending_approvals (the approval
gate on outward pushes — Task 3 of docs/plans/2026-07-08-factory-owned-bus-human-queue.md)
and operator_actions (the audit trail of every dashboard Queue-tab action). Hermetic —
uses the `store` fixture from conftest.py (a tmp-dir, schema-initialized Blackboard)."""
from __future__ import annotations

import json


# -- pending_approvals: add/get round-trip -----------------------------------

def test_add_pending_approval_returns_id_and_round_trips_payload(store):
    payload = {"range": "base..HEAD", "n_commits": 3, "subjects": ["a", "b", "c"]}
    approval_id = store.add_pending_approval("graduation", payload)
    assert isinstance(approval_id, int)

    row = store.get_approval(approval_id)
    assert row is not None
    assert row["id"] == approval_id
    assert row["kind"] == "graduation"
    assert row["status"] == "pending"
    assert row["note"] == ""
    assert row["created_at"]
    assert row["resolved_at"] is None
    # payload fidelity: parsed back to the exact dict that was stored, and the raw
    # column round-trips through json byte-for-byte the same way
    assert row["payload"] == payload
    assert json.loads(row["payload_json"]) == payload


def test_get_approval_missing_returns_none(store):
    assert store.get_approval(999) is None


# -- supersede semantics ------------------------------------------------------

def test_second_add_of_same_kind_supersedes_the_first(store):
    first_id = store.add_pending_approval("graduation", {"n_commits": 1})
    second_id = store.add_pending_approval("graduation", {"n_commits": 2})

    first = store.get_approval(first_id)
    second = store.get_approval(second_id)
    assert first["status"] == "superseded"
    assert first["resolved_at"]  # stamped when superseded
    assert second["status"] == "pending"
    assert second["resolved_at"] is None


def test_supersede_only_touches_the_same_kind(store):
    grad_id = store.add_pending_approval("graduation", {"n": 1})
    pub_id = store.add_pending_approval("publication", {"n": 1})
    # a second graduation proposal must not touch the unrelated publication row
    grad_id_2 = store.add_pending_approval("graduation", {"n": 2})

    assert store.get_approval(grad_id)["status"] == "superseded"
    assert store.get_approval(pub_id)["status"] == "pending"
    assert store.get_approval(grad_id_2)["status"] == "pending"


def test_supersede_does_not_touch_already_resolved_rows(store):
    """A previously approved/rejected row of the same kind must stay put — supersede only
    ever touches a currently-'pending' row (there is at most one)."""
    old_id = store.add_pending_approval("graduation", {"n": 1})
    store.resolve_approval(old_id, "approved", note="shipped")
    new_id = store.add_pending_approval("graduation", {"n": 2})

    assert store.get_approval(old_id)["status"] == "approved"
    assert store.get_approval(old_id)["note"] == "shipped"
    assert store.get_approval(new_id)["status"] == "pending"


# -- latest_resolved_approval / latest_rejected_approval (Fix 3) ---------------

def test_latest_resolved_approval_returns_most_recent_terminal_row(store):
    """Fix 3a (final whole-branch review): the {RESUME} seam reads the MOST RECENTLY resolved
    approval of a kind so a rejection feeds back into planning."""
    a1 = store.add_pending_approval("graduation", {"n": 1})
    store.resolve_approval(a1, "rejected", note="not yet")
    assert store.latest_resolved_approval("graduation")["id"] == a1

    a2 = store.add_pending_approval("graduation", {"n": 2})
    store.resolve_approval(a2, "approved", note="shipped")
    row = store.latest_resolved_approval("graduation")
    assert row["id"] == a2 and row["status"] == "approved"   # the newer decision wins


def test_latest_resolved_approval_ignores_live_rows(store):
    """A still-pending row (resolved_at NULL) is not 'resolved' — the latest RESOLVED row is
    the prior rejection, not the fresh pending proposal."""
    a1 = store.add_pending_approval("graduation", {"n": 1})
    store.resolve_approval(a1, "rejected", note="no")
    store.add_pending_approval("publication", {"ahead": 3})   # unrelated, still pending
    assert store.latest_resolved_approval("graduation")["id"] == a1
    assert store.latest_resolved_approval("publication") is None


def test_latest_rejected_approval_only_matches_rejections(store):
    """Fix 3b: re-proposal suppression compares against the most recent REJECTED row only."""
    a1 = store.add_pending_approval("graduation", {"n_commits": 1, "tip_sha": "t0"})
    store.resolve_approval(a1, "rejected", note="no")
    assert store.latest_rejected_approval("graduation")["id"] == a1
    a2 = store.add_pending_approval("graduation", {"n_commits": 2, "tip_sha": "t1"})
    store.resolve_approval(a2, "approved")
    # a later APPROVED row does not become the "latest rejected"
    assert store.latest_rejected_approval("graduation")["id"] == a1
    assert store.latest_rejected_approval("publication") is None


# -- resolve_approval ----------------------------------------------------------

def test_resolve_approval_from_pending_succeeds_and_sets_resolved_at(store):
    approval_id = store.add_pending_approval("publication", {"lag": 5})
    ok = store.resolve_approval(approval_id, "approved", note="looks good")
    assert ok is True

    row = store.get_approval(approval_id)
    assert row["status"] == "approved"
    assert row["note"] == "looks good"
    assert row["resolved_at"]


def test_resolve_approval_default_note_is_empty(store):
    approval_id = store.add_pending_approval("graduation", {})
    store.resolve_approval(approval_id, "rejected")
    assert store.get_approval(approval_id)["note"] == ""


def test_resolve_approval_on_already_resolved_row_returns_false_and_leaves_it_unchanged(store):
    approval_id = store.add_pending_approval("graduation", {})
    assert store.resolve_approval(approval_id, "approved", note="first") is True
    before = store.get_approval(approval_id)

    # a second resolution attempt must be a no-op: False, and the row untouched
    ok = store.resolve_approval(approval_id, "rejected", note="second")
    assert ok is False
    after = store.get_approval(approval_id)
    assert after == before
    assert after["status"] == "approved"
    assert after["note"] == "first"


def test_resolve_approval_unknown_id_returns_false(store):
    assert store.resolve_approval(12345, "approved") is False


# -- status filter --------------------------------------------------------------

def test_pending_approvals_status_filter_and_newest_first(store):
    a = store.add_pending_approval("graduation", {"n": 1})
    b = store.add_pending_approval("publication", {"n": 1})
    store.resolve_approval(a, "approved")

    pending = store.pending_approvals()  # default status="pending"
    assert [r["id"] for r in pending] == [b]

    approved = store.pending_approvals(status="approved")
    assert [r["id"] for r in approved] == [a]

    # newest-first ordering within one status. `d` is a second publication proposal, so
    # it supersedes `b` (same kind, still pending) — only `d` and `c` remain pending.
    c = store.add_pending_approval("graduation", {"n": 2})
    d = store.add_pending_approval("publication", {"n": 2})
    still_pending = store.pending_approvals()
    assert [r["id"] for r in still_pending] == [d, c]
    assert store.get_approval(b)["status"] == "superseded"


def test_pending_approvals_payload_parsed_for_every_row(store):
    store.add_pending_approval("graduation", {"x": 1})
    store.add_pending_approval("graduation", {"x": 2})
    rows = store.pending_approvals(status="superseded")
    assert len(rows) == 1
    assert rows[0]["payload"] == {"x": 1}


# -- atomic claim (quality review fix: approve must execute at most once) --------

def test_claim_approval_transitions_pending_to_executing(store):
    aid = store.add_pending_approval("graduation", {"n": 1})
    assert store.claim_approval(aid) is True
    assert store.get_approval(aid)["status"] == "executing"


def test_claim_approval_second_claim_loses_the_race(store):
    """The double-click race: exactly ONE of two claims wins; the loser gets False."""
    aid = store.add_pending_approval("graduation", {"n": 1})
    assert store.claim_approval(aid) is True
    assert store.claim_approval(aid) is False              # already executing
    assert store.get_approval(aid)["status"] == "executing"


def test_claim_approval_refuses_resolved_rows_and_unknown_ids(store):
    aid = store.add_pending_approval("graduation", {"n": 1})
    assert store.resolve_approval(aid, "rejected") is True
    assert store.claim_approval(aid) is False
    assert store.claim_approval(9999) is False


def test_unclaim_approval_reverts_executing_to_pending(store):
    aid = store.add_pending_approval("graduation", {"n": 1})
    store.claim_approval(aid)
    assert store.unclaim_approval(aid) is True
    assert store.get_approval(aid)["status"] == "pending"


def test_unclaim_approval_only_from_executing(store):
    aid = store.add_pending_approval("graduation", {"n": 1})
    assert store.unclaim_approval(aid) is False            # pending, not executing
    store.resolve_approval(aid, "approved")
    assert store.unclaim_approval(aid) is False            # resolved rows are immutable


def test_supersede_never_touches_an_executing_row(store):
    """An executing row must survive a concurrent shift-end refile of the same kind —
    supersede-first only retires 'pending' rows, never one that is mid-push."""
    executing_id = store.add_pending_approval("graduation", {"n": 1})
    store.claim_approval(executing_id)
    fresh_id = store.add_pending_approval("graduation", {"n": 2})
    assert store.get_approval(executing_id)["status"] == "executing"  # survived
    assert store.get_approval(fresh_id)["status"] == "pending"


def test_resolve_approval_from_executing_succeeds(store):
    """The atomic-claim flow's happy end: executing → approved (and executing → rejected
    is equally legal — resolve accepts both live states)."""
    aid = store.add_pending_approval("graduation", {"n": 1})
    store.claim_approval(aid)
    assert store.resolve_approval(aid, "approved", note="pushed") is True
    row = store.get_approval(aid)
    assert row["status"] == "approved" and row["note"] == "pushed" and row["resolved_at"]


# -- update_approval_payload (stale-preview refresh) ------------------------------

def test_update_approval_payload_on_pending_row(store):
    aid = store.add_pending_approval("graduation", {"n_commits": 2})
    assert store.update_approval_payload(aid, {"n_commits": 5}) is True
    assert store.get_approval(aid)["payload"] == {"n_commits": 5}


def test_update_approval_payload_on_executing_row(store):
    aid = store.add_pending_approval("graduation", {"n_commits": 2})
    store.claim_approval(aid)
    assert store.update_approval_payload(aid, {"n_commits": 7}) is True
    assert store.get_approval(aid)["payload"] == {"n_commits": 7}


def test_update_approval_payload_refuses_resolved_rows(store):
    """A resolved row's payload is part of the immutable decision record."""
    aid = store.add_pending_approval("graduation", {"n_commits": 2})
    assert store.resolve_approval(aid, "approved") is True
    assert store.update_approval_payload(aid, {"n_commits": 9}) is False
    assert store.get_approval(aid)["payload"] == {"n_commits": 2}  # unchanged


# -- operator_actions ------------------------------------------------------------

def test_record_operator_action_round_trip(store):
    action_id = store.record_operator_action("answer", "msg-42", detail="cleared the escalation")
    assert isinstance(action_id, int)

    rows = store.recent_operator_actions()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == action_id
    assert row["action"] == "answer"
    assert row["item_ref"] == "msg-42"
    assert row["detail"] == "cleared the escalation"
    assert row["created_at"]


def test_record_operator_action_default_detail_is_empty(store):
    store.record_operator_action("task_drop", "task-abc")
    assert store.recent_operator_actions()[0]["detail"] == ""


def test_recent_operator_actions_newest_first_and_limit(store):
    for i in range(5):
        store.record_operator_action("answer", f"msg-{i}")

    rows = store.recent_operator_actions()
    assert [r["item_ref"] for r in rows] == ["msg-4", "msg-3", "msg-2", "msg-1", "msg-0"]

    limited = store.recent_operator_actions(limit=2)
    assert [r["item_ref"] for r in limited] == ["msg-4", "msg-3"]


# -- reap_orphaned_approvals (Fix 4d) ------------------------------------------

def test_reap_orphaned_approvals_resolves_stale_executing_rows(store):
    """Fix 4d (final whole-branch review): a crash between claim (pending→executing) and
    resolve strands a row in 'executing' — invisible (the queue lists only 'pending') and
    unapprovable (claim_approval refuses a non-pending row). The startup reaper resolves rows
    stuck 'executing' beyond the age floor to 'stale' with a FAIL-SAFE note (the push may or
    may not have landed — verify with git ls-remote, never assume success)."""
    aid = store.add_pending_approval("graduation", {"n_commits": 2, "tip_sha": "t0"})
    assert store.claim_approval(aid) is True               # now 'executing'
    # backdate claimed_at past the age floor (Fix B: the floor keys on CLAIM time, not
    # proposal time — a fresh claim is spared, see next test)
    store.conn.execute("UPDATE pending_approvals SET claimed_at = ? WHERE id = ?",
                       ("2000-01-01T00:00:00.000000Z", aid))
    store.conn.commit()

    reaped = store.reap_orphaned_approvals()
    assert [r["id"] for r in reaped] == [aid]
    row = store.get_approval(aid)
    assert row["status"] == "stale"
    assert row["resolved_at"]
    assert "git ls-remote" in row["note"]                  # fail-safe: verify, don't assume
    assert store.recent_operator_actions()[0]["action"] == "reap-orphaned-approval"


def test_reap_orphaned_approvals_spares_a_recent_in_flight_execution(store):
    """The age floor protects a legitimately in-flight execution in a SEPARATE process from
    being reaped mid-push: a freshly-claimed row is left alone."""
    aid = store.add_pending_approval("graduation", {"n_commits": 2})
    assert store.claim_approval(aid) is True               # 'executing', created just now
    assert store.reap_orphaned_approvals() == []
    assert store.get_approval(aid)["status"] == "executing"


def test_reap_orphaned_approvals_ignores_pending_and_resolved_rows(store):
    """Only 'executing' rows are orphan candidates — a pending card and a resolved decision
    are untouched no matter how old."""
    pend = store.add_pending_approval("graduation", {"n": 1})
    done = store.add_pending_approval("publication", {"ahead": 3})
    store.resolve_approval(done, "approved")
    store.conn.execute("UPDATE pending_approvals SET created_at = '2000-01-01T00:00:00.000000Z'")
    store.conn.commit()
    assert store.reap_orphaned_approvals() == []
    assert store.get_approval(pend)["status"] == "pending"
    assert store.get_approval(done)["status"] == "approved"


# -- reap_orphaned_approvals keys on CLAIM time, not proposal time (Fix B) --------

def test_claim_approval_stamps_claimed_at(store):
    """claim_approval now stamps WHEN the row was claimed — the reaper's age floor reads
    this, not created_at (proposal time)."""
    aid = store.add_pending_approval("graduation", {"n": 1})
    assert store.get_approval(aid)["claimed_at"] is None    # unclaimed: NULL
    store.claim_approval(aid)
    assert store.get_approval(aid)["claimed_at"]             # stamped, non-empty


def test_reap_orphaned_approvals_spares_a_recently_claimed_old_card(store):
    """Fix B: an operator can Approve a card that was PROPOSED hours ago — nothing wrong
    with that, it just sat in the queue. That click starts a BRAND NEW push right now. The
    old code keyed the age floor on created_at (proposal time), so this in-flight (or just-
    succeeded) push got reaped out from under it and mislabeled 'crashed'. Keying on
    claimed_at instead spares it: the card is old, but the CLAIM is fresh."""
    aid = store.add_pending_approval("graduation", {"n": 1})
    store.conn.execute("UPDATE pending_approvals SET created_at = ? WHERE id = ?",
                       ("2000-01-01T00:00:00.000000Z", aid))
    store.conn.commit()
    assert store.claim_approval(aid) is True                 # claimed_at stamped NOW
    assert store.reap_orphaned_approvals() == []              # spared — claimed recently
    assert store.get_approval(aid)["status"] == "executing"


def test_reap_orphaned_approvals_reaps_a_card_claimed_over_an_hour_ago(store):
    """The genuinely-stuck case still reaps: claimed long ago and never resolved."""
    aid = store.add_pending_approval("graduation", {"n": 1})
    assert store.claim_approval(aid) is True
    store.conn.execute("UPDATE pending_approvals SET claimed_at = ? WHERE id = ?",
                       ("2000-01-01T00:00:00.000000Z", aid))
    store.conn.commit()
    reaped = store.reap_orphaned_approvals()
    assert [r["id"] for r in reaped] == [aid]
    assert store.get_approval(aid)["status"] == "stale"


def test_reap_orphaned_approvals_pre_migration_row_falls_back_to_created_at(store):
    """A row claimed before this column existed has claimed_at NULL — COALESCE falls back
    to created_at, exactly as the reaper behaved before Fix B."""
    aid = store.add_pending_approval("graduation", {"n": 1})
    assert store.claim_approval(aid) is True
    store.conn.execute(
        "UPDATE pending_approvals SET claimed_at = NULL, created_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00.000000Z", aid))
    store.conn.commit()
    reaped = store.reap_orphaned_approvals()
    assert [r["id"] for r in reaped] == [aid]


def test_migrate_claimed_at_idempotent_on_already_migrated_store(store):
    """Re-running init_db() (e.g. a process restart) against a store that already has
    claimed_at must not raise (sqlite3 has no ADD COLUMN IF NOT EXISTS — the `if cols and
    'claimed_at' not in cols` guard in _migrate is what makes this safe)."""
    aid = store.add_pending_approval("graduation", {"n": 1})
    store.claim_approval(aid)
    store.init_db()                                           # re-migrate — must not raise
    cols = [r[1] for r in store.conn.execute(
        "PRAGMA table_info(pending_approvals)").fetchall()]
    assert cols.count("claimed_at") == 1                       # not duplicated
    assert store.get_approval(aid)["claimed_at"]                # data survived the re-run


def test_migrate_adds_claimed_at_to_old_pending_approvals_table(tmp_path):
    """A DB created before claimed_at existed (pre-Fix-B) gains it via _migrate — the
    ALTER TABLE pattern (common/store.py) that backfills every other additive column."""
    import sqlite3
    from factory.common.store import Blackboard

    db = str(tmp_path / "old.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE pending_approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "kind TEXT, status TEXT NOT NULL DEFAULT 'pending', payload_json TEXT NOT NULL, "
        "note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, resolved_at TEXT)")
    conn.execute(
        "INSERT INTO pending_approvals(kind, status, payload_json, created_at) "
        "VALUES ('graduation', 'executing', '{}', '2000-01-01T00:00:00.000000Z')")
    conn.commit()
    conn.close()
    with Blackboard(db) as s:
        s.init_db()                                           # must migrate, not crash
        cols = {r[1] for r in s.conn.execute(
            "PRAGMA table_info(pending_approvals)").fetchall()}
        assert "claimed_at" in cols
        row = s.get_approval(1)
        assert row["claimed_at"] is None                       # backfilled NULL
        # the pre-migration row (claimed_at NULL, created_at ancient) still reaps via the
        # COALESCE fallback
        reaped = s.reap_orphaned_approvals()
        assert [r["id"] for r in reaped] == [1]
