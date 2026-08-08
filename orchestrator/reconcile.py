"""orchestrator/reconcile.py — the crash-consistency reconciler (design: docs/plans/
2026-08-08-crash-consistency-design.md, Component C).

Runs at shift start (orchestrator/shift.py, between `reap_orphaned_shifts` and broker
receipt ingestion — it must see an `executing` row before the reaper turns it into the
lossy `'stale'`) and on demand (`factory reconcile [--dry-run]`).

For every `operations` row still `'planned'`/`'executing'` — a row Component B's own
begin/complete calls did NOT get to close out, i.e. a crash-orphaned intent, never a
normal in-flight one — this asks git (and, for the broker, receipts) what actually
happened and resolves the row, or escalates `'unknown'` when it honestly cannot tell.

Binding rules this module holds itself to:
  1. NEVER probe GitHub for issue state (armed mode has no `gh` credential at all; a
     wrong close is unrecoverable while a duplicate comment is merely cosmetic).
  2. NEVER silently resolve an unknown — an unanswerable row always escalates with the
     exact operator command to check by hand, never guessed in either direction.
  3. Bounded: at most `DEFAULT_LIMIT` rows per run; the remainder waits for the next run.
  4. Own STOP check — the shift-startup killswitch check (orchestrator/shift.py) runs
     LATER, after this sweep, so a halted factory must not run this on its own.
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional

from ..common import config, filelock, killswitch

DEFAULT_LIMIT = 50
_UNRESOLVED_STATUSES = ("planned", "executing")


# -- small git/process helpers -------------------------------------------------

def _git(runner, root: str, *args, timeout: int = 60):
    return runner(["git", "-C", root, *args], capture_output=True, text=True, timeout=timeout)


def _ok(res) -> bool:
    return getattr(res, "returncode", 1) == 0


def _lines(res) -> list:
    return [ln for ln in (getattr(res, "stdout", "") or "").splitlines() if ln.strip()]


def _unresolved_operations(store, limit: int) -> list:
    """Every row still `'planned'`/`'executing'`, oldest-first, capped at `limit` total
    (not per-status) — the bounded sweep."""
    rows = []
    for status in _UNRESOLVED_STATUSES:
        rows.extend(store.operations(status=status))
    rows.sort(key=lambda r: r["id"])
    return rows[:limit]


def _escalate(store, op: dict, detail: str) -> None:
    """The only path to 'unknown' — always paired with a durable, deduped escalation
    (reporting.factory_memory.record_graduation_failure's own pattern) carrying the
    EXACT operator command to verify by hand."""
    from ..reporting import factory_memory
    store.set_operation_status(op["id"], "unknown", detail[:2000])
    factory_memory.record_graduation_failure(
        store, error=detail, ref=f"reconcile:{op['kind']}-unknown")


# -- kind: merge ---------------------------------------------------------------

def _resolve_merge(store, op: dict, *, merge_repo: Optional[str], auto_branch: str,
                   runner) -> None:
    """Merges are self-describing (code_round.py's `Factory-Task:` trailer) and the
    auto-revert self-heal now carries a `Factory-Revert:` trailer (adapters/base.py) — so
    "did this land, and is it still standing" is answerable LOCALLY, no fetch, no
    credential. Landed (and not later reverted) -> reconciled, task repaired to 'done' if
    the crash lost that write. Not landed (or landed-then-reverted) -> reconciled, task
    returned to 'open' for redispatch — but only when the task is still visibly
    crash-interrupted ('claimed'/'in_progress'); an already closed-out task is untouched."""
    payload = op.get("payload") or {}
    task_id = payload.get("task_id") or ""
    grep_cmd = (f"git -C <factory/auto worktree> log {auto_branch} "
               f"--grep='Factory-Task: {task_id}' --format=%H")
    if not task_id or not merge_repo or not os.path.isdir(merge_repo):
        _escalate(store, op, f"cannot resolve merge op #{op['id']} — no reachable "
                            f"factory/auto worktree to check locally; verify manually: "
                            f"{grep_cmd}")
        return

    landed = _git(runner, merge_repo, "log", auto_branch, "--grep",
                 f"Factory-Task: {task_id}", "--fixed-strings", "--format=%H")
    if not _ok(landed):
        _escalate(store, op, f"git log failed reading {merge_repo!r} for op #{op['id']}; "
                            f"verify manually: {grep_cmd}")
        return

    merge_shas = _lines(landed)
    standing = None
    for sha in merge_shas:
        reverted = _git(runner, merge_repo, "log", auto_branch, "--grep",
                        f"Factory-Revert: {sha}", "--fixed-strings", "--format=%H")
        if _ok(reverted) and not _lines(reverted):
            standing = sha
            break

    task = store.get_task(task_id)
    crash_interrupted = bool(task) and task.get("status") in ("claimed", "in_progress")
    if standing:
        store.set_operation_status(op["id"], "reconciled", f"landed: {standing}")
        if crash_interrupted:
            store.set_task_status(task_id, "done", result=standing)
    else:
        why = "not landed" if not merge_shas else "landed then auto-reverted"
        store.set_operation_status(op["id"], "reconciled", why)
        if crash_interrupted:
            store.set_task_status(task_id, "open")


# -- kind: graduate_push (unarmed) ---------------------------------------------

def _matching_executing_approval(store, kind: str, base_sha: str, tip_sha: str) -> Optional[dict]:
    for row in store.pending_approvals(status="executing"):
        if row.get("kind") != kind:
            continue
        p = row.get("payload") or {}
        if p.get("base_sha") == base_sha and p.get("tip_sha") == tip_sha:
            return row
    return None


def _resolve_graduate_push(store, op: dict, *, root: Optional[str], base: str,
                           remote: str, runner) -> None:
    """Answerable with a read-only fetch + a local ancestor check (the same live-truth
    query the publication broker itself makes) — no push, no gh. Landed -> 'applied', and
    the matching pending_approvals row (if still 'executing') resolves 'approved'. Not
    landed -> 'reconciled', and the matching approval returns to 'pending' (retryable)."""
    base_sha, tip_sha = op.get("base_sha") or "", op.get("tip_sha") or ""
    target_ref = op.get("target_ref") or base
    cmd = (f"git -C {root or '<target root>'} fetch {remote} {target_ref} && "
          f"git -C {root or '<target root>'} merge-base --is-ancestor {tip_sha} "
          f"{remote}/{target_ref}; echo $?")
    if not root or not tip_sha or not target_ref:
        _escalate(store, op, f"cannot resolve graduate_push op #{op['id']} — missing "
                            f"root/tip_sha/base; verify manually: {cmd}")
        return

    try:
        with filelock.repo_lock(root):
            fetched = _git(runner, root, "fetch", remote, target_ref)
            if not _ok(fetched):
                _escalate(store, op, f"fetch failed for op #{op['id']}; verify manually: {cmd}")
                return
            anc = _git(runner, root, "merge-base", "--is-ancestor", tip_sha,
                      f"{remote}/{target_ref}")
    except filelock.LockBusyError:
        _escalate(store, op, f"push lock busy resolving op #{op['id']}; retry, or verify "
                            f"manually: {cmd}")
        return

    landed = _ok(anc)
    match = _matching_executing_approval(store, "graduation", base_sha, tip_sha)
    if landed:
        store.set_operation_status(op["id"], "applied",
                                  f"landed: ancestor of {remote}/{target_ref}")
        if match:
            store.resolve_approval(match["id"], "approved",
                                   note=f"reconciled: confirmed landed on {remote}/{target_ref}")
            store.record_operator_action(
                "reconcile-resolved", f"approval-{match['id']}",
                "graduation push confirmed landed via git (reconciler sweep)")
    else:
        store.set_operation_status(op["id"], "reconciled",
                                  f"not landed: not an ancestor of {remote}/{target_ref}")
        if match:
            store.unclaim_approval(match["id"])
            store.record_operator_action(
                "reconcile-unclaimed", f"approval-{match['id']}",
                "graduation push did not land — approval returned to pending "
                "(reconciler sweep)")


# -- kind: graduate_prepare (armed) --------------------------------------------

def _resolve_graduate_prepare(store, op: dict, *, root: Optional[str], base: str,
                              remote: str, receipts_dir: Optional[str], runner) -> None:
    """Receipt-first (the broker's own verdict is authoritative and needs no fetch), then
    git as a fallback (the tip may have landed via some other path). A row that is
    neither resolvable AND still legitimately in-flight (its paired approval is still
    'executing', not yet past its own TTL) is left untouched — not every unresolved row
    is an orphan; a broker that hasn't acted yet is normal, not a crash."""
    from ..reporting import envelope as envelope_mod

    idem_key = op.get("idem_key") or ""
    nonce = idem_key.split(":", 1)[1] if idem_key.startswith("gradprep:") else ""
    payload = op.get("payload") or {}
    cmd = (f"factory broker-receipts   # or inspect "
          f"{receipts_dir or '<receipts dir>'}/{nonce}.receipt.json")

    if nonce and receipts_dir and envelope_mod.has_receipt(receipts_dir, nonce):
        receipt = envelope_mod.read_receipt(receipts_dir, nonce) or {}
        status = receipt.get("status", "")
        if status == "pushed":
            sha = (receipt.get("receipt_sha") or "")[:9]
            store.set_operation_status(op["id"], "reconciled", f"broker pushed -> {sha}")
        elif status in ("rejected", "expired"):
            store.set_operation_status(op["id"], "reconciled", f"broker {status}")
        else:
            store.set_operation_status(op["id"], "reconciled",
                                      f"broker receipt status={status!r}")
        return

    tip_sha = op.get("tip_sha") or ""
    target_ref = op.get("target_ref") or base
    if root and tip_sha and target_ref:
        try:
            with filelock.repo_lock(root):
                fetched = _git(runner, root, "fetch", remote, target_ref)
                landed = False
                if _ok(fetched):
                    anc = _git(runner, root, "merge-base", "--is-ancestor", tip_sha,
                              f"{remote}/{target_ref}")
                    landed = _ok(anc)
        except filelock.LockBusyError:
            landed = False
        if landed:
            store.set_operation_status(
                op["id"], "reconciled",
                f"landed: ancestor of {remote}/{target_ref} (no receipt found, "
                f"confirmed via git)")
            return

    # Not resolvable yet — is it still a NORMAL in-flight wait, or a real orphan?
    approval_id = payload.get("approval_id")
    match = store.get_approval(approval_id) if approval_id else None
    if match and match.get("status") == "executing":
        return   # legitimately in-flight — the broker/receipt lifecycle still owns this

    _escalate(store, op, f"no receipt and git shows no landing for gradprep op #{op['id']} "
                        f"(nonce {nonce or '?'}); verify manually: {cmd}")


# -- kind: issue_sync -----------------------------------------------------------

def _resolve_issue_sync(store, op: dict) -> None:
    """NEVER probes GitHub (binding rule 1) — the (issue, commit) ledger (`issue_sync`
    table) or nothing. Present in the ledger -> reconciled; absent -> unknown, escalated
    (never silently assumed either posted or not)."""
    payload = op.get("payload") or {}
    idem_key = op.get("idem_key") or ""
    number = payload.get("issue_number")
    sha = payload.get("sha") or op.get("tip_sha") or ""
    if number is None and idem_key.startswith("issue:"):
        parts = idem_key.split(":")
        if len(parts) == 3:
            try:
                number = int(parts[1])
            except ValueError:
                number = None
            sha = sha or parts[2]
    if number is not None and sha and store.issue_sync_seen(int(number), sha):
        store.set_operation_status(op["id"], "reconciled",
                                  "already recorded in the issue_sync ledger")
        return
    _escalate(store, op, f"issue_sync op #{op['id']} not found in the issue_sync ledger — "
                        f"the reconciler NEVER probes GitHub; verify manually: "
                        f"gh issue view {number if number is not None else '<number>'} "
                        f"-R <repo>")


# -- the sweep -------------------------------------------------------------------

def run_reconcile(store, *, root: Optional[str] = None, auto_branch: str = "factory/auto",
                  base: Optional[str] = None, remote: str = "origin",
                  merge_repo: Optional[str] = None, receipts_dir: Optional[str] = None,
                  runner=subprocess.run, limit: int = DEFAULT_LIMIT,
                  dry_run: bool = False) -> dict:
    """The reconciler sweep. Own STOP check (see the module docstring's binding rule 4).
    `root`/`base`/`merge_repo`/`receipts_dir` default to the live config/adapter/spool
    when not given (production); tests inject them directly against real tmp-dir git
    repos, bypassing config entirely.

    `dry_run` resolves nothing — returns the bounded row list that WOULD be examined
    (id/kind/idem_key/status only), for `factory reconcile --dry-run`'s preview.

    Returns `{'action': 'halted'|'dry_run'|'reconciled', 'examined': int,
    'resolved': [...], 'unknown': [...]}` (resolved/unknown are the post-resolution
    operation rows)."""
    if killswitch.is_halted():
        return {"action": "halted", "examined": 0, "resolved": [], "unknown": []}

    if root is None:
        try:
            root = config.get_adapter().entry()[0]
        except Exception:  # noqa: BLE001 — no adapter configured (early bootstrap, tests)
            root = None
    if base is None:
        try:
            base = config.target_config().get("base_branch") or ""
        except Exception:  # noqa: BLE001
            base = ""
    if merge_repo is None and root:
        wt = root.rstrip("/") + ".factory-auto"
        merge_repo = wt if os.path.isdir(os.path.join(wt, ".git")) else root
    if receipts_dir is None:
        from ..common import paths
        try:
            spool_root = (config.load_config().get("autonomy") or {}).get(
                "broker_spool_root") or None
        except Exception:  # noqa: BLE001
            spool_root = None
        receipts_dir = paths.broker_receipts_dir(spool_root)

    rows = _unresolved_operations(store, limit)
    if dry_run:
        return {"action": "dry_run", "examined": len(rows),
               "rows": [{"id": r["id"], "kind": r["kind"], "idem_key": r["idem_key"],
                        "status": r["status"]} for r in rows]}

    resolved, unknown = [], []
    for op in rows:
        before = op["status"]
        kind = op.get("kind")
        try:
            if kind == "merge":
                _resolve_merge(store, op, merge_repo=merge_repo, auto_branch=auto_branch,
                              runner=runner)
            elif kind == "graduate_push":
                _resolve_graduate_push(store, op, root=root, base=base, remote=remote,
                                      runner=runner)
            elif kind == "graduate_prepare":
                _resolve_graduate_prepare(store, op, root=root, base=base, remote=remote,
                                         receipts_dir=receipts_dir, runner=runner)
            elif kind == "issue_sync":
                _resolve_issue_sync(store, op)
            else:
                _escalate(store, op, f"operation #{op['id']} has an unrecognized kind "
                                    f"{kind!r} — the reconciler cannot resolve it")
        except Exception as e:  # noqa: BLE001 — a resolver blow-up escalates, never crashes
            _escalate(store, op, f"reconciler blew up resolving op #{op['id']} ({kind}): {e}")

        after = store.get_operation(op["id"])
        if after is None:
            continue
        if after["status"] == "unknown":
            unknown.append(after)
        elif after["status"] != before:
            resolved.append(after)

    return {"action": "reconciled", "examined": len(rows), "resolved": resolved,
           "unknown": unknown}
