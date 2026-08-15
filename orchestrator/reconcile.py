"""orchestrator/reconcile.py — the crash-consistency reconciler (design: docs/plans/
2026-08-08-crash-consistency-design.md, Component C).

Runs at shift start (orchestrator/shift.py, between `reap_orphaned_shifts` and broker
receipt ingestion — it must see an `executing` row before the reaper turns it into the
lossy `'stale'`) and on demand (`factory reconcile [--dry-run]`).

For every `operations` row still `'planned'`/`'executing'` — a row Component B's own
begin/complete calls did NOT get to close out, i.e. a crash-orphaned intent, never a
normal in-flight one — this asks git (and, for the broker, receipts) what actually
happened and resolves the row, or escalates `'unknown'` when it honestly cannot tell.

Plus one row shape that is NOT identified by status alone: a `merge` row sitting at
`'applied'` whose task never closed out. `'applied'` is written when the merge lands, but
the round continues through the whole re-baseline before the merge is kept or reverted, so
a crash in that stretch leaves a row that looks finished and is not — see
`_crashed_applied_merges`.

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


def _escalate(store, op: dict, detail: str, *, ref: Optional[str] = None) -> None:
    """The only path to 'unknown' — always paired with a durable, deduped escalation
    (reporting.factory_memory.record_graduation_failure's own pattern) carrying the
    EXACT operator command to verify by hand.

    `ref` overrides the per-KIND dedup marker. The default groups every unknown of a kind
    into one backlog task, which is right for a repeated infrastructure failure but wrong
    when each row names a DIFFERENT artifact the operator must look at by hand: the second
    unverified merge would be swallowed by the first one's task and its sha never shown.
    Callers in that situation pass a ref scoped to the artifact."""
    from ..reporting import factory_memory
    store.set_operation_status(op["id"], "unknown", detail[:2000])
    factory_memory.record_graduation_failure(
        store, error=detail, ref=ref or f"reconcile:{op['kind']}-unknown")


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
    status = (task or {}).get("status")
    if standing:
        # LANDED. Repair the task record whenever it disagrees with git — NOT only when
        # it is still claimed/in_progress. `reap_orphaned_shifts` (shift.py:41) runs
        # BEFORE this sweep and has already requeued the crashed shift's tasks to 'open',
        # so a claimed/in_progress-only guard is False by the time we get here and the
        # headline repair never fires: the merge sits in git, the task goes back on the
        # backlog, and the factory rebuilds work it already landed (Phase 2 review, F1).
        # git is canonical for "what landed" — the store follows it.
        store.set_operation_status(op["id"], "reconciled", f"landed: {standing}")
        if status != "done":
            store.set_task_status(task_id, "done", result=standing)
    else:
        why = "not landed" if not merge_shas else "landed then auto-reverted"
        # NOT landed => the intended effect did NOT happen, so this row must never signal
        # "already done" to a later `begin_operation`. 'failed' is the honest terminal
        # state and keeps the work retryable; 'reconciled' means "resolved AND the effect
        # is in place" and is what suppresses a retry (review F2).
        store.set_operation_status(op["id"], "failed", why)
        # Only repair a task the crash left mid-flight. An 'open' task is already correct
        # (the reaper requeued it) and a 'blocked' one carries a legitimate failure record
        # that must not be silently reopened.
        if status in ("claimed", "in_progress"):
            store.set_task_status(task_id, "open")


def _crashed_applied_merges(store, limit: int) -> list:
    """`applied` merge rows whose ROUND never finished — the second half of the merge
    crash window (drill 1, 2026-08-13).

    `applied` is written the instant the merge lands (`code_round._op_complete`), but the
    round runs on for the whole re-baseline — a full grade plus the target's own suite —
    and only then keeps the merge or auto-reverts it. A crash anywhere in that stretch left
    the row at `applied`, which `_UNRESOLVED_STATUSES` does not sweep, so the merge's FATE
    was never resolved: the task was never repaired (the factory re-dispatched work it had
    already landed — precisely the consequence the design's seam map attributed to this
    window) and a REGRESSING merge could stay in the branch with nothing flagging it.

    A completed round now resolves its own row to `reconciled`, so a row left at `applied`
    is by construction a crashed one. The task-status predicate is what keeps rows written
    BEFORE that change out of the sweep — their task closed out normally — so this needs no
    migration and cannot retroactively escalate historical clean merges."""
    rows = []
    for row in store.operations(status="applied"):
        if row.get("kind") != "merge":
            continue          # 'applied' IS terminal for graduate_push — never sweep those
        task_id = (row.get("payload") or {}).get("task_id") or ""
        if not task_id:
            continue          # nothing to repair, and no way to tell crashed from clean
        task = store.get_task(task_id)
        if task and task.get("status") == "done":
            continue          # the round closed out; this row is history, not a crash
        rows.append(row)
    return rows[:limit]


def _resolve_applied_merge(store, op: dict, *, merge_repo: Optional[str], auto_branch: str,
                           runner) -> None:
    """Resolve one crashed `applied` merge row.

    The RECEIPT, not the `Factory-Task:` trailer, is the evidence here: it is the sha git
    itself returned from the merge, so this stays correct for a merge whose task carried no
    ref (`code_round` writes the trailer only when one is present) — reusing `_resolve_merge`
    would read a missing trailer as "never landed" and flip a genuinely applied row to
    'failed'.

    One outcome is determinable and one is not:
      - a `Factory-Revert: <receipt>` commit exists → the fate IS known (landed, then
        reverted): resolve, and hand the task back for redispatch.
      - the merge is still standing → it was never re-baselined. Whether that re-baseline
        would have kept or reverted it is exactly what the crash destroyed, and this module
        does not guess (binding rule 2): escalate. Never mark the task done — that would
        claim verified work — and never revert, which is a decision, not a repair."""
    receipt = (op.get("receipt") or "").strip()
    task_id = (op.get("payload") or {}).get("task_id") or ""
    verify = (f"git -C <factory/auto worktree> branch --contains {receipt[:12]}"
              if receipt else f"git -C <factory/auto worktree> log {auto_branch}")
    ref = f"reconcile:merge-unverified:{receipt[:12]}" if receipt else None

    if not receipt or not merge_repo or not os.path.isdir(merge_repo):
        _escalate(store, op, f"merge op #{op['id']} landed but its round never finished, and "
                            f"no reachable factory/auto worktree can confirm the merge; "
                            f"verify manually: {verify}", ref=ref)
        return

    res = _git(runner, merge_repo, "merge-base", "--is-ancestor", receipt, auto_branch)
    rc = getattr(res, "returncode", None)
    if rc not in (0, 1):   # 1 is a real "no"; anything else is git failing, not answering
        _escalate(store, op, f"git could not confirm whether merge {receipt[:12]} is on "
                            f"{auto_branch} for op #{op['id']}; verify manually: {verify}",
                  ref=ref)
        return
    if rc == 1:
        _escalate(store, op, f"merge op #{op['id']} recorded receipt {receipt[:12]}, but that "
                            f"sha is not on {auto_branch} — the branch moved under it; "
                            f"verify manually: {verify}", ref=ref)
        return

    reverted = _git(runner, merge_repo, "log", auto_branch, "--grep",
                    f"Factory-Revert: {receipt}", "--fixed-strings", "--format=%H")
    if _ok(reverted) and _lines(reverted):
        store.set_operation_status(op["id"], "reconciled",
                                   f"landed then auto-reverted: {receipt}")
        if (store.get_task(task_id) or {}).get("status") in ("claimed", "in_progress"):
            store.set_task_status(task_id, "open")
        return

    _escalate(store, op, f"merge {receipt[:12]} landed but its round crashed before the "
                        f"re-baseline finished: the merge is UNVERIFIED — neither confirmed "
                        f"nor reverted, and it is standing on {auto_branch} now. Re-baseline "
                        f"or revert it by hand; verify manually: {verify}", ref=ref)


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

    # `git merge-base --is-ancestor` answers with exit 0 (yes) or exit 1 (no). ANY other
    # code — 128 for "not a valid object name" (an unknown tip_sha, e.g. after a restore
    # from a snapshot taken on another clone; a missing origin/<base> tracking ref) —
    # means the question could not be ASKED, not that the answer is no. Treating it as
    # "not landed" is a guess, and guessing is what this phase forbids (review F3).
    rc = getattr(anc, "returncode", 1)
    if rc not in (0, 1):
        err = (getattr(anc, "stderr", "") or "").strip()[:200]
        _escalate(store, op, f"cannot determine whether op #{op['id']} landed — "
                            f"`merge-base --is-ancestor` exited {rc} ({err}); "
                            f"verify manually: {cmd}")
        return

    landed = rc == 0
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
        # 'failed', not 'reconciled' — see the merge resolver's note: the push did NOT
        # happen, so the operator's retry must not be suppressed by begin_operation's
        # skip (which would report a never-executed push as 'synced' + 'approved').
        store.set_operation_status(op["id"], "failed",
                                  f"not landed: not an ancestor of {remote}/{target_ref}")
        if match:
            store.unclaim_approval(match["id"])
            store.record_operator_action(
                "reconcile-unclaimed", f"approval-{match['id']}",
                "graduation push did not land — approval returned to pending "
                "(reconciler sweep)")


# -- kind: graduate_prepare (armed) --------------------------------------------

def _resolve_graduate_prepare(store, op: dict, *, root: Optional[str], base: str,
                              remote: str, receipts_dir: Optional[str], runner,
                              outbox_dir: Optional[str] = None) -> None:
    """Receipt-first (the broker's own verdict is authoritative and needs no fetch), then
    git as a fallback (the tip may have landed via some other path), then the OUTBOX — was
    the envelope this row is about ever written at all? Only a row that is none of those
    AND still legitimately in-flight (its paired approval is still 'executing') is left
    untouched: a broker that hasn't acted yet is normal, not a crash."""
    from ..reporting import envelope as envelope_mod

    idem_key = op.get("idem_key") or ""
    nonce = idem_key.split(":", 1)[1] if idem_key.startswith("gradprep:") else ""
    payload = op.get("payload") or {}
    approval_id = payload.get("approval_id")
    match = store.get_approval(approval_id) if approval_id else None
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

    # No receipt and no landing. Before calling this a normal in-flight wait, ask the OUTBOX
    # whether the envelope was ever written — the intent row opens the instant the nonce
    # exists, which is BEFORE `write_envelope`, so a crash in that gap leaves a row (and an
    # 'executing' approval) for an envelope that does not exist and never will. Drill 1
    # (2026-08-14) measured the cost: the broker can never produce a receipt, so the approval
    # sat 'executing' until the orphan reaper aged it out — and `propose_graduation` refuses
    # while a graduation approval is 'executing', so graduation stalled for up to
    # `autonomy.envelope_ttl_hours` on a state that was answerable immediately.
    #
    # Absence is safe evidence here, in this order only: the broker writes its receipt
    # BEFORE archiving the envelope out of the outbox (orchestrator/broker.py `_finalize`),
    # so a consumed envelope always leaves a receipt — which the check above would already
    # have found. The one other envelope-less-looking state, an unpinned envelope waiting in
    # unattended mode, leaves the envelope IN the outbox.
    #
    # The guard is on the SPOOL ROOT, not on `outbox/`. The outbox directory is created
    # lazily by the first `write_envelope`, so in exactly the case this resolves — a crash
    # BEFORE that write — it does not exist yet, and requiring it would make the state
    # permanently unprovable (measured: the drill's prep-before case). The spool root does
    # exist by then: the same prepare pushes the tip to the bare repo inside it several
    # steps earlier. So a spool root that exists is proof we are looking at the live spool;
    # one that does not is a misconfigured root (the F1 both-halves-same-root bug), where
    # absence proves nothing at all.
    spool_root = os.path.dirname(outbox_dir.rstrip(os.sep)) if outbox_dir else ""
    if (nonce and outbox_dir and spool_root and os.path.isdir(spool_root)
            and nonce not in envelope_mod.list_outbox(outbox_dir)):
        store.set_operation_status(
            op["id"], "failed",
            f"prepare never completed: no envelope for nonce {nonce} in the outbox, and no "
            f"receipt — the crash landed between the intent row and the envelope write")
        if match and match.get("status") == "executing":
            store.unclaim_approval(match["id"])
            store.record_operator_action(
                "reconcile-unclaimed", f"approval-{match['id']}",
                "armed graduation crashed before its envelope was written — approval "
                "returned to pending (reconciler sweep)")
        return

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
                  outbox_dir: Optional[str] = None,
                  runner=subprocess.run, limit: int = DEFAULT_LIMIT,
                  dry_run: bool = False, ignore_stop: bool = False) -> dict:
    """The reconciler sweep. Own STOP check (see the module docstring's binding rule 4).
    `root`/`base`/`merge_repo`/`receipts_dir` default to the live config/adapter/spool
    when not given (production); tests inject them directly against real tmp-dir git
    repos, bypassing config entirely.

    `dry_run` resolves nothing — returns the bounded row list that WOULD be examined
    (id/kind/idem_key/status only), for `factory reconcile --dry-run`'s preview.

    `ignore_stop` (Component D, `orchestrator.db_restore`): a restore's OWN precondition
    REQUIRES STOP to be engaged, so the normal STOP gate would make the reconciler a
    permanent no-op there — an explicit, human-triggered administrative act (mirroring
    `reporting.approvals.execute_approval`'s documented STOP-bypass reasoning: STOP
    brakes AUTONOMOUS work, not an operator's explicit command). Never set by the
    shift.py wiring or the `factory reconcile` CLI — both stay STOP-honoring.

    Returns `{'action': 'halted'|'dry_run'|'reconciled', 'examined': int,
    'resolved': [...], 'unknown': [...]}` (resolved/unknown are the post-resolution
    operation rows)."""
    if not ignore_stop and killswitch.is_halted():
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
    if receipts_dir is None or outbox_dir is None:
        from ..common import paths
        try:
            spool_root = (config.load_config().get("autonomy") or {}).get(
                "broker_spool_root") or None
        except Exception:  # noqa: BLE001
            spool_root = None
        # BOTH halves must resolve through the SAME root (the F1 lesson): reading receipts
        # from one spool and the outbox from another would make every armed prepare look
        # like an envelope that was never written.
        if receipts_dir is None:
            receipts_dir = paths.broker_receipts_dir(spool_root)
        if outbox_dir is None:
            # When the caller named a receipts dir, derive the outbox from IT rather than
            # from config: config could name a different spool entirely, and reading
            # receipts from one spool while judging "was an envelope ever written" against
            # another is the F1 bug in its most damaging form — every armed prepare would
            # look like an envelope that never existed.
            outbox_dir = (os.path.join(os.path.dirname(receipts_dir.rstrip(os.sep)), "outbox")
                          if receipts_dir else paths.broker_outbox_dir(spool_root))

    rows = _unresolved_operations(store, limit)
    # Plus the merge rows whose round crashed AFTER the receipt was written — invisible to
    # the status sweep above, and the larger half of the merge crash window.
    rows += _crashed_applied_merges(store, max(0, limit - len(rows)))
    if dry_run:
        return {"action": "dry_run", "examined": len(rows),
               "rows": [{"id": r["id"], "kind": r["kind"], "idem_key": r["idem_key"],
                        "status": r["status"]} for r in rows]}

    resolved, unknown = [], []
    for op in rows:
        before = op["status"]
        kind = op.get("kind")
        try:
            if kind == "merge" and before == "applied":
                _resolve_applied_merge(store, op, merge_repo=merge_repo,
                                      auto_branch=auto_branch, runner=runner)
            elif kind == "merge":
                _resolve_merge(store, op, merge_repo=merge_repo, auto_branch=auto_branch,
                              runner=runner)
            elif kind == "graduate_push":
                _resolve_graduate_push(store, op, root=root, base=base, remote=remote,
                                      runner=runner)
            elif kind == "graduate_prepare":
                _resolve_graduate_prepare(store, op, root=root, base=base, remote=remote,
                                         receipts_dir=receipts_dir, outbox_dir=outbox_dir,
                                         runner=runner)
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
