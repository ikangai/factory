# Factory-owned bus + human work queue — implementation plan

> **For Claude:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (fresh
> subagent per task, spec review + quality review per task). Design (approved):
> docs/plans/2026-07-08-factory-owned-bus-human-queue-design.md — read it first.

**Goal:** Vendor the agora bus into the factory, give the operator a workable human queue
in the fleet dashboard (answer / reframe / add / approve), and gate outward GitHub pushes
behind explicit GUI approval.

**Branch:** `feat/factory-bus-human-queue`. Conventions: TDD red-first per task; hermetic
tests (tmp stores, scripted runners — see tests/test_issue_sync.py, tests/conftest.py
idioms); dense why-comments; commit per task with Co-Authored-By trailer; suite must stay
green (baseline 840).

---

## Task 1 — Vendor the bus (haiku)
Create `vendor/agora/chat.py` as a byte-identical copy of
`/Users/martintreiber/.claude/plugins/cache/ikangai/agora/0.15.1/.groupchat/chat.py`
(verified byte-identical to the 0.15.3 the deployment runs). Add
`vendor/agora/VENDORED.md` (source, version, date, re-vendor procedure, NEVER edit chat.py
in place). Add `vendor/__init__.py` + `vendor/agora/__init__.py` (empty, so the path ships
with the package). Test `tests/test_vendored_bus.py`: run the vendored CLI via subprocess
with `AGORA_DIR=<tmp>` — `send --from tester "hello"` then verify the message lands
(chat.db exists; a read command returns the text). Diff-check test: NO (fragile);
instead assert `hashlib.sha256` of the file matches a constant recorded in the test —
update-consciously-on-revendor.

## Task 2 — common/bus.py wrapper + collab.py switch (sonnet)
`common/bus.py`: `VENDORED_CHAT = paths.factory("vendor", "agora", "chat.py")`;
functions `send(text, frm="factory", bus_dir=None)`, `answer(msg_id, text, frm="operator",
bus_dir=None)`, `open_questions(bus_dir=None) -> list[dict]`, `who(bus_dir=None)`,
`recent(n=50, bus_dir=None)` — each a subprocess call to the vendored CLI with
`AGORA_DIR=bus_dir or roles.common.factory_agora_dir()`, injectable `runner` for tests,
never raises (returns None/[] + logged line on failure — bus outages must not kill the
factory). Discover the CLI's read/questions output format by running it (`--help`, then the
relevant subcommands against a tmp bus you seed with send/escalation) and parse
accordingly; if a `--json` flag exists, prefer it. Switch `reporting/collab.py` to read via
the same bus dir resolution (keep its current output shape — the dashboard feed must not
change). Worker env (`worker_bus_env`) unchanged. Tests: wrapper round-trip against a real
tmp bus (send → recent sees it; a `@human` send → open_questions lists it; answer clears
it), plus failure-path (missing chat.py → [] not raise).

## Task 3 — Store: pending_approvals + operator_actions (sonnet)
`store/schema.sql` + `_migrate` additive path (follow the existing ALTER pattern in
common/store.py): tables `pending_approvals(id INTEGER PK, kind TEXT CHECK(kind IN
('graduation','publication')), status TEXT DEFAULT 'pending' CHECK(status IN
('pending','approved','rejected','stale','superseded')), payload_json TEXT NOT NULL,
note TEXT DEFAULT '', created_at TEXT NOT NULL, resolved_at TEXT)`, and
`operator_actions(id INTEGER PK, action TEXT NOT NULL, item_ref TEXT NOT NULL, detail TEXT
DEFAULT '', created_at TEXT NOT NULL)`. Store methods: `add_pending_approval(kind,
payload) -> id` (supersede: mark any older pending row of the same kind 'superseded' —
one live proposal per kind), `pending_approvals(status='pending')`,
`resolve_approval(id, status, note='')`, `record_operator_action(action, item_ref,
detail='')`, `recent_operator_actions(limit)`. TDD against tmp Blackboard.

## Task 4 — derive_human_queue (sonnet)
`reporting/human_queue.py`: `derive_human_queue(store, bus_dir=None) -> dict` returning
`{"items": [...], "counts": {...}}`; item shapes: `{"type": "escalation", "id", "ts",
"sender", "text"}` (from `common.bus.open_questions`), `{"type": "blocked", "task_id",
"title", "reason", "age_days", "evidence_head"}` (store.recent_blocked_tasks + task_evidence
if available), `{"type": "approval", "approval_id", "kind", "summary", "n_commits",
"age_days", "stale"}` (payload_json parsed; stale = age > 3 days). Sorted: escalations,
approvals, blocked. Pure + hermetic tests (tmp store, tmp bus seeded via common.bus).

## Task 5 — Approval gate + promote_to_release (sonnet — the correctness-critical task)
1. config.yaml `autonomy: push_approval: true` with a brake-class comment (config-only —
   NOT added to SETTINGS_SPEC, mirroring enforce_shift_budget).
2. `orchestrator/orchestrator.py` `_graduate_after_shift`: when the gate is ON and the
   normal preconditions hold (real+shipped), do NOT call graduate_and_push for real —
   call it with `dry_run=True`, store the preview via
   `store.add_pending_approval("graduation", payload)` (payload: range, n_commits, commit
   subjects list, synced-preview, fetch_failed flag), print `[run] graduation proposed →
   approval #N pending (autonomy.push_approval)`, return `{"action": "proposed",
   "approval_id": N}`. Gate OFF → behavior byte-identical to today (existing tests must
   pass unchanged apart from explicitly toggling the gate in fixtures — flip the gate OFF
   in existing test setups ONLY via monkeypatched config, do not weaken assertions).
3. `reporting/approvals.py`: `execute_approval(store, approval_id, *, graduate_fn=None,
   promote_fn=None, runner=subprocess.run) -> dict` — re-resolves config/adapter, runs the
   REAL graduate_and_push (or promote) for the approved kind, resolves the row
   (approved + result note) and records the operator action; rejection helper
   `reject_approval(store, id, note)`.
4. `reporting/issue_sync.py`: `promote_to_release(*, root, base, release="main",
   remote="origin", runner=subprocess.run) -> dict` — fail-closed mechanization of the
   2026-07-08 manual promotion: fetch base+release; verify release contains no commits
   missing from base+merge topology conflicts (`git merge-base --is-ancestor` checks);
   create a temp worktree detached at `origin/<release>`, `merge --no-ff origin/<base>`
   (abort+skip on conflict), plain push `HEAD:<release>`, remove worktree in a finally.
   Skips: fetch-failed / nothing-to-promote / merge-conflict / push-failed.
5. Publication proposals: in `_warn_graduation_lag`, when the publication edge exceeds
   threshold AND push_approval is on, ALSO file a pending_approval("publication", payload
   with the lag count) if none pending (supersede handles refresh).
Tests: gate-on → proposed row with correct payload + no push calls recorded; gate-off →
unchanged; execute_approval drives injected graduate_fn and resolves row; reject
records note; promote_to_release happy path + each skip against a scripted runner
(test_issue_sync.py fake-runner idiom).

## Task 6 — Fleet server endpoints (sonnet)
`dashboard/fleet_server.py`: extend the /api/fleet payload with `human_queue` (from
derive_human_queue) and add POST endpoints (reuse the existing CSRF/localhost guard +
JSON body parsing patterns already in the file):
- `POST /api/queue/answer` {id, text} → common.bus.answer + record_operator_action.
- `POST /api/queue/task` {task_id?, op: reframe|retry|drop|add, title?, detail?} —
  reframe: update title/detail, clear result, status open; retry: status open, clear
  result; drop: status dropped; add: store.add_task with generated task-id (+ optional
  spec fields folded via reporting/scope_check.spec_detail_suffix like research_feed does).
- `POST /api/queue/approval` {approval_id, op: approve|reject, note?} → approvals module.
All return JSON {ok, ...}; every success records an operator_action. Hermetic endpoint
tests following the file's existing test harness (tests/test_resources.py or
tests/test_fleet_server*.py — find and match).

## Task 7 — Queue tab UI (sonnet)
`reporting/fleet_viz.py` (the page the fleet server serves): add a "Queue" tab/panel as
the FIRST panel: escalation cards with inline textarea+Answer button; approval cards
showing kind, commit list/summary, Approve/Reject(+note); blocked cards with reason,
Reframe (editable title/detail) / Retry / Drop; an Add-task form. Badge with queue count
in the tab header. Match the existing page's styling/JS conventions; fetch()-POST to the
Task-6 endpoints; optimistic refresh of /api/fleet after each action. MANDATORY: extract
and `node --check` the inline JS in the test (an existing test may already do this — grep
`node --check` / `node -c` in tests/; extend it), a silent JS syntax error froze this
dashboard once before.

## Task 8 — Prompts + deployment kit (haiku)
1. Worker/conductor/researcher prompt seams: where agora posting is described (grep
   "bus"/"agora"/"announce" in roles/*/prompt.md + roles/common.py develop_candidate
   prompt suffix), state the send command explicitly with the vendored path:
   `python3 {FACTORY_ROOT}/vendor/agora/chat.py send --from <handle> "..."` — the factory
   must not depend on the plugin's SessionStart hook. Keep AGORA_DIR env plumbing as-is.
2. deploy/user-factory: remove the agora-plugin install step from
   02-bootstrap echo + runbook §3 (replace with one line: the bus is vendored, nothing to
   install); runbook gains a "Human queue" paragraph in §6 (steering): answer/unblock/
   approve from the dashboard; approvals now gate GitHub pushes (autonomy.push_approval).
3. `bin/factory bus` passthrough subcommand (argv → vendored chat.py) so the operator can
   also drive the bus via CLI without knowing the vendor path. Small; wire in bin/factory
   (bash) next to the board/smoke special cases.

## Task 9 — Integration (coordinator)
Full suite green; `node --check` on the dashboard JS; live smoke of derive_human_queue
against the REAL store+bus (read-only); final whole-branch adversarial review (opus);
operator merge gate; push to the deploy bare; deployment update instructions (update.sh +
daemon kickstart + note that the next graduation will PAUSE for approval in the GUI).
