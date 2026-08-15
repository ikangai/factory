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


# -- kind: merge, status 'applied' — the round crashed AFTER the receipt ----------
# Drill 1 (2026-08-13) found this half of the merge window unreconcilable: 'applied' is
# written the instant the merge lands, but the round runs on through the whole re-baseline
# before keeping or reverting it, and _UNRESOLVED_STATUSES never swept 'applied'.

def _applied_merge(store, task_id, receipt, *, task_status="in_progress", kind="merge"):
    store.add_task(task_id, "do the thing", source="human")
    store.set_task_status(task_id, task_status)
    op = store.begin_operation(kind, f"merge:{task_id}:CANDTIP", tip_sha="CANDTIP",
                               payload={"task_id": task_id})["operation"]
    store.complete_operation(op["id"], receipt=receipt)     # -> 'applied'
    return op


def test_applied_merge_still_standing_escalates_as_unverified(tmp_path, store):
    """The re-baseline never ran, so nobody knows whether it would have kept or reverted
    this merge. That is exactly what the crash destroyed — so escalate, never guess (binding
    rule 2), and never mark the task done: that would claim verified work."""
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    merge_sha = _commit(repo, "b.txt", "2", message="factory: cand")   # no trailer at all
    op = _applied_merge(store, "task-ap1", merge_sha)

    result = reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main", root=None)

    assert result["examined"] == 1
    row = store.get_operation(op["id"])
    assert row["status"] == "unknown"
    assert "UNVERIFIED" in row["detail"] and merge_sha[:12] in row["detail"]
    assert store.get_task("task-ap1")["status"] != "done"
    # Escalated PER MERGE, not per kind: a second unverified merge must not be swallowed by
    # the first one's backlog task, which would hide its sha.
    escalations = [t for t in store.list_tasks(status="open")
                   if t.get("source_ref") == f"reconcile:merge-unverified:{merge_sha[:12]}"]
    assert len(escalations) == 1


def test_applied_merge_that_was_reverted_reconciles_and_reopens_the_task(tmp_path, store):
    """Both the merge and its `Factory-Revert:` trailer are in git, so the fate IS known —
    resolve it and hand the task back for redispatch, with no escalation to read."""
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    merge_sha = _commit(repo, "b.txt", "2", message="factory: cand")
    _commit(repo, "b.txt", "1", message=f"Revert cand\n\nFactory-Revert: {merge_sha}")
    op = _applied_merge(store, "task-ap2", merge_sha)

    reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main", root=None)

    row = store.get_operation(op["id"])
    assert row["status"] == "reconciled"
    assert "auto-reverted" in row["detail"]
    assert store.get_task("task-ap2")["status"] == "open"
    assert not [t for t in store.list_tasks(status="open")
                if "reconcile:" in (t.get("source_ref") or "")]


def test_applied_merge_whose_task_is_done_is_never_swept(tmp_path, store):
    """The no-migration guard. Rows written by CLEAN rounds before this fix sit at 'applied'
    forever; their task closed out normally, and re-examining them would escalate history as
    if it had crashed."""
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    merge_sha = _commit(repo, "b.txt", "2", message="factory: cand")
    op = _applied_merge(store, "task-ap3", merge_sha, task_status="done")

    result = reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main", root=None)

    assert result["examined"] == 0
    assert store.get_operation(op["id"])["status"] == "applied"


def test_applied_merge_receipt_not_on_the_branch_escalates(tmp_path, store):
    """The receipt is a real commit, just not on the auto branch — the branch moved under
    it. A definite "no", and still not something to resolve in either direction."""
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    _git(repo, "checkout", "-q", "-b", "side")
    elsewhere = _commit(repo, "c.txt", "3", message="on another branch")
    _git(repo, "checkout", "-q", "main")
    op = _applied_merge(store, "task-ap4", elsewhere)

    reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main", root=None)

    row = store.get_operation(op["id"])
    assert row["status"] == "unknown"
    assert "not on main" in row["detail"]


def test_applied_merge_with_an_unresolvable_sha_escalates_as_git_failing(tmp_path, store):
    """A sha git does not even have an object for makes `merge-base --is-ancestor` exit 128,
    NOT 1 — git failing to answer, not answering "no". Both escalate, but only a genuine
    exit 1 may be reported as a definite not-on-the-branch."""
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    op = _applied_merge(store, "task-ap4b", "0" * 40)

    reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main", root=None)

    row = store.get_operation(op["id"])
    assert row["status"] == "unknown"
    assert "could not confirm" in row["detail"]


def test_applied_non_merge_rows_are_left_alone(tmp_path, store):
    """'applied' is the TERMINAL success state for graduate_push (_resolve_graduate_push
    sets it) — sweeping those would re-open settled publications."""
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    op = _applied_merge(store, "task-ap5", _head(repo), kind="graduate_push")

    result = reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main", root=None)

    assert result["examined"] == 0
    assert store.get_operation(op["id"])["status"] == "applied"


def test_resolve_merge_not_landed_returns_task_to_open(tmp_path, store):
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")   # no Factory-Task commit at all

    store.add_task("task-2", "do another thing", source="human")
    store.set_task_status("task-2", "in_progress")
    store.begin_operation("merge", "merge:task-2:CANDTIP", payload={"task_id": "task-2"})

    reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main")
    op = store.get_operation_by_key("merge:task-2:CANDTIP")
    # 'failed', not 'reconciled': the effect did NOT happen, so this row must stay
    # retryable — 'reconciled' is what begin_operation treats as "already done" and
    # would suppress the operator's retry (Phase 2 review, F2).
    assert op["status"] == "failed"
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
    # 'failed', not 'reconciled': the effect did NOT happen, so this row must stay
    # retryable — 'reconciled' is what begin_operation treats as "already done" and
    # would suppress the operator's retry (Phase 2 review, F2).
    assert op["status"] == "failed"
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
    # 'failed', not 'reconciled': the effect did NOT happen, so this row must stay
    # retryable — 'reconciled' is what begin_operation treats as "already done" and
    # would suppress the operator's retry (Phase 2 review, F2).
    assert op["status"] == "failed"
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


def _spool_with_envelope(tmp_path, nonce):
    """A live spool whose outbox actually holds `nonce`'s envelope — what "the broker
    hasn't acted yet" MEANS. `receipts/` names the spool; the outbox is its sibling."""
    outbox = tmp_path / "spool" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / f"{nonce}.json").write_text("{}")
    return str(tmp_path / "spool" / "receipts")


def test_resolve_graduate_prepare_still_in_flight_is_left_alone(tmp_path, store):
    """No receipt, git shows no landing, the paired approval is still 'executing' AND its
    envelope is sitting in the outbox — the broker simply hasn't acted yet. Not every
    unresolved row is an orphan.

    The envelope is what makes this in-flight rather than stuck: an 'executing' approval
    with no envelope anywhere is the crashed-before-the-write state, resolved below."""
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

    result = reconcile.run_reconcile(store, root=root, base="main",
                                     receipts_dir=_spool_with_envelope(tmp_path, "nonceD"))
    row = store.get_operation(op["id"])
    assert row["status"] == "executing"          # untouched — still legitimately in flight
    assert row not in result["resolved"] and row not in result["unknown"]
    assert store.get_approval(aid)["status"] == "executing"


def test_resolve_graduate_prepare_never_written_fails_and_frees_the_approval(tmp_path, store):
    """The crash landed between the intent row and `write_envelope`, so no envelope exists
    and the broker can never produce a receipt.

    Drill 1 (2026-08-14): this used to be left alone by the in-flight guard, so the approval
    sat 'executing' until the orphan reaper aged it out — and `propose_graduation` refuses
    while one is 'executing', stalling graduation for up to the envelope TTL on a state that
    was answerable immediately. 'failed', not 'reconciled': the prepare did NOT happen, so
    the retry must not be suppressed by `begin_operation`'s skip."""
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")
    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)
    tip_sha = _commit(root, "b.txt", "1", message="local only")

    aid = store.add_pending_approval("graduation", {"n_commits": 1})
    store.claim_approval(aid)
    op = store.begin_operation("graduate_prepare", "gradprep:nonceF",
                               target_ref="main", base_sha=old_sha, tip_sha=tip_sha,
                               payload={"approval_id": aid})["operation"]

    # A live spool (it exists — the prepare pushes to a bare repo inside it before the
    # envelope is built) whose outbox holds nothing for this nonce.
    (tmp_path / "spool").mkdir()
    reconcile.run_reconcile(store, root=root, base="main",
                            receipts_dir=str(tmp_path / "spool" / "receipts"))

    row = store.get_operation(op["id"])
    assert row["status"] == "failed"
    assert "never completed" in row["detail"] and "nonceF" in row["detail"]
    assert store.get_approval(aid)["status"] == "pending"     # graduation unblocked at once


def test_resolve_graduate_prepare_absent_spool_root_concludes_nothing(tmp_path, store):
    """A spool root that does not exist is a MISCONFIGURED root, not evidence that no
    envelope was written — the F1 both-halves-same-root bug would otherwise read every
    armed prepare as never-written and unclaim live approvals."""
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")
    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)
    tip_sha = _commit(root, "b.txt", "1", message="local only")

    aid = store.add_pending_approval("graduation", {"n_commits": 1})
    store.claim_approval(aid)
    op = store.begin_operation("graduate_prepare", "gradprep:nonceG",
                               target_ref="main", base_sha=old_sha, tip_sha=tip_sha,
                               payload={"approval_id": aid})["operation"]

    reconcile.run_reconcile(store, root=root, base="main",
                            receipts_dir=str(tmp_path / "nowhere" / "receipts"))

    assert store.get_operation(op["id"])["status"] == "executing"   # nothing concluded
    assert store.get_approval(aid)["status"] == "executing"


def test_resolve_graduate_prepare_truly_orphaned_with_a_live_spool_fails(tmp_path, store):
    """No receipt, no landing, no envelope and no approval to explain the wait. This used
    to escalate 'unknown'; with the outbox consulted it is determinable — the prepare never
    completed — so it resolves 'failed' (retryable) instead of asking a human to look."""
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")

    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)
    tip_sha = _commit(root, "b.txt", "1", message="orphaned")

    store.begin_operation("graduate_prepare", "gradprep:nonceE",
                          target_ref="main", base_sha=old_sha, tip_sha=tip_sha,
                          payload={"approval_id": None})
    (tmp_path / "spool").mkdir()
    reconcile.run_reconcile(store, root=root, base="main",
                            receipts_dir=str(tmp_path / "spool" / "receipts"))
    op = store.get_operation_by_key("gradprep:nonceE")
    assert op["status"] == "failed"


def test_resolve_graduate_prepare_orphaned_and_unprovable_still_escalates(tmp_path, store):
    """The escalation path survives for the case that IS genuinely unanswerable: no
    approval, and no spool to consult either."""
    origin = _init(str(tmp_path / "origin"))
    old_sha = _commit(origin, "a.txt", "1", message="root")

    root = str(tmp_path / "root")
    subprocess.run(["git", "clone", "-q", origin, root], check=True,
                   capture_output=True, text=True)
    tip_sha = _commit(root, "b.txt", "1", message="orphaned")

    store.begin_operation("graduate_prepare", "gradprep:nonceH",
                          target_ref="main", base_sha=old_sha, tip_sha=tip_sha,
                          payload={"approval_id": None})
    reconcile.run_reconcile(store, root=root, base="main",
                            receipts_dir=str(tmp_path / "nowhere" / "receipts"))
    assert store.get_operation_by_key("gradprep:nonceH")["status"] == "unknown"


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


# ==========================================================================================
# Phase 2 adversarial-review regressions (F1/F2/F3). Each of these FAILED before its fix.
# ==========================================================================================
def test_landed_merge_is_repaired_even_after_the_shift_reaper_requeued_the_task(tmp_path, store):
    """F1 — the headline crash repair, in the WIRED order. `reap_orphaned_shifts` runs at
    shift.py:41, BEFORE this sweep, and requeues the crashed shift's in-flight tasks to
    'open'. The original guard repaired only 'claimed'/'in_progress' tasks, so by the time
    the reconciler ran it was always False: the merge sat in git, the task went back on the
    backlog, and the factory rebuilt work it had already landed — exactly the consequence
    the design set out to prevent. git is canonical for what landed; the store follows."""
    repo = _init(str(tmp_path / "auto"))
    _commit(repo, "a.txt", "1", message="root")
    merge_sha = _commit(repo, "b.txt", "2",
                        message="factory: cand\n\nFactory-Task: task-f1")

    shift_id = store.start_shift(token_budget=1000)
    store.add_task("task-f1", "a task", source="worker")
    store.set_task_status("task-f1", "in_progress", shift_id=shift_id)
    store.begin_operation("merge", "merge:task-f1:CANDTIP", tip_sha="CANDTIP",
                          payload={"task_id": "task-f1"}, shift_id=shift_id)

    store.reap_orphaned_shifts()                       # the production order
    assert store.get_task("task-f1")["status"] == "open"

    reconcile.run_reconcile(store, merge_repo=repo, auto_branch="main")

    task = store.get_task("task-f1")
    assert task["status"] == "done", "already-merged work would be redispatched"
    assert task["result"] == merge_sha


def test_a_not_landed_operation_does_not_suppress_the_operators_retry(store):
    """F2 — `begin_operation` treats 'reconciled' as a terminal success (skip=True), but the
    reconciler wrote 'reconciled' for NOT-landed too. A graduation push that never happened
    was therefore permanently suppressed on retry AND reported as 'synced'/'approved' — the
    machinery lying about an effect, which this phase forbids. Not-landed is now 'failed'."""
    key = "grad:me/repo:aaa:bbb"
    first = store.begin_operation(kind="graduate_push", idem_key=key, target_ref="main",
                                  base_sha="aaa", tip_sha="bbb")
    store.set_operation_status(first["operation"]["id"], "failed",
                               "not landed: not an ancestor of origin/main")

    retry = store.begin_operation(kind="graduate_push", idem_key=key, target_ref="main",
                                  base_sha="aaa", tip_sha="bbb")
    assert retry["skip"] is False, "a push that never landed must stay retryable"


def test_an_applied_operation_still_suppresses_a_repeat(store):
    """The other half of F2: the idempotency win must survive the fix."""
    key = "grad:me/repo:ccc:ddd"
    first = store.begin_operation(kind="graduate_push", idem_key=key, target_ref="main",
                                  base_sha="ccc", tip_sha="ddd")
    store.complete_operation(first["operation"]["id"], receipt="pushedsha")

    again = store.begin_operation(kind="graduate_push", idem_key=key, target_ref="main",
                                  base_sha="ccc", tip_sha="ddd")
    assert again["skip"] is True, "an effect that DID happen must not be repeated"


def test_an_unanswerable_ancestor_check_escalates_instead_of_asserting_not_landed(store):
    """F3 — `merge-base --is-ancestor` answers with exit 0/1. Exit 128 ('not a valid object
    name' — an unknown tip after restoring a snapshot taken on another clone, a missing
    tracking ref) means the question could not be ASKED. Resolving that to 'not landed' is
    a guess, and it unclaimed the approval on the strength of it."""
    class _R:
        def __init__(self, rc, err=""):
            self.returncode, self.stdout, self.stderr = rc, "", err

    def fake_runner(argv, **kw):
        if "--is-ancestor" in argv:
            return _R(128, "fatal: Not a valid object name origin/main")
        return _R(0)

    op = store.begin_operation("graduate_push", "grad:z:1:2", target_ref="main",
                               base_sha="1", tip_sha="2")["operation"]
    reconcile._resolve_graduate_push(store, op, root=".", base="main",
                                     remote="origin", runner=fake_runner)

    row = store.get_operation(op["id"])
    assert row["status"] == "unknown", "an unanswerable question must not become an answer"
    assert "exited 128" in row["detail"]
