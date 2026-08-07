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

**AMENDED 2026-08-07** (a security fix round, probe-executed against the first
implementation): the original wording below overclaimed. An allowlist match plus live-
state re-verification proves the envelope is *internally consistent and points at a
live, reachable, fast-forward destination* — it does NOT prove a human ever saw or
approved the *content*. Under the guest-house threat model the factory (and everything
it writes, including an envelope's own `base_sha`/`tip_sha`/`range`/`n_commits` fields)
is attacker-controlled, so a compromised factory could point `tip_sha` at any commit that
happens to be a fast-forward descendant of the real base and have it pass every check the
original design specified. The corrected model, now what the code actually enforces
(`orchestrator/broker.py`):

> The factory PREPARES publications; it never executes them when the broker is armed. An
> envelope is a REQUEST. THREE separate, operator-owned records — never any envelope
> field — are the authority:
> 1. `~/.factory-broker.yaml` (the allowlist) authorizes the **destination**
>    (repo_slug/base_branch -> remote_url/bare_path).
> 2. `~/.factory-broker/pins` (the pin store) authorizes the **content** — `tip_sha` must
>    be a sha the operator explicitly approved, either interactively (reviewing an
>    operator-*derived* diff — `git log`/`git diff --stat` read directly from the bare
>    repo's own objects, never the envelope's claims — and confirming) or ahead of time
>    (`factory broker pin <sha>`). This is what makes "cannot publish anything other than
>    the exact artifact approved" true.
> 3. `~/.factory-broker/spent` (the spent-nonce ledger) authorizes that a nonce has not
>    already been acted on — the real replay guard (never the shared spool's own
>    `.receipt.json` copy, which lives in factory-writable space).
>
> The broker ALSO re-verifies every field against live state immediately before acting:
> content hash, schema version, a single verified nonce identity, expiry, a live
> `git ls-remote` (has anyone moved the branch since approval?), and
> `merge-base --is-ancestor` (fast-forward only, never a force push). Issue actions
> execute only from the envelope's own preview text, validated (never reinterpreted)
> before ever reaching `gh`'s argv. The factory user holds no GitHub credential in broker
> mode, and (round 2) keeps only a read-only `git fetch` capability for the base branch —
> never push, never `gh`.

**What this does NOT defend against:** an allowlist entry with `require_pin: false`
disables the content-authenticity gate for that entry — destination/liveness checks
still apply, but any fast-forward descendant of the live base is then publishable exactly
as the original (unamended) design assumed the allowlist alone already prevented. An
operator who pins a sha without actually reviewing the derived diff gets no protection
either — the pin store records an *action*, not a *reviewed decision*. `watch`
(unattended, no human in the loop) refuses to run without `--unattended` and, even then,
never bypasses `require_pin`: an unpinned tip is left `pending`, never auto-approved —
but a `require_pin: false` entry is unattended-publishable by design, which is a real,
documented trade-off, not an oversight.

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
/Users/Shared/factory-broker/                (shared, group-writable — factory CAN write)
  outbox/    factory writes envelopes; operator/broker reads
  receipts/  broker writes an INFORMATIONAL receipt copy; factory reads (ingestion only)
clive-publish.git   bare repo — the factory pushes tip there LOCALLY (file remote, no
                    credential); the broker pushes from it to GitHub.

~/.factory-broker/                            (operator home, 700 — NEVER group-shared)
  pins    operator-approved tip_shas — THE content-authenticity authority
  spent   append-only spent-nonce ledger — THE replay-guard authority
  processed/  finalized envelope+hash pairs, archived OUT of the shared/factory-writable
              outbox once a verdict is reached (2026-08-07 addition — the old outbox/done
              sat inside the SAME factory-writable tree as outbox itself, so the very
              actor the replay guard exists to stop could re-drop an archived envelope)
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

`factory broker run-once | watch | status | pin <sha> | unpin <sha> | pins` from the
operator's own factory checkout.

- **Allowlist — authorizes the DESTINATION**: `~/.factory-broker.yaml` (operator home,
  600): `publications: [{repo_slug, remote_url, base_branch, bare_path,
  allow_issue_ops: true, require_pin: true}]`. `repo_slug`/`base_branch` are cross-
  checked against the matching allowlist entry; no entry ⇒ reject.
- **Pin store — authorizes the CONTENT** (2026-08-07 addition, CRITICAL-1):
  `~/.factory-broker/pins` (operator home, 700, NEVER the shared/factory-writable
  spool). `require_pin` (default true per entry) means `tip_sha` must be a sha the
  operator explicitly pinned before it can execute — either interactively (`run-once`
  without `--unattended`: render `git log --oneline`/`git diff --stat` read directly
  from the bare repo's own objects — never the envelope's claims — and prompt; a
  confirm pins the tip and proceeds, a decline rejects) or ahead of time
  (`factory broker pin <sha> [--note]`).
- **Spent-nonce ledger — the replay-guard authority**: `~/.factory-broker/spent`
  (operator home, 700, append-only). Never the shared spool's own `.receipt.json` copy
  (informational only — factory-writable, so an attacker who can write there could in
  principle delete/re-create it).
- **Verification, immediately before executing** (any failure ⇒ receipt
  `rejected`/`expired` + reason, never retried silently; an unpinned-but-otherwise-clean
  envelope under `--unattended` gets a distinct SOFT `pending` outcome — not spent, not
  archived, so a later pin + a later run can still complete it):
  1. a SINGLE verified nonce identity (the envelope's own `nonce` field must equal the
     filename it was found under — desynchronizing them must not bypass the ledger).
  2. content hash matches; schema version known; nonce not in the spent ledger; not
     expired.
  3. allowlist entry exists and matches repo_slug/base_branch (bare_path/remote_url are
     read FROM the entry, never from the envelope — an envelope cannot smuggle its own
     destination).
  4. bare repo's `tip_sha` object exists and `git ls-remote <remote_url>
     refs/heads/<base_branch>` == `base_sha` (nobody moved the branch since approval —
     drill 3's exact check).
  5. `git -C <bare> merge-base --is-ancestor <base_sha> <tip_sha>` (fast-forward only,
     never a force push).
  6. `require_pin` ⇒ `tip_sha` must be pinned (else `pending` unattended / interactive
     prompt).
  7. `policy_hash` recorded into the receipt (informational — operator config may
     legitimately differ).
- **Execute**: `git -C <bare> push <remote_url> <tip_sha>:refs/heads/<base_branch>`
  with the OPERATOR's credential; then issue actions from the envelope preview only
  (`gh issue close/comment`), each field-validated (a positive int `number`, `op` in
  `{close, comment}`, never reinterpreted) and capped
  (`MAX_ISSUE_ACTIONS`) before ever touching `gh`'s argv. Write receipt `pushed` with
  the resulting remote sha; mark the nonce spent in the ledger.
- `watch` = poll loop with backoff; REFUSES to run without `unattended=True` (a
  persistent loop can't prompt a human) — `require_pin` stays fully in force regardless.
  The LaunchAgent uses WatchPaths on the outbox, passes `--unattended`, and just runs
  `run-once`.

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
reject; replay a spent nonce → reject (via the operator-owned ledger, not the
factory-writable receipt copy — deleting the latter must not un-spend it); expire →
reject; happy path (a PINNED tip) → pushed + receipt + approval resolved. Red-proof: a
test passing on base discards; claim lease: an expired orphan reopens, the current
shift's tasks don't.

**2026-08-07 addition, the content-authenticity drill:** a factory-chosen tip that is a
valid fast-forward descendant of the live base, matching every allowlist/liveness check,
must still be REFUSED when unpinned (`tests/test_broker.py::
test_critical1_unpinned_malicious_tip_never_reaches_the_real_remote`) — and only proceeds
once the operator has explicitly pinned that exact sha.

## Out of scope (explicit)

Phase 2 intent rows (the envelope/receipt pair IS the publication intent record;
merge/issue-sync intent rows come with Phase 2), worker DB isolation (Phase 3),
two-person approval (parked), Windows broker (the guest-house WSL path publishes
nothing today).
