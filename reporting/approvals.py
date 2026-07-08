"""Human-queue approval execution (design: docs/plans/2026-07-08-factory-owned-bus-human-
queue-design.md §3). `autonomy.push_approval` (config.yaml-only, default ON) stops
`orchestrator._graduate_after_shift` / `_warn_graduation_lag` from pushing directly —
instead they file a `pending_approvals` row (a `graduate_and_push(dry_run=True)` preview,
or a publication-lag snapshot). This module turns operator action on that row into the
REAL push: `execute_approval` re-resolves the target config the same way
`orchestrator.cmd_graduate`/`_graduate_after_shift` do and runs the real
`graduate_and_push`/`promote_to_release`; `reject_approval` just records the decision.

THE CARD IS A PINNED CONSENT ARTIFACT. The stored payload is exactly what the operator
saw and approved; execute_approval pushes ONLY when reality still matches it. Before
pushing it re-derives the preview (a fresh dry-run / lag count, inside the push lock) and
compares against the stored payload — a mismatch (upstream moved, more shifts merged)
refuses to push, refreshes the card's payload in place, reverts the row to pending, and
returns {"error": "preview-stale"} so the UI re-renders the TRUE range for a re-click.

Concurrency: the row is CLAIMED atomically (store.claim_approval, pending→executing)
before anything runs, so a double-click / two-dashboard race executes at most once; the
push itself runs under common.filelock.repo_lock so approvals, shift-end graduations and
`factory graduate` in other processes serialize on the same repo.

STOP semantics, deliberate: an operator's Approve click (like `factory graduate`)
intentionally bypasses the STOP killswitch — STOP brakes AUTONOMOUS work, not explicit
human actions taken from the queue.

Layering note: `orchestrator.py` imports `reporting` (e.g. `from ..reporting import
issue_sync`), never the reverse — importing `orchestrator` here to reuse
`_graduation_test_fn` would be circular. Its 3-line closure is replicated below instead,
tagged so a future change to that gate is easy to keep in lockstep.
"""
from __future__ import annotations

from ..common import config, filelock


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


def _graduation_payload(preview: dict) -> dict:
    """The card shape a graduation preview pins: what the Queue tab renders AND what the
    stale-check compares. The consent-critical set is n_commits + the two ENDPOINT SHAs
    (base_sha = origin/<base> tip, tip_sha = <auto_branch> tip). The `range` string is a
    CONSTANT symbolic literal (origin/<base>..<auto_branch>) so on its own it guards only
    against config drift changing branch names — never a same-count amend/force-push; the
    SHAs close that masked case (Fix 2, final whole-branch review). Missing SHAs pin ""
    (fail-closed — "" can never match a real fresh SHA, so the gate refuses)."""
    return {
        "range": preview.get("range", ""),
        "n_commits": preview.get("n_commits", 0),
        "base_sha": preview.get("base_sha", ""),
        "tip_sha": preview.get("tip_sha", ""),
        "synced_preview": preview.get("synced", []),
        "fetch_failed": bool(preview.get("fetch_failed", False)),
    }


def propose_graduation(store, *, preview: dict) -> int:
    """File a graduation approval from a `graduate_and_push(dry_run=True)` preview. Thin:
    the payload carries only what the Queue-tab card renders. `add_pending_approval`
    handles the supersede-one-live-proposal-per-kind semantics, so a fresher preview
    (e.g. next shift) automatically retires a stale one. Returns the new row's id."""
    return store.add_pending_approval("graduation", _graduation_payload(preview))


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


def _fail_attempt(store, approval_id: int, note: str) -> None:
    """A failed/aborted execution attempt: revert the claim (executing → pending, so the
    operator can fix the cause and retry the SAME approval — deliberately NOT auto-
    rejected) and audit the attempt so the history shows it was tried and why it failed."""
    store.unclaim_approval(approval_id)
    store.record_operator_action("approve-failed", f"approval-{approval_id}", note)


def execute_approval(store, approval_id, *, graduate_fn=None, promote_fn=None,
                     lag_fn=None) -> dict:
    """Operator clicked Approve on a pending_approvals row: claim it atomically, verify
    reality still matches the pinned card (see module docstring), and run the REAL push
    for the row's kind under the cross-process push lock.

    Outcomes:
    - success ('synced'/'promoted') → row resolves executing→'approved' with a short
      note + an 'approve' audit row; returns {"ok": True, "result": ...}.
    - lag cleared (fresh n_commits/ahead <= 0 — everything landed by other means) → NO
      push; row resolves executing→'stale' with an 'approve-stale-cleared' audit row
      (Fix A / Fix 4c); returns {"ok": False, "error": "lag-cleared"} rather than
      re-pending a 0-count card that could never clear.
    - stale preview (fresh dry-run/lag differs from the stored payload) → NO push; the
      row's payload is refreshed in place, the claim reverts to 'pending', an
      'approve-stale-refreshed' audit row is written; returns
      {"ok": False, "error": "preview-stale", "fresh": <fresh preview>} — the UI
      re-renders the card and the operator re-approves the TRUE range.
    - push failure (skip like tests-failed/push-failed, lock-busy, an unverifiable
      preview) → row reverts to 'pending' (retryable, deliberately NOT auto-rejected) +
      an 'approve-failed' audit row; returns {"ok": False, ...}.
    """
    row = store.get_approval(approval_id)
    if row is None:
        return {"ok": False, "error": "not-found"}
    kind = row.get("kind")
    if kind not in ("graduation", "publication"):
        return {"ok": False, "error": f"unknown kind: {kind}"}
    # Atomic claim BEFORE any work: exactly one of two racing Approve calls wins.
    if not store.claim_approval(approval_id):
        return {"ok": False, "error": f"not-pending (status={row.get('status')})"}

    from . import issue_sync  # local: avoid the import at module load for lighter callers
    payload = row.get("payload") or {}
    ref = f"approval-{approval_id}"
    root = config.get_adapter().entry()[0]
    base = config.target_config().get("base_branch") or "chore/extract-factory"

    try:
        # Preview re-derivation AND the push share one lock hold: no other pusher can
        # move the remote between "reality matches the card" and the push itself.
        with filelock.repo_lock(root):
            if kind == "graduation":
                graduate_fn = graduate_fn or issue_sync.graduate_and_push
                repo = config.target_repo_slug()
                test_fn = _graduation_test_fn()
                fresh = graduate_fn(root=root, base=base, repo=repo, store=store,
                                    test_fn=test_fn, dry_run=True)
                if fresh.get("action") != "dry_run":
                    _fail_attempt(store, approval_id,
                                  f"preview failed: {_result_note(fresh)}")
                    return {"ok": False, "result": fresh}
                if fresh.get("n_commits", 0) <= 0:
                    # Fix A (final adversarial re-verification), symmetric with the
                    # publication ahead<=0 case (Fix 4c) just below: everything the card
                    # covers already landed by other means (another approval, a manual push)
                    # — there is NOTHING left to graduate. This check sits BEFORE the
                    # mismatch/consent compare below so it catches BOTH shapes of the loop:
                    # (1) the stale-refresh path — the stored payload still pins a real
                    # count, but reality is now 0. The OLD code fell through to the mismatch
                    # branch, rewrote the card's payload to n_commits:0, and reverted it to
                    # pending; approving that 0-commit card then ran graduate_fn for real,
                    # which no-ops on 0 commits, so _fail_attempt reverted it to pending
                    # AGAIN — a card that can never clear without a manual Reject.
                    # (2) a DIRECT approve where the fresh preview already MATCHES a pinned
                    # 0-commit payload (e.g. a second click after (1), or any other path that
                    # produced a 0-commit card) — the old mismatch check saw no difference and
                    # would have run the same no-op-then-repend loop.
                    # Resolve 'stale' instead so the row leaves the queue for good.
                    store.resolve_approval(approval_id, "stale", note="nothing left to graduate")
                    store.record_operator_action(
                        "approve-stale-cleared", ref,
                        "graduation lag cleared (base already current — nothing to graduate)")
                    return {"ok": False, "error": "lag-cleared"}
                # Consent match: count + BOTH endpoint SHAs (Fix 2 — the range alone is a
                # constant literal). The range comparison is kept as a config-drift guard
                # (branch-name change), but a same-count amend/force-push now trips the SHAs.
                if (fresh.get("range") != payload.get("range")
                        or fresh.get("n_commits") != payload.get("n_commits")
                        or fresh.get("base_sha", "") != payload.get("base_sha", "")
                        or fresh.get("tip_sha", "") != payload.get("tip_sha", "")):
                    store.update_approval_payload(approval_id, _graduation_payload(fresh))
                    store.unclaim_approval(approval_id)
                    store.record_operator_action(
                        "approve-stale-refreshed", ref,
                        f"card showed {payload.get('n_commits')} commit(s) "
                        f"({payload.get('range', '')}, tip {(payload.get('tip_sha') or '')[:9]}); "
                        f"reality is {fresh.get('n_commits')} ({fresh.get('range', '')}, "
                        f"tip {(fresh.get('tip_sha') or '')[:9]})")
                    return {"ok": False, "error": "preview-stale", "fresh": fresh}
                result = graduate_fn(root=root, base=base, repo=repo, store=store,
                                     test_fn=test_fn)
            else:  # kind == "publication"
                release = config.target_config().get("release_branch") or "main"
                # The publication card pins the lag COUNT the alarm measured
                # (origin/<release>..factory/auto via graduation_lag(base=<release>) —
                # the same edge _warn_graduation_lag files from).
                lag_fn = lag_fn or issue_sync.graduation_lag
                fresh_lag = lag_fn(root=root, base=release)
                ahead = fresh_lag.get("ahead")
                if ahead is None:
                    note = f"lag unmeasurable: {fresh_lag.get('error', '')}"
                    _fail_attempt(store, approval_id, note)
                    return {"ok": False, "result": {"action": "error", "error": note}}
                if ahead <= 0:
                    # Fix 4c (final whole-branch review): the base branch already contains
                    # everything (or more) — there is nothing to promote. Re-pending an
                    # ahead=0 card would create an approval that can NEVER clear (approve →
                    # promote_to_release skips 'nothing-to-promote' → the stale-check re-pends
                    # 0 again, a loop). Resolve it 'stale' so it leaves the queue for good.
                    store.resolve_approval(approval_id, "stale", note="publication lag cleared")
                    store.record_operator_action(
                        "approve-stale-cleared", ref,
                        "publication lag cleared (base already current — nothing to promote)")
                    return {"ok": False, "error": "lag-cleared"}
                if ahead != payload.get("ahead"):
                    fresh_payload = {"ahead": ahead, "release": release}
                    store.update_approval_payload(approval_id, fresh_payload)
                    store.unclaim_approval(approval_id)
                    store.record_operator_action(
                        "approve-stale-refreshed", ref,
                        f"card showed {payload.get('ahead')} commit(s) behind "
                        f"origin/{release}; reality is {ahead}")
                    return {"ok": False, "error": "preview-stale", "fresh": fresh_payload}
                promote_fn = promote_fn or issue_sync.promote_to_release
                result = promote_fn(root=root, base=base, release=release)
    except filelock.LockBusyError:
        _fail_attempt(store, approval_id, "lock-busy: another pusher holds the repo lock")
        return {"ok": False, "error": "lock-busy"}

    note = _result_note(result)
    if result.get("action") in ("synced", "promoted"):
        resolved = store.resolve_approval(approval_id, "approved", note=note)
        # resolved can only be False if the row left 'executing' underneath us (should be
        # impossible — the claim is exclusive); surface it in the audit rather than drop it.
        store.record_operator_action(
            "approve", ref, note if resolved else note + " (warn: resolve raced)")
        return {"ok": True, "result": result}
    _fail_attempt(store, approval_id, note)
    return {"ok": False, "result": result}


def reject_approval(store, approval_id, note: str = "") -> dict:
    """Operator clicked Reject: resolve the row 'rejected' with the operator's note (surfaces
    in the conductor's {RESUME} seam per the design) and audit the decision."""
    ok = store.resolve_approval(approval_id, "rejected", note)
    store.record_operator_action("reject", f"approval-{approval_id}", note)
    return {"ok": ok}
