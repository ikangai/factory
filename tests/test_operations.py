"""Crash-consistency intent rows (common/store.py, design: docs/plans/2026-08-08-crash-
consistency-design.md, Component A). Hermetic — uses the `store` fixture from
conftest.py (a tmp-dir, schema-initialized Blackboard)."""
from __future__ import annotations


# -- begin_operation: insert + idempotency-skip semantics --------------------

def test_begin_operation_creates_a_row_in_executing(store):
    res = store.begin_operation("merge", "merge:task-1:sha1", target_ref="factory/cand-1",
                                tip_sha="sha1", payload={"task_id": "task-1"})
    assert res["created"] is True
    assert res["skip"] is False
    row = res["operation"]
    assert row["kind"] == "merge"
    assert row["idem_key"] == "merge:task-1:sha1"
    assert row["status"] == "executing"
    assert row["target_ref"] == "factory/cand-1"
    assert row["tip_sha"] == "sha1"
    assert row["payload"] == {"task_id": "task-1"}
    assert row["base_sha"] == ""
    assert row["receipt"] == ""
    assert row["attempts"] == 1
    assert row["created_at"] and row["updated_at"]


def test_begin_operation_second_call_same_key_returns_existing_not_created(store):
    first = store.begin_operation("merge", "merge:task-1:sha1")
    second = store.begin_operation("merge", "merge:task-1:sha1")
    assert second["created"] is False
    assert second["operation"]["id"] == first["operation"]["id"]


def test_begin_operation_skip_false_while_still_executing(store):
    """A second begin_operation call for a row still 'executing' (not yet resolved) is
    NOT the idempotency-skip case — the caller must not assume the effect happened."""
    store.begin_operation("merge", "merge:task-1:sha1")
    again = store.begin_operation("merge", "merge:task-1:sha1")
    assert again["skip"] is False
    assert again["operation"]["status"] == "executing"


def test_begin_operation_skip_true_when_already_applied(store):
    first = store.begin_operation("merge", "merge:task-1:sha1")
    store.complete_operation(first["operation"]["id"], receipt="MERGESHA")
    again = store.begin_operation("merge", "merge:task-1:sha1")
    assert again["created"] is False
    assert again["skip"] is True
    assert again["operation"]["status"] == "applied"
    assert again["operation"]["receipt"] == "MERGESHA"


def test_begin_operation_skip_true_when_reconciled(store):
    first = store.begin_operation("merge", "merge:task-1:sha1")
    store.set_operation_status(first["operation"]["id"], "reconciled", "landed")
    again = store.begin_operation("merge", "merge:task-1:sha1")
    assert again["skip"] is True


def test_begin_operation_different_keys_are_independent_rows(store):
    a = store.begin_operation("merge", "merge:task-1:sha1")
    b = store.begin_operation("merge", "merge:task-2:sha2")
    assert a["operation"]["id"] != b["operation"]["id"]


# -- complete_operation: rowcount-guarded executing -> applied ---------------

def test_complete_operation_transitions_executing_to_applied(store):
    op = store.begin_operation("grad_push", "grad:repo:base1:tip1")["operation"]
    ok = store.complete_operation(op["id"], receipt="deadbeef")
    assert ok is True
    row = store.get_operation(op["id"])
    assert row["status"] == "applied"
    assert row["receipt"] == "deadbeef"
    assert row["attempts"] == 2   # begin_operation starts attempts at 1


def test_complete_operation_second_call_is_a_guarded_no_op(store):
    op = store.begin_operation("merge", "merge:task-1:sha1")["operation"]
    assert store.complete_operation(op["id"], receipt="first") is True
    # already 'applied' — a second complete must not silently overwrite the receipt
    assert store.complete_operation(op["id"], receipt="second") is False
    assert store.get_operation(op["id"])["receipt"] == "first"


def test_complete_operation_unknown_id_returns_false(store):
    assert store.complete_operation(99999, receipt="x") is False


# -- set_operation_status: unguarded administrative transition ---------------

def test_set_operation_status_moves_planned_or_executing_to_reconciled(store):
    op = store.begin_operation("merge", "merge:task-1:sha1")["operation"]
    ok = store.set_operation_status(op["id"], "reconciled", "not landed")
    assert ok is True
    row = store.get_operation(op["id"])
    assert row["status"] == "reconciled"
    assert row["detail"] == "not landed"


def test_set_operation_status_can_move_applied_to_reconciled(store):
    """The merge-then-auto-revert self-heal path: the SAME row moves applied ->
    reconciled once the revert is known (Component B)."""
    op = store.begin_operation("merge", "merge:task-1:sha1")["operation"]
    store.complete_operation(op["id"], receipt="MERGESHA")
    ok = store.set_operation_status(op["id"], "reconciled", "auto-reverted -> REVERTSHA")
    assert ok is True
    row = store.get_operation(op["id"])
    assert row["status"] == "reconciled"
    assert row["receipt"] == "MERGESHA"          # receipt is untouched by set_operation_status
    assert "REVERTSHA" in row["detail"]


def test_set_operation_status_unknown_id_returns_false(store):
    assert store.set_operation_status(99999, "unknown", "x") is False


# -- get_operation_by_key / get_operation -------------------------------------

def test_get_operation_by_key_missing_returns_none(store):
    assert store.get_operation_by_key("merge:nope:nope") is None


def test_get_operation_missing_returns_none(store):
    assert store.get_operation(99999) is None


# -- operations(): status filter + ordering -----------------------------------

def test_operations_filtered_by_status(store):
    a = store.begin_operation("merge", "merge:task-1:sha1")["operation"]
    b = store.begin_operation("merge", "merge:task-2:sha2")["operation"]
    store.complete_operation(b["id"], receipt="x")

    executing = store.operations(status="executing")
    assert [r["id"] for r in executing] == [a["id"]]
    applied = store.operations(status="applied")
    assert [r["id"] for r in applied] == [b["id"]]


def test_operations_no_status_returns_all_oldest_first(store):
    a = store.begin_operation("merge", "merge:task-1:sha1")["operation"]
    b = store.begin_operation("grad_push", "grad:repo:base:tip")["operation"]
    rows = store.operations()
    assert [r["id"] for r in rows] == [a["id"], b["id"]]


def test_operations_empty_table_returns_empty_list(store):
    assert store.operations() == []
    assert store.operations(status="executing") == []


# -- idem_key uniqueness is a real DB constraint ------------------------------

def test_idem_key_is_unique_at_the_schema_level(store):
    store.begin_operation("merge", "merge:task-1:sha1")
    # A raw INSERT bypassing begin_operation's own IntegrityError handling must still
    # be rejected by the UNIQUE constraint itself.
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO operations(kind, idem_key, status, payload_json, attempts, "
            "created_at, updated_at) VALUES ('merge','merge:task-1:sha1','planned','{}',"
            "0,'x','x')")
