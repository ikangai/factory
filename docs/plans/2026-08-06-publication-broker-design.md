# Phase 1 — publication broker, red-proof tests, claim leases (2026-08-06)

**Provenance:** Phase 1 of `docs/plans/2026-08-06-production-hardening-roadmap.md`.
Seam-mapped against main @ 442e80e; this doc encodes the mapped reality, including
eight corrections to the roadmap's sketch (issue closure is inside `graduate_and_push`,
the closure preview is not consent-compared, no claim machinery exists yet, the broker
cannot read anything under the factory user's 700 home).

## Goal

Make the reviewer's sentence true in code: *even a total compromise of the guest-house
account cannot silently publish different code from what the operator approved.* Plus
the two cheap high-value verifier fixes riding along: red-proof tests and claim leases.

## The authority line (this design's spine)

> The factory PREPARES publications; it never executes them when the broker is armed.
> An envelope is a REQUEST — the broker's own operator-side allowlist, never any
> envelope field, is the authority on what may be pushed where. The broker re-verifies
> every sha against live state immediately before acting; mismatch, expiry, or a reused
> nonce invalidates the envelope permanently. Issue actions execute only from the
> approved preview. The factory user holds no GitHub credential in broker mode.

## Component A — the envelope (`reporting/envelope.py`, factory side)

A self-contained JSON file (the broker cannot read the blackboard — guest-house homes
are 700):

```json
{"schema_version": 1, "action": "graduate",
 "repo_slug": "ikangai/clive", "base_branch": "chore/extract-factory",
 "base_sha": "<origin/<base> tip at prepare time>", "tip_sha": "<local base after ff>",
 "range": "...", "n_commits": 3,
 "issue_actions": [{"op": "close", "number": 12, "sha": "...", "line": "closes #12"}],
 "approval_id": 7, "policy_hash": "<sha256 of config.yaml>",
 "created_at": "...", "expires_at": "...", "nonce": "<uuid4>"}
```

- Written to the spool outbox as `<nonce>.json` plus `<nonce>.json.sha256` (content
  hash — the broker recomputes and refuses on mismatch; cheap tamper/truncation guard).
- `expires_at` = created + `autonomy.envelope_ttl_hours` (default 24; `autonomy.*` =
  frozen knob for free).
- Builder takes the existing `_graduation_payload` preview + the dry-run `sync_issues`
  plan; nothing is computed twice.

## Component B — the spool (`/Users/Shared/factory-broker/`)

Follows the `/Users/Shared/factory.git` ownership-split precedent:

```
/Users/Shared/factory-broker/
  outbox/    owner factory:staff  (factory writes envelopes; operator reads)
  receipts/  owner operator:staff (broker writes receipts; factory reads)
clive-publish.git   bare repo, owner factory:staff, operator-readable —
                    the factory pushes tip there LOCALLY (file remote, no credential);
                    the broker pushes from it to GitHub.
```

Receipt: `{"nonce", "status": "pushed|rejected|expired", "receipt_sha", "detail",
"executed_at"}` as `<nonce>.receipt.json`. A nonce with an existing receipt is spent —
the broker refuses to execute it again (at-least-once prep, exactly-once effect via
git's own atomicity: pushing an already-present sha to the same ref is a no-op).
Single-operator machine caveat (runbook): `/Users/Shared` is world-readable; envelopes
contain no secrets, only shas and issue numbers.

Dev/single-user layout: `<factory>/state/broker/{outbox,receipts}` + a local bare —
same code paths, configurable roots, hermetically testable with `file://` remotes.

## Component C — the broker (`orchestrator/broker.py`, runs as the OPERATOR)

`factory broker run-once | watch | status` from the operator's own factory checkout.

- **Allowlist first**: `~/.factory-broker.yaml` (operator home, 600):
  `publications: [{repo_slug, remote_url, base_branch, bare_path, allow_issue_ops: true}]`.
  Every envelope field is cross-checked against the matching allowlist entry;
  no entry ⇒ reject. The envelope requests; the allowlist authorizes.
- **Verification, immediately before executing** (any failure ⇒ receipt
  `rejected` + reason, never retried silently):
  1. content hash matches; schema version known; not expired; nonce has no receipt.
  2. allowlist entry exists and matches repo_slug/base_branch/bare_path.
  3. bare repo's `tip_sha` object exists and `git ls-remote <remote_url>
     refs/heads/<base_branch>` == `base_sha` (nobody moved the branch since approval —
     drill 3's exact check).
  4. `git -C <bare> merge-base --is-ancestor <base_sha> <tip_sha>` (fast-forward only,
     never a force push).
  5. `policy_hash` recorded into the receipt (informational — operator config may
     legitimately differ).
- **Execute**: `git -C <bare> push <remote_url> <tip_sha>:refs/heads/<base_branch>`
  with the OPERATOR's credential; then issue actions from the envelope preview only
  (`gh issue close/comment`), each idempotency-guarded by the receipt. Write receipt
  `pushed` with the resulting remote sha.
- `watch` = poll loop with backoff; the LaunchAgent uses WatchPaths on the outbox and
  just runs `run-once`.

## Component D — factory-side integration

- Config: `autonomy.publication_broker: false` (config-only; frozen via the
  `autonomy.` prefix). OFF = today's behavior, byte-for-byte.
- ON: the real-push path of graduation (`_graduate_after_shift`, `cmd_graduate`,
  `approvals.execute_approval`) still does the local ff-merge + retest, then: push tip
  to the local bare (file remote), write the envelope, resolve the approval to
  `executing` with the nonce in its payload. **No `git push origin`, no `gh`, and the
  factory functions with no `GH_TOKEN` present.**
- Receipt ingestion: at shift start (next to `reap_orphaned_approvals`) and via
  `factory broker-receipts` — receipts resolve the approval (`approved` + receipt sha /
  `rejected` + reason surfaced on the Queue tab), then archive to `receipts/done/`.
- **Consent-compare the issue preview** (mapped gap #6, fixed in BOTH modes):
  `synced_preview` joins the `approvals.py:175-178` compare, so closure keywords
  appearing between preview and consent trip `preview-stale` exactly like a moved sha.
- `promote_to_release` (publication kind) gets the same envelope treatment with
  `action: "promote"` and the release branch.

## Component E — red-proof tests (`super_worker.red_proof`)

A shipped test must *discriminate*: FAIL on the pristine base, PASS on the candidate.

- `develop.py` records `base_sha` before dispatch and threads
  `base_repo=dev_clone, base_sha=...` into `run_code_round` (the pristine clone is
  alive until the `rmtree` — no new clone). The red-proof runs in a fresh
  `git worktree add --detach` at `base_sha` (the clone was worker-reachable; the
  detached worktree at a recorded sha is the cheap integrity answer; Phase 3 owns the
  hostile-worker case).
- Changed test files = `changed` filtered through `reporting/acceptance._is_test`.
  Each runs against the base worktree via the existing `adapter.run_named_test`
  (rc-classified): `failed` OR `missing` (collection error / file absent on base) =
  red-proof satisfied; `passed` = the test does not discriminate ⇒ stage `no_test`,
  reason "test passes on the pristine base", action `discarded` (joins the existing
  `_RETRY_STAGES` retry semantics).
- Knob: `super_worker.red_proof` in `SETTINGS_SPEC` (bool, default **false** for
  bake-in) AND in `harness_surface.FROZEN_KNOB_KEYS` — born frozen, with the
  regression test asserting membership (the module's own history demands it).
- Runs only when `require_test`/the org-chart stage resolves the test gate on; ordered
  right after the `no_test` diff check, before the full suite (fail fast, one file at
  a time, bounded by `run_named_test`'s timeout).

## Component F — claim leases

Mapped reality: no `claimed_at`, no TTL; a task orphaned outside a shift stays
`in_progress` until the next `run_shift` reaps its shift — unbounded when the claim
had no shift or the runner never restarts.

- Migration (ALTER pattern): `tasks.claimed_at` — stamped by `set_task_status`
  whenever status enters `claimed`/`in_progress`.
- `store.reap_expired_task_leases(ttl_minutes, keep_shift_id=None)`: in-flight tasks
  with `claimed_at` older than TTL and not belonging to the currently running shift →
  `open`, with a task-evidence-style note; returns the reclaimed ids.
- Called at shift start beside `reap_orphaned_approvals` (`shift.py:42`) with
  `keep_shift_id=<this shift>`; close-out report line "reclaimed N expired claim(s)".
  CLI: `factory task reap` for manual sweeps.
- Knob: `super_worker.claim_lease_minutes` in `SETTINGS_SPEC` (int, default 240) +
  `SANE_BOUNDS (10, 1440)` — an editable capacity knob, deliberately NOT frozen (the
  harness engineer tuning lease length is legitimate; it cannot disable the sweep).

## Deployment (Component G)

- `deploy/user-factory/04-install-broker-agent.sh` (operator, no root): creates the
  spool + bare with the ownership split above, writes `~/.factory-broker.yaml` from
  the target config if absent, installs `com.factory.broker.plist` as an operator
  **LaunchAgent** (`~/Library/LaunchAgents`, `launchctl bootstrap gui/$UID`,
  WatchPaths on the outbox) — NOT a system daemon; the same bootout-then-bootstrap
  idempotency loop as 03.
- Credential migration note in the runbook: with the broker armed, remove `GH_TOKEN`
  from `~factory/.factory-secrets/env` (and `gh auth setup-git`'s helper) — one move
  disarms push AND issue closure, per the mapped credential topology.
- Runbook: `docs/runbooks/publication-broker.md` — topology diagram, arming steps,
  verification list, receipt semantics, drill-3 procedure, teardown, and the
  single-user dev mode.

## Freezing

New publication machinery joins `FROZEN_SURFACES`: `orchestrator/broker.py`,
`reporting/envelope.py`, `deploy/` (the whole kit is publication/deployment surface).

## Acceptance (drill 3, now executable hermetically)

Integration test with `file://` remotes end-to-end: prepare envelope → move the
"origin" base ref → broker rejects with `base moved`; tamper the envelope → hash
reject; replay a spent nonce → reject; expire → reject; happy path → pushed +
receipt + approval resolved. Red-proof: a test passing on base discards; claim lease:
an expired orphan reopens, the current shift's tasks don't.

## Out of scope (explicit)

Phase 2 intent rows (the envelope/receipt pair IS the publication intent record;
merge/issue-sync intent rows come with Phase 2), worker DB isolation (Phase 3),
two-person approval (parked), Windows broker (the guest-house WSL path publishes
nothing today).
