# Factory-owned bus + human work queue — design

Validated with the operator 2026-07-08 (brainstorm, 3 sections approved). Motivation: three
frictions from the first live deployment day — answering a factory escalation required a
cross-user `sudo … chat.py answer` incantation; the coordination bus is an external plugin
that already version-drifted between accounts (0.15.1 vs 0.15.3) and needs a manual install
step in the bootstrap; the dashboard shows a work queue but offers no way to WORK the items
that need a human.

## 1. The factory owns its bus (vendored)

`vendor/agora/chat.py` — the plugin implementation copied VERBATIM (source: agora 0.15.3,
byte-identical to 0.15.1; provenance in `vendor/agora/VENDORED.md`, never edited in place —
re-vendor consciously). Wire format (sqlite `chat.db` in `FACTORY_ROOT/.groupchat`) is
unchanged, so interactive dev sessions using the real plugin stay compatible on the same bus.

- `common/bus.py`: thin Python wrapper invoking the vendored CLI by absolute path
  (subprocess — same contract agents use; no import of the 4.5k-line module). API:
  `send`, `answer`, `open_questions`, `who`, `recent`.
- `reporting/collab.py` and every factory-side bus consumer go through the wrapper.
- Worker/conductor prompts carry an explicit bus-contract line (send command with the
  vendored path) instead of relying on the plugin's SessionStart hook. The agora plugin
  drops out of the deployment bootstrap; it remains optional for human dev sessions.
- v1 simplification vs the brainstorm text: NO separate escalations shadow table — the bus
  is already sqlite and `derive_human_queue` reads it live; the store-side history comes
  from the `operator_actions` audit rows written on every answer.

## 2. Human work queue in the fleet dashboard

`reporting/human_queue.py` → `derive_human_queue(store, bus_dir)` (pure, hermetic): items =
open `@human` escalations (bus) + blocked tasks with reason/evidence (store) + pending
approvals (store) + stale flags (age thresholds). The fleet dashboard (:9788/:8788) renders
it as the first tab; every item carries its action inline.

Actions (POST endpoints on the fleet server, localhost + existing CSRF origin guard, each
writing an `operator_actions` audit row):
- `answer` — replies to an escalation via the bus (clears the tracked flag).
- `task reframe` (edit title/detail → reopen with cleared result) / `retry` / `drop`.
- `task add` — title + detail + optional target_surface/acceptance (born spec-complete).
- `approval approve/reject` — see 3.

## 3. Approval gate on outward pushes

`autonomy.push_approval: true` — default ON, config-file-only (brake class, like
`enforce_shift_budget`; autonomy is turned UP by file edit, never by browser click).

- Propose: where `_graduate_after_shift` would push, it instead captures the dry-run
  preview (range, commits, issue actions, graduation-test verdict) into a
  `pending_approvals` row. Shift ends; nothing left the machine.
- Approve (GUI) → executes the real `graduate_and_push` (fetch-first, fail-closed
  unchanged; upstream drift since proposal → skip + card updates, never force).
- Reject (GUI, with note) → recorded; surfaces in the conductor's `{RESUME}` seam.
- Publication gets the same card type: `promote_to_release()` in `reporting/issue_sync.py`
  performs the base→release merge-push via a temp worktree (the manual procedure from
  2026-07-08, mechanized), also approval-gated.
- Stale: unapproved cards older than N days flag stale; complements the shift-end lag alarm.

## Testing

Three hermetic layers: `derive_human_queue` + approval flow against tmp store + scripted
bus; endpoint tests via the existing fleet-server harness; one end-to-end test proving an
approval invokes `graduate_and_push` with an injected runner. Dashboard inline JS gets
`node --check` (an earlier live lesson).
