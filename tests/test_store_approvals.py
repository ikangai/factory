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
