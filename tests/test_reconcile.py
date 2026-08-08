"""orchestrator/reconcile.py — the crash-consistency reconciler (Component C, design:
docs/plans/2026-08-08-crash-consistency-design.md). Hermetic: every git-touching test
uses a REAL local repo under tmp_path (file:// remotes where a remote is needed) — never
the network, never a fake runner standing in for real git semantics.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from factory.common import killswitch
from factory.orchestrator import reconcile
from factory.reporting import envelope


# -- git helpers ----------------------------------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, text=True).stdout


def _init(path):
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return path


def _commit(repo, name, content, *, message):
    with open(os.path.join(repo, name), "w") as fh:
        fh.write(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").strip()


# -- kind: merge ------------------------------------------------------------------

def test_resolve_merge_landed_reconciles_and_repairs_the_task(tmp_path, store):
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    merge_sha = _commit(repo, "b.txt", "2", message="factory: cand\n\nFactory-Task: task-1")

    store.add_task("task-1", "do the thing", source="human")
    store.set_task_status("task-1", "in_progress")
    op = store.begin_operation("merge", "merge:task-1:CANDTIP", tip_sha="CANDTIP",
                               payload={"task_id": "task-1"})["operation"]

    result = reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main",
                                     root=None)
    assert result["action"] == "reconciled"
    row = store.get_operation(op["id"])
    assert row["status"] == "reconciled"
    assert merge_sha in row["detail"]
    assert store.get_task("task-1")["status"] == "done"
    assert store.get_task("task-1")["result"] == merge_sha


def test_resolve_merge_not_landed_returns_task_to_open(tmp_path, store):
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")   # no Factory-Task commit at all

    store.add_task("task-2", "do another thing", source="human")
    store.set_task_status("task-2", "in_progress")
    store.begin_operation("merge", "merge:task-2:CANDTIP", payload={"task_id": "task-2"})

    reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main")
    op = store.get_operation_by_key("merge:task-2:CANDTIP")
    assert op["status"] == "reconciled"
    assert "not landed" in op["detail"]
    assert store.get_task("task-2")["status"] == "open"


def test_resolve_merge_landed_then_reverted_returns_task_to_open(tmp_path, store):
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    merge_sha = _commit(repo, "b.txt", "2", message="factory: cand\n\nFactory-Task: task-3")
    _commit(repo, "b.txt", "1", message=f"Revert\n\nFactory-Revert: {merge_sha}")

    store.add_task("task-3", "x", source="human")
    store.set_task_status("task-3", "in_progress")
    store.begin_operation("merge", "merge:task-3:CANDTIP", payload={"task_id": "task-3"})

    reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main")
    op = store.get_operation_by_key("merge:task-3:CANDTIP")
    assert op["status"] == "reconciled"
    assert "reverted" in op["detail"]
    assert store.get_task("task-3")["status"] == "open"


def test_resolve_merge_does_not_touch_an_already_closed_out_task(tmp_path, store):
    """The task was already closed out normally (no crash) — the reconciler only ever
    repairs a task still visibly 'claimed'/'in_progress'."""
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")

    store.add_task("task-4", "x", source="human")
    store.set_task_status("task-4", "blocked", result="discarded (tests)")
    store.begin_operation("merge", "merge:task-4:CANDTIP", payload={"task_id": "task-4"})

    reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main")
    assert store.get_task("task-4")["status"] == "blocked"   # untouched


def test_resolve_merge_missing_worktree_escalates_unknown(store):
    store.add_task("task-5", "x", source="human")
    store.begin_operation("merge", "merge:task-5:CANDTIP", payload={"task_id": "task-5"})

    result = reconcile.run_reconcile(store, merge_repo="/definitely/does/not/exist",
                                     auto_branch="main")
    op = store.get_operation_by_key("merge:task-5:CANDTIP")
    assert op["status"] == "unknown"
    assert "git -C" in op["detail"]
    assert len(result["unknown"]) == 1
    # Escalation lands a durable backlog task + learning (factory_memory.record_graduation_failure)
    backlog = [t for t in store.list_tasks() if t.get("source_ref") == "reconcile:merge-unknown"]
    assert len(backlog) == 1


# -- kind: graduate_push -----------------------------------------------------------

def test_resolve_graduate_push_landed_applies_and_resolves_the_approval(tmp_path, store):
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")
    tip_sha = _commit(origin, "a.txt", "2", message="graduated")   # already ON origin

    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)

    aid = store.add_pending_approval("graduation", {"base_sha": old_sha, "tip_sha": tip_sha})
    store.claim_approval(aid)
    op = store.begin_operation("graduate_push", f"grad:o/r:{old_sha}:{tip_sha}",
                               target_ref="main", base_sha=old_sha, tip_sha=tip_sha)["operation"]

    result = reconcile.run_reconcile(store, root=root, base="main", merge_repo=root)
    row = store.get_operation(op["id"])
    assert row["status"] == "applied"
    assert "landed" in row["detail"]
    assert store.get_approval(aid)["status"] == "approved"
    assert len(result["resolved"]) == 1


def test_resolve_graduate_push_not_landed_unclaims_the_approval(tmp_path, store):
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")

    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)
    # A commit that only exists locally in `root` — never pushed (simulating a crash
    # before the push, or a push that never reached origin).
    never_pushed_sha = _commit(root, "b.txt", "1", message="local only")

    aid = store.add_pending_approval("graduation",
                                     {"base_sha": old_sha, "tip_sha": never_pushed_sha})
    store.claim_approval(aid)
    store.begin_operation("graduate_push", f"grad:o/r:{old_sha}:{never_pushed_sha}",
                          target_ref="main", base_sha=old_sha, tip_sha=never_pushed_sha)

    reconcile.run_reconcile(store, root=root, base="main")
    op = store.get_operation_by_key(f"grad:o/r:{old_sha}:{never_pushed_sha}")
    assert op["status"] == "reconciled"
    assert "not landed" in op["detail"]
    assert store.get_approval(aid)["status"] == "pending"   # returned, retryable


def test_resolve_graduate_push_missing_root_escalates_unknown(store, monkeypatch):
    """`root=None` alone is not enough to prove this — run_reconcile falls back to the
    live config's adapter when root isn't given, which would pull in a REAL repo path on
    a dev machine. Force the fallback itself to fail (no adapter resolvable) to test the
    genuinely-unresolvable case hermetically."""
    def _no_adapter():
        raise RuntimeError("no adapter configured")
    monkeypatch.setattr(reconcile.config, "get_adapter", _no_adapter)
    store.begin_operation("graduate_push", "grad:o/r:old:tip", base_sha="old", tip_sha="tip")
    reconcile.run_reconcile(store, root=None)
    op = store.get_operation_by_key("grad:o/r:old:tip")
    assert op["status"] == "unknown"


# -- kind: graduate_prepare --------------------------------------------------------

def test_resolve_graduate_prepare_receipt_pushed(tmp_path, store):
    receipts_dir = str(tmp_path / "receipts")
    envelope.write_receipt(nonce="nonceA", status="pushed", receipts_dir=receipts_dir,
                           receipt_sha="cafef00d")
    op = store.begin_operation("graduate_prepare", "gradprep:nonceA",
                               payload={"approval_id": None})["operation"]

    reconcile.run_reconcile(store, receipts_dir=receipts_dir, root=None)
    row = store.get_operation(op["id"])
    assert row["status"] == "reconciled"
    assert "cafef00d" in row["detail"]


def test_resolve_graduate_prepare_receipt_rejected(tmp_path, store):
    receipts_dir = str(tmp_path / "receipts")
    envelope.write_receipt(nonce="nonceB", status="rejected", receipts_dir=receipts_dir,
                           detail="base moved")
    store.begin_operation("graduate_prepare", "gradprep:nonceB")

    reconcile.run_reconcile(store, receipts_dir=receipts_dir, root=None)
    op = store.get_operation_by_key("gradprep:nonceB")
    assert op["status"] == "reconciled"
    assert "rejected" in op["detail"]


def test_resolve_graduate_prepare_no_receipt_but_landed_via_git(tmp_path, store):
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")
    tip_sha = _commit(origin, "a.txt", "2", message="landed anyway")

    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)

    receipts_dir = str(tmp_path / "receipts")   # empty — no receipt written
    store.begin_operation("graduate_prepare", "gradprep:nonceC",
                          target_ref="main", base_sha=old_sha, tip_sha=tip_sha)

    reconcile.run_reconcile(store, root=root, base="main", receipts_dir=receipts_dir)
    op = store.get_operation_by_key("gradprep:nonceC")
    assert op["status"] == "reconciled"
    assert "confirmed via git" in op["detail"]


def test_resolve_graduate_prepare_still_in_flight_is_left_alone(tmp_path, store):
    """No receipt, git shows no landing, but the paired approval is still legitimately
    'executing' (the broker hasn't acted yet) — not every unresolved row is an orphan."""
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")

    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)
    tip_sha = _commit(root, "b.txt", "1", message="local only, not pushed yet")

    aid = store.add_pending_approval("graduation", {"n_commits": 1})
    store.claim_approval(aid)   # 'executing'
    op = store.begin_operation("graduate_prepare", "gradprep:nonceD",
                               target_ref="main", base_sha=old_sha, tip_sha=tip_sha,
                               payload={"approval_id": aid})["operation"]

    receipts_dir = str(tmp_path / "receipts")
    result = reconcile.run_reconcile(store, root=root, base="main",
                                     receipts_dir=receipts_dir)
    row = store.get_operation(op["id"])
    assert row["status"] == "executing"          # untouched — still legitimately in flight
    assert row not in result["resolved"] and row not in result["unknown"]


def test_resolve_graduate_prepare_truly_orphaned_escalates_unknown(tmp_path, store):
    """No receipt, git shows no landing, and no live approval to explain the wait."""
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")

    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)
    tip_sha = _commit(root, "b.txt", "1", message="orphaned")

    store.begin_operation("graduate_prepare", "gradprep:nonceE",
                          target_ref="main", base_sha=old_sha, tip_sha=tip_sha,
                          payload={"approval_id": None})
    receipts_dir = str(tmp_path / "receipts")
    reconcile.run_reconcile(store, root=root, base="main", receipts_dir=receipts_dir)
    op = store.get_operation_by_key("gradprep:nonceE")
    assert op["status"] == "unknown"


# -- kind: issue_sync ---------------------------------------------------------------

def test_resolve_issue_sync_present_in_ledger_reconciles(store):
    store.record_issue_sync(40, "sha1", "comment", "https://gh/x")
    store.begin_operation("issue_sync", "issue:40:sha1",
                          payload={"issue_number": 40, "sha": "sha1"})
    reconcile.run_reconcile(store, root=None)
    op = store.get_operation_by_key("issue:40:sha1")
    assert op["status"] == "reconciled"


def test_resolve_issue_sync_absent_from_ledger_escalates_never_probes_github(store):
    store.begin_operation("issue_sync", "issue:41:sha2",
                          payload={"issue_number": 41, "sha": "sha2"})
    reconcile.run_reconcile(store, root=None)
    op = store.get_operation_by_key("issue:41:sha2")
    assert op["status"] == "unknown"
    assert "NEVER probes GitHub" in op["detail"]
    assert "gh issue view" in op["detail"]


# -- the sweep: STOP, bounding, dry-run, untouched-terminal-rows ---------------------

def test_run_reconcile_honors_stop(store, monkeypatch):
    monkeypatch.setattr(killswitch, "is_halted", lambda: True)
    store.begin_operation("issue_sync", "issue:1:a", payload={"issue_number": 1, "sha": "a"})
    result = reconcile.run_reconcile(store)
    assert result["action"] == "halted"
    assert store.get_operation_by_key("issue:1:a")["status"] == "executing"   # untouched


def test_run_reconcile_dry_run_resolves_nothing(store):
    op = store.begin_operation("issue_sync", "issue:1:a",
                               payload={"issue_number": 1, "sha": "a"})["operation"]
    store.record_issue_sync(1, "a", "comment")   # WOULD reconcile if run for real

    result = reconcile.run_reconcile(store, dry_run=True)
    assert result["action"] == "dry_run"
    assert result["examined"] == 1
    assert result["rows"][0]["id"] == op["id"]
    assert store.get_operation(op["id"])["status"] == "executing"   # nothing changed


def test_run_reconcile_never_touches_applied_or_reconciled_rows(store):
    a = store.begin_operation("merge", "merge:t1:s1")["operation"]
    store.complete_operation(a["id"], receipt="done")
    b = store.begin_operation("merge", "merge:t2:s2")["operation"]
    store.set_operation_status(b["id"], "reconciled", "already handled")

    result = reconcile.run_reconcile(store, root=None)
    assert result["examined"] == 0
    assert store.get_operation(a["id"])["status"] == "applied"
    assert store.get_operation(b["id"])["status"] == "reconciled"


def test_run_reconcile_bounded_to_limit(store):
    for i in range(5):
        store.begin_operation("issue_sync", f"issue:{i}:sha{i}",
                              payload={"issue_number": i, "sha": f"sha{i}"})
        store.record_issue_sync(i, f"sha{i}", "comment")   # every row IS resolvable

    result = reconcile.run_reconcile(store, root=None, limit=2)
    assert result["examined"] == 2
    resolved_statuses = [r["status"] for r in result["resolved"]]
    assert resolved_statuses == ["reconciled", "reconciled"]
    # the oldest 2 rows (lowest ids) were the ones examined
    still_open = [op for op in store.operations(status="executing")]
    assert len(still_open) == 3


def test_run_reconcile_unrecognized_kind_escalates(store):
    store.begin_operation("mystery_kind", "mystery:1")
    result = reconcile.run_reconcile(store, root=None)
    assert len(result["unknown"]) == 1
    assert result["unknown"][0]["status"] == "unknown"
