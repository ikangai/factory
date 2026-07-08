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
