"""Human-queue approval execution (design: docs/plans/2026-07-08-factory-owned-bus-human-
queue-design.md §3). `autonomy.push_approval` (config.yaml-only, default ON) stops
`orchestrator._graduate_after_shift` / `_warn_graduation_lag` from pushing directly —
instead they file a `pending_approvals` row (a `graduate_and_push(dry_run=True)` preview,
or a publication-lag snapshot). This module turns operator action on that row into the
REAL push: `execute_approval` re-resolves the target config the same way
`orchestrator.cmd_graduate`/`_graduate_after_shift` do and runs the real
`graduate_and_push`/`promote_to_release`; `reject_approval` just records the decision.

Layering note: `orchestrator.py` imports `reporting` (e.g. `from ..reporting import
issue_sync`), never the reverse — importing `orchestrator` here to reuse
`_graduation_test_fn` would be circular. Its 3-line closure is replicated below instead,
tagged so a future change to that gate is easy to keep in lockstep.
"""
from __future__ import annotations

from ..common import config


def _graduation_test_fn():
    """Replica of orchestrator._graduation_test_fn (see module docstring — reporting
    cannot import orchestrator). Re-run the target's suite on the integrated tip before an
    APPROVED graduation actually pushes: same prod-push quality gate, same config knob
    (autonomy.graduation_retest, default ON) as the proposal path used when it was first
    previewed."""
    auton = config.load_config().get("autonomy", {}) or {}
    if not auton.get("graduation_retest", True):
        return None
    return lambda root: config.get_adapter().run_tests(root)


def propose_graduation(store, *, preview: dict) -> int:
    """File a graduation approval from a `graduate_and_push(dry_run=True)` preview. Thin:
    the payload carries only what the Queue-tab card renders. `add_pending_approval`
    handles the supersede-one-live-proposal-per-kind semantics, so a fresher preview
    (e.g. next shift) automatically retires a stale one. Returns the new row's id."""
    payload = {
        "range": preview.get("range", ""),
        "n_commits": preview.get("n_commits", 0),
        "synced_preview": preview.get("synced", []),
        "fetch_failed": bool(preview.get("fetch_failed", False)),
    }
    return store.add_pending_approval("graduation", payload)


def _result_note(result: dict) -> str:
    """A short, human-readable summary of a graduate/promote result — used both as the
    resolved row's note and the operator_actions audit detail."""
    action = result.get("action")
    if action == "synced":
        return f"pushed {result.get('n_commits', 0)} commit(s) ({result.get('range', '')})"
    if action == "promoted":
        sha = (result.get("sha") or "")[:9]
        return f"promoted {result.get('n_commits', 0)} commit(s) -> {sha}"
    if action == "skip":
        return f"skip: {result.get('reason', '')}"
    if action == "error":
        return f"error: {result.get('error', '')}"
    return str(action)


def execute_approval(store, approval_id, *, graduate_fn=None, promote_fn=None) -> dict:
    """Operator clicked Approve on a pending_approvals row: re-resolve config the same way
    `orchestrator.cmd_graduate`/`_graduate_after_shift` do, and run the REAL push for the
    row's kind (never the dry-run preview again — the whole point of approving is to push).

    On success (result action 'synced'/'promoted') the row resolves 'approved' with a
    short note and an operator_actions audit row is written. On any other outcome (a
    skip like upstream drift/tests-failed/push-failed, or an exception surfaced by the
    injected fn's own error handling) the row is deliberately LEFT PENDING rather than
    auto-rejected — a transient failure (e.g. the retest went red, or origin/<base> moved
    since the preview) is something the operator should be able to fix and retry the SAME
    approval, not lose track of. The attempt is still audited ('approve-failed') so the
    dashboard/history shows it was tried and why it didn't land."""
    row = store.get_approval(approval_id)
    if row is None:
        return {"ok": False, "error": "not-found"}
    if row.get("status") != "pending":
        return {"ok": False, "error": f"not-pending (status={row.get('status')})"}

    from . import issue_sync  # local: avoid the import at module load for lighter callers
    kind = row.get("kind")

    if kind == "graduation":
        graduate_fn = graduate_fn or issue_sync.graduate_and_push
        repo = config.target_repo_slug()
        root = config.get_adapter().entry()[0]
        base = config.target_config().get("base_branch") or "chore/extract-factory"
        result = graduate_fn(root=root, base=base, repo=repo, store=store,
                             test_fn=_graduation_test_fn())
    elif kind == "publication":
        promote_fn = promote_fn or issue_sync.promote_to_release
        root = config.get_adapter().entry()[0]
        base = config.target_config().get("base_branch") or "chore/extract-factory"
        release = config.target_config().get("release_branch") or "main"
        result = promote_fn(root=root, base=base, release=release)
    else:
        return {"ok": False, "error": f"unknown kind: {kind}"}

    note = _result_note(result)
    if result.get("action") in ("synced", "promoted"):
        store.resolve_approval(approval_id, "approved", note=note)
        store.record_operator_action("approve", f"approval-{approval_id}", note)
        return {"ok": True, "result": result}
    store.record_operator_action("approve-failed", f"approval-{approval_id}", note)
    return {"ok": False, "result": result}


def reject_approval(store, approval_id, note: str = "") -> dict:
    """Operator clicked Reject: resolve the row 'rejected' with the operator's note (surfaces
    in the conductor's {RESUME} seam per the design) and audit the decision."""
    ok = store.resolve_approval(approval_id, "rejected", note)
    store.record_operator_action("reject", f"approval-{approval_id}", note)
    return {"ok": ok}
