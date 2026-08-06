# Publication broker runbook

Companion to `docs/plans/2026-08-06-publication-broker-design.md` (Phase 1 of
`docs/plans/2026-08-06-production-hardening-roadmap.md`). Implements the reviewer's
sign-off sentence in code:

> Even a total compromise of the guest-house account cannot silently publish different
> code from what the operator approved.

## The authority line

> The factory PREPARES publications; it never executes them when the broker is armed. An
> envelope is a REQUEST — the broker's own operator-side allowlist, never any envelope
> field, is the authority on what may be pushed where. The broker re-verifies every sha
> against live state immediately before acting; mismatch, expiry, or a reused nonce
> invalidates the envelope permanently. Issue actions execute only from the approved
> preview. The factory user holds no GitHub credential when armed.

Everything below exists to make that sentence true, and to let you verify it's true
rather than take it on faith.

## Topology

```
  ┌──────────────────────────── factory user (guest house) ────────────────────────────┐
  │                                                                                     │
  │  graduation/promotion approved  ─►  local ff-merge + retest  ─►  push tip to the    │
  │                                       (unchanged path)             LOCAL bare repo  │
  │                                                                    (file remote —   │
  │                                                                    NO credential)   │
  │                                          │                              │           │
  │                                          ▼                              ▼           │
  │                                  write envelope.json          clive-publish.git     │
  │                                  + envelope.json.sha256        (bare, group-shared) │
  │                                          │                                          │
  └──────────────────────────────────────────┼──────────────────────────────────────────┘
                                              │  spool: outbox/ (factory writes,
                                              │  operator/broker reads via 'staff' group)
                                              ▼
  ┌──────────────────────────── operator account (broker) ─────────────────────────────┐
  │                                                                                     │
  │  factory broker run-once / watch  (LaunchAgent, WatchPaths on outbox)               │
  │      1. content hash matches (tamper/truncation guard)                             │
  │      2. schema_version known, not expired, nonce has no receipt yet                │
  │      3. ~/.factory-broker.yaml allowlist has a matching repo_slug/base_branch entry │
  │         (no entry ⇒ reject — THIS is the authority, not the envelope)              │
  │      4. git ls-remote <remote_url> refs/heads/<base_branch> == envelope.base_sha    │
  │         (has anyone moved the branch since approval? drill 3's exact check)        │
  │      5. git merge-base --is-ancestor base_sha tip_sha  (fast-forward ONLY)          │
  │                                          │                                          │
  │                          ok ─────────────┼───────────── any failure                 │
  │                           │                                    │                    │
  │                           ▼                                    ▼                    │
  │   git push <remote_url> tip_sha:refs/heads/<base_branch>   receipt: rejected/expired │
  │   gh issue comment/close (from the envelope's own preview                           │
  │   text, never re-derived) — the OPERATOR's own credential                           │
  │                           │                                                         │
  │                           ▼                                                         │
  │                  receipt: pushed + receipt_sha                                      │
  │                                          │                                          │
  └──────────────────────────────────────────┼──────────────────────────────────────────┘
                                              │  spool: receipts/ (broker writes,
                                              │  factory reads)
                                              ▼
                        factory broker-receipts  (shift start + on demand)
                        resolves the pending_approvals row: approved / rejected
```

Dev/single-user mode: the same code paths run under ONE account (operator == factory
user) — the spool defaults to `<factory>/state/broker/{outbox,receipts}` and a local bare
at `<factory>/state/broker/clive-publish.git` instead of `/Users/Shared/factory-broker/`;
override either with `FACTORY_BROKER_SPOOL`/`FACTORY_BROKER_BARE`/
`FACTORY_BROKER_ALLOWLIST` (see `common/paths.py`). Useful for development and for the
hermetic test suite (`tests/test_broker.py`, `tests/test_envelope.py`) — never a
substitute for the real cross-account split in production; a single-user deployment gets
NO credential separation at all (the same account holds both halves).

## Prerequisites

- The guest-house deployment (`deploy/user-factory/01-03`) already installed and running
  — see `docs/runbooks/factory-user-deployment.md`.
- You (the OPERATOR) have your OWN working `gh`/git credential for the target repo,
  independent of the factory user's `GH_TOKEN` — `gh auth status` succeeds as yourself.
- A factory checkout of your own, on your own account (the single-line installer's
  convention: clone it as `factory`, e.g. `~/factory`).

## Install

From your own factory checkout, as yourself (no `sudo`, and NOT as the `factory` user):

```bash
bash deploy/user-factory/04-install-broker-agent.sh          # real install
bash deploy/user-factory/04-install-broker-agent.sh --dry-run  # preview only, no changes
```

This creates:

- `/Users/Shared/factory-broker/{outbox,receipts}` (each with a `done/` subdir) — group-
  shared via the `staff` group every regular macOS account belongs to by default (the same
  mechanism `01-create-user.sh` already uses for the code-handoff bare repo). This is
  **not** a literal `chown factory:staff` — that needs root, which this script deliberately
  never asks for; `install -d -m 2775 -g staff` (setgid, group-writable) plus
  `git init --bare --shared=group` on the publish repo achieve the same cross-user access
  without it.
- `/Users/Shared/factory-broker/clive-publish.git` — the local bare repo the factory
  pushes candidate tips to and the broker pushes FROM to the real remote.
- `~/.factory-broker.yaml` (600, your home — **only written if absent**, never overwritten
  on a rerun) — pre-filled with `base_branch`/`bare_path` derived from the factory
  checkout's `config.yaml` where possible; `remote_url` is always left as `CHANGE-ME` for
  you to fill in by hand (this script must never guess a push target).
- `~/Library/LaunchAgents/com.factory.broker.plist`, bootstrapped into `gui/$UID`
  (idempotent — bootout-then-bootstrap on a rerun, mirroring `03-install-daemons.sh`'s own
  loop). `WatchPaths` on the outbox fires `broker run-once` per envelope;
  `StartInterval=300` is a belt-and-suspenders poll.

Re-run any time to pick up a new plist; it never touches an already-written
`~/.factory-broker.yaml`.

## Arming

The broker being *installed* does nothing on its own — the factory only starts preparing
envelopes (instead of pushing directly) once **both**:

1. `autonomy.publication_broker: true` in the factory's `config.yaml` (default `false`).
   Frozen via the `autonomy.` prefix like every other brake in that block — a deliberate
   file edit, never a board click.
2. `~/.factory-broker.yaml` has a real, reviewed entry (not `CHANGE-ME`) for the target's
   `repo_slug`/`base_branch` — see the allowlist shape below.

**Arm in this order** (never the reverse — a broker with no allowlist entry just rejects
every envelope, which is safe; `publication_broker: true` with a stale/wrong allowlist is
also safe, same reason — but going config-first means you're covered even if you forget
step 2 immediately):

1. Edit `~/.factory-broker.yaml`:
   ```yaml
   publications:
     - repo_slug: ikangai/clive
       remote_url: git@github.com:ikangai/clive.git   # YOUR credential pushes here
       base_branch: chore/extract-factory
       bare_path: /Users/Shared/factory-broker/clive-publish.git
       allow_issue_ops: true
     # a SECOND entry, same shape, for target.release_branch (promotion envelopes,
     # action=promote) if you also approve publications from the queue
   ```
2. Verify the broker can actually reach the entry:
   `bin/factory broker status` (from your own checkout) — lists pending envelopes/
   receipts, no side effects.
3. **Remove the factory user's GitHub credential** — this is the step that actually
   disarms direct push AND issue-closure from the guest house, so it is not optional:
   ```bash
   # as the factory user (fast-user-switch or sudo -u factory -i):
   #   edit ~/.factory-secrets/env — comment out/remove the GH_TOKEN export line
   sudo -u factory -i gh auth logout       # drops gh's git credential helper too
   ```
   Verify: `sudo -u factory -i git -C ~/fab/clive push origin HEAD:some-throwaway-branch`
   must now FAIL (no credential) — that failure is the proof the boundary is real, not
   a bug to fix.
4. Set `autonomy.publication_broker: true` in the factory's `config.yaml` (on the branch
   the factory actually runs — see `apply-config-overlay.py`/the deploy branch convention
   if you run the guest-house deployment).
5. Run the drill-3 procedure below before trusting any of this unattended.

## Verification checklist

- [ ] `bin/factory broker status` runs cleanly from your own account with no errors.
- [ ] `~/.factory-broker.yaml` is `600`, owned by you, and has no `CHANGE-ME` left in an
      entry you intend to use.
- [ ] `launchctl print gui/$(id -u)/com.factory.broker` shows the agent loaded, pointed at
      your real checkout path, `WatchPaths` on the real outbox dir.
- [ ] The factory user's `GH_TOKEN` is gone from `~factory/.factory-secrets/env` and
      `gh auth status` (as factory) reports logged out.
- [ ] A test push from the factory user's account to the real remote fails for lack of
      credential (the step 3 verification above).
- [ ] `autonomy.publication_broker: true` is set on the branch the factory actually runs.
- [ ] The drill-3 procedure (below) has been run at least once, for real, on this
      deployment.

## Receipt semantics

A receipt (`<nonce>.receipt.json` in `receipts/`, archived to `receipts/done/` once
ingested) is the broker's **permanent, one-shot** verdict on one envelope:

- `pushed` — the real push succeeded; `receipt_sha` is the pushed tip. Issue actions from
  the envelope's own preview ran too (best-effort per action — one `gh` failure doesn't
  undo the push or block the others; failures show up in the broker's own log, not the
  receipt).
- `rejected` — any verification check failed (bad hash, no allowlist entry, base moved,
  not-a-fast-forward, a real push that itself failed) — `detail` names the reason.
- `expired` — the envelope outlived `autonomy.envelope_ttl_hours` (default 24) before the
  broker ever got to it (e.g. your machine was asleep). Distinct status from `rejected` so
  it's easy to tell "the operator wasn't around in time" from "something was wrong".

**A nonce with a receipt is spent forever** — `orchestrator/broker.py:verify_envelope`
refuses to re-execute it even if somehow re-presented (the replayed-nonce drill). The
factory-side approval that produced the envelope stays `pending_approvals.status =
'executing'` until a receipt shows up; `reporting.approvals.ingest_broker_receipts`
resolves it (`approved` on `pushed`, `rejected` otherwise) — called automatically at every
shift start and on demand via `factory broker-receipts`. `orchestrator/shift.py` also
widens the orphan-approval reaper's grace period while the broker is armed (an envelope
legitimately outlives the default 1h "probably crashed" floor while your broker is simply
offline).

A **rejected/expired envelope is never retried silently.** The operator's Approve click
made local progress (the tip is safely on `factory/auto` and the local bare repo either
way) — re-approving the same graduation from the Queue tab prepares a fresh envelope with
a fresh nonce and fresh shas.

## Drill-3 procedure

Drill 3 (`docs/plans/2026-08-06-production-hardening-roadmap.md` Part 5): *change the
branch/candidate after approval — execution rejected.* The code-level proof lives in `tests/test_broker.py`
(`test_verify_rejects_base_moved_since_approval`, `test_verify_rejects_tampered_envelope`,
`test_verify_rejects_a_spent_nonce_replay`, `test_verify_rejects_an_expired_envelope`, and
`test_verify_happy_path`) — real local git repos, no mocks, exercised on every test run. To rehearse
it live, against your own real deployment:

1. **Base moved.** Approve a graduation (broker armed). BEFORE running
   `factory broker run-once`, push any other commit directly to the real remote's base
   branch (from anywhere else with access). Run `factory broker run-once` — expect a
   `rejected` receipt citing "base moved"; confirm the intruding commit is untouched on
   the remote (nothing force-pushed over it).
2. **Tamper.** Find the pending envelope under `outbox/`, hand-edit a field in the `.json`
   (leave the `.sha256` sidecar alone). Run `run-once` — expect `rejected`, "content hash
   mismatch".
3. **Replayed nonce.** After a successful `pushed` receipt, manually re-drop the SAME
   envelope+hash files back into `outbox/` (they were archived to `outbox/done/` — copy
   them back). Run `run-once` — expect `rejected`, "already has a receipt".
4. **Expiry.** Approve a graduation, then don't run the broker until
   `autonomy.envelope_ttl_hours` has passed (or temporarily set it very low for the
   drill). Run `run-once` — expect an `expired` receipt.
5. **Happy path.** Approve a graduation with nothing else changed; run `run-once`; confirm
   `pushed`, the real remote now has the new tip, and `factory broker-receipts` resolves
   the approval to `approved`.

Record the outcome of each step (pass/fail + evidence) in this file's revision history or
your own ops log before marking the drill complete for a given deployment.

## Teardown

```bash
launchctl bootout gui/$(id -u)/com.factory.broker
rm -f ~/Library/LaunchAgents/com.factory.broker.plist
# leave the spool + bare repo in place unless you're fully decommissioning — the
# outbox/receipts history is useful forensics. To remove everything:
rm -rf /Users/Shared/factory-broker
rm -f ~/.factory-broker.yaml
```

Disarming without a full teardown: set `autonomy.publication_broker: false` — the factory
returns to pushing directly (re-provision `GH_TOKEN` on the factory user first, or nothing
will publish at all).

## Troubleshooting

- **`factory broker run-once` reports nothing but you approved a graduation.** Check
  `outbox/` directly — if it's empty, the factory-side prepare step (`graduate_and_push`
  via `execute_approval`) may have failed before writing the envelope; check the
  `operator_actions` audit table / the dashboard Queue tab note.
- **Every envelope rejects with "no allowlist entry".** `repo_slug`/`base_branch` in
  `~/.factory-broker.yaml` must match EXACTLY what `config.target_repo_slug()` /
  `target.base_branch` resolve to on the factory side — a trailing slash or a stale
  branch name is the usual cause.
- **The LaunchAgent won't load / `launchctl bootstrap` errors.** GUI LaunchAgents require
  an actual logged-in GUI session for the target UID — `launchctl bootstrap gui/$UID`
  from a headless SSH session with no Aqua session behind it can fail in ways this
  runbook's own dry-run pass cannot reproduce; log in at the console (or via Screen
  Sharing) once, then retry.
- **This host's Bash sandbox could not fully exercise the agent.** During this feature's
  own development, `launchctl bootstrap` succeeded and the agent loaded with the correct
  configuration, but the actual spawned process failed with "Operation not permitted" —
  consistent with the dev sandbox's own process/filesystem restrictions (a sandboxed
  shell's paths are not necessarily reachable the same way by an unsandboxed `launchd`),
  not a bug in the plist/script. Verify on a normal, unsandboxed macOS session before
  relying on this in production; `bash -n` + `shellcheck` + `--dry-run` + plist XML
  validation (all clean) are as far as this could be verified in that environment.
