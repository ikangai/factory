# Publication broker runbook

Companion to `docs/plans/2026-08-06-publication-broker-design.md` (Phase 1 of
`docs/plans/2026-08-06-production-hardening-roadmap.md`). Implements the reviewer's
sign-off sentence in code:

> Even a total compromise of the guest-house account cannot silently publish different
> code from what the operator approved.

## The authority line

**Corrected 2026-08-07** after a probe-executed security review: the original wording
(destination-match + live-state re-verification) proved internal consistency and a live,
reachable, fast-forward destination — it did NOT prove a human had approved the
*content*. A compromised factory could point `tip_sha` at any commit that happened to be
a fast-forward descendant of the real base and pass every check. The corrected model,
what the code actually enforces today:

> The factory PREPARES publications; it never executes them when the broker is armed. An
> envelope is a REQUEST. The operator's own allowlist (`~/.factory-broker.yaml`)
> authorizes the **destination**. The operator's own pin store
> (`~/.factory-broker/pins`) authorizes the **content** — a `tip_sha` must be a sha the
> operator explicitly reviewed and approved, never inferred from the envelope alone. The
> operator's own append-only ledger (`~/.factory-broker/spent`) is the **replay-guard**
> authority. The broker re-verifies every field against LIVE state immediately before
> acting; mismatch, expiry, or a reused nonce invalidates the envelope permanently. Issue
> actions execute only from the approved preview, field-validated before ever reaching
> `gh`'s argv. The factory user holds no GitHub push/issue credential when armed (it
> keeps a read-only `git fetch` for the base branch — see "Credential model" below).

**What this does NOT defend against**, read before you rely on any of it:

- An allowlist entry with `require_pin: false` disables the content-authenticity gate for
  that entry — you get destination + liveness checks only, exactly what the *original*
  (now-corrected) design assumed was already the whole story. Never set this without
  understanding you're opting back INTO the original gap.
- Pinning a sha without actually reading the diff you're shown gives you nothing — the
  pin store records that you *acted*, not that you *reviewed*. Read the operator-derived
  commits/diffstat every single time.
- `watch`/the LaunchAgent (`--unattended`) never bypasses `require_pin` — an unpinned tip
  is left `pending`, never auto-approved — but a `require_pin: false` entry publishes
  unattended by design by the same reasoning as the first bullet.

Everything below exists to make the corrected sentence true, and to let you verify it's
true rather than take it on faith.

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
                                              │  SHARED spool: outbox/ (factory writes,
                                              │  operator/broker reads via 'staff' group)
                                              ▼
  ┌──────────────────────────── operator account (broker) ─────────────────────────────┐
  │                                                                                     │
  │  factory broker run-once / watch  (LaunchAgent: run-once --unattended, WatchPaths)  │
  │      1. a SINGLE verified nonce identity (envelope content == the filename)         │
  │      2. content hash matches; schema known; nonce not in ~/.factory-broker/spent    │
  │         (operator-owned ledger — the SHARED receipts/ copy is informational only)   │
  │      3. not expired                                                                 │
  │      4. ~/.factory-broker.yaml allowlist has a matching repo_slug/base_branch entry  │
  │         — DESTINATION authority (bare_path/remote_url come from HERE, never the      │
  │         envelope) — no entry ⇒ reject                                                │
  │      5. git ls-remote <remote_url> refs/heads/<base_branch> == envelope.base_sha     │
  │         (has anyone moved the branch since approval? drill 3's exact check)          │
  │      6. git merge-base --is-ancestor base_sha tip_sha  (fast-forward ONLY)           │
  │      7. require_pin (default true) ⇒ tip_sha in ~/.factory-broker/pins — CONTENT     │
  │         authority. Unpinned: unattended ⇒ 'pending' (left for later, not spent);     │
  │         interactive ⇒ show the operator-DERIVED diff (read from the bare repo's OWN  │
  │         objects, never the envelope's claims) and prompt                             │
  │                                          │                                           │
  │           confirmed/pinned ──────────────┼───────────── declined / any other failure │
  │                           │                                    │                     │
  │                           ▼                                    ▼                     │
  │   git push <remote_url> tip_sha:refs/heads/<base_branch>   mark ~/.factory-broker/    │
  │   gh issue comment/close (field-validated, capped,          spent + receipt:         │
  │   from the envelope's own preview text — the                rejected/expired         │
  │   OPERATOR's own credential)                                                         │
  │                           │                                                          │
  │                           ▼                                                          │
  │        mark ~/.factory-broker/spent + receipt: pushed + receipt_sha                  │
  │                                          │                                           │
  └──────────────────────────────────────────┼───────────────────────────────────────────┘
                                              │  SHARED spool: receipts/ (broker writes an
                                              │  INFORMATIONAL copy; factory reads for
                                              │  ingestion only — NOT the spend authority)
                                              ▼
                        factory broker-receipts  (shift start + on demand)
                        resolves the pending_approvals row: approved / rejected
```

Dev/single-user mode: the same code paths run under ONE account (operator == factory
user) — the shared spool defaults to `<factory>/state/broker/{outbox,receipts}` and a
local bare at `<factory>/state/broker/clive-publish.git`; the operator-owned authority
dir defaults to `~/.factory-broker/` either way (it's never inside the shared spool).
Override any of these with `FACTORY_BROKER_SPOOL`/`FACTORY_BROKER_BARE`/
`FACTORY_BROKER_ALLOWLIST`/`FACTORY_BROKER_OPERATOR_DIR`/`FACTORY_BROKER_PINS`/
`FACTORY_BROKER_SPENT`/`FACTORY_BROKER_PROCESSED` (see `common/paths.py`). Useful for
development and for the hermetic test suite (`tests/test_broker.py`,
`tests/test_envelope.py`) — never a substitute for the real cross-account split in
production; a single-user deployment gets NO credential separation at all AND no real
content-authenticity boundary either (the same account both writes envelopes and pins
them) — it only exercises the mechanism, not the security property.

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

- `/Users/Shared/factory-broker/{outbox,receipts}` (each with a `done/` subdir) — SHARED,
  group-writable via the `staff` group every regular macOS account belongs to by default
  (the same mechanism `01-create-user.sh` already uses for the code-handoff bare repo).
  This is **not** a literal `chown factory:staff` — that needs root, which this script
  deliberately never asks for; `install -d -m 2775 -g staff` (setgid, group-writable) plus
  `git init --bare --shared=group` on the publish repo achieve the same cross-user access
  without it. The factory user CAN write here — that's the design (a one-way handoff) —
  which is exactly why nothing security-critical is decided by reading this tree; see the
  next bullet.
- `/Users/Shared/factory-broker/clive-publish.git` — the local bare repo the factory
  pushes candidate tips to and the broker pushes FROM to the real remote.
- **`~/.factory-broker/` (700, your home, operator-only — the factory user CANNOT write
  here)** — `pins` (content approvals) and `spent` (the replay-guard ledger). This is the
  actual security boundary the earlier bullets are not.
- `~/.factory-broker.yaml` (600, your home — **only written if absent**, never overwritten
  on a rerun) — pre-filled with `base_branch`/`bare_path` derived from the factory
  checkout's `config.yaml` where possible; `remote_url` is always left as `CHANGE-ME` for
  you to fill in by hand (this script must never guess a push target); `require_pin: true`
  is written explicitly so you see the knob and its default.
- `~/Library/LaunchAgents/com.factory.broker.plist`, bootstrapped into `gui/$UID`
  (idempotent — bootout-then-bootstrap on a rerun, mirroring `03-install-daemons.sh`'s own
  loop), running `broker run-once --unattended`. `WatchPaths` on the outbox fires it per
  envelope; `StartInterval=300` is a belt-and-suspenders poll. `--unattended` never
  bypasses `require_pin` — see "The authority line" above.

Re-run any time to pick up a new plist; it never touches an already-written
`~/.factory-broker.yaml` or an existing `~/.factory-broker/` dir.

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
       require_pin: true    # CONTENT authorization — see "what this does not defend
                             # against" before you ever set this false
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
   **Verify BOTH halves — this is the whole credential model, and only one half is about
   what must fail:**
   ```bash
   # PUSH must now FAIL — that failure is the proof the boundary is real, not a bug:
   sudo -u factory -i git -C ~/fab/clive push origin HEAD:some-throwaway-branch
   # FETCH must still SUCCEED — the armed prepare path refuses to prepare without it:
   sudo -u factory -i git -C ~/fab/clive fetch origin <base-branch>
   ```
   The factory keeps a **read-only fetch** capability and loses push + `gh`. That is
   deliberate: `graduate_and_prepare_envelope` fetches `origin/<base>` and **fails closed**
   (`reason: fetch-failed`, nothing prepared) before pinning `base_sha`. Without the fetch
   the local ref goes stale the moment the broker's first push lands, and every subsequent
   envelope would pin a `base_sha` the broker correctly rejects as "base moved" — a
   self-inflicted deadlock that looks exactly like an attack.

   For a **public** target, unauthenticated HTTPS fetch is enough — removing `GH_TOKEN`
   costs you nothing. For a **private** target, leave the factory a fetch-only credential
   (a fine-grained PAT with read-only Contents on that one repo, or a read-only deploy
   key); it can pull, it cannot publish. If you see repeated "base moved" rejections, check
   the fetch half above first — a broken fetch is the usual cause, not a hostile factory.
4. Set `autonomy.publication_broker: true` in the factory's `config.yaml` (on the branch
   the factory actually runs — see `apply-config-overlay.py`/the deploy branch convention
   if you run the guest-house deployment). Also confirm `autonomy.push_approval: true` —
   the broker only intercepts the path that already goes through an operator Approve
   click; verify by hand (as of this writing the code does not yet refuse the
   contradictory combination for you) that a graduation still files a Queue-tab approval
   rather than pushing directly.
5. Content approval is a SEPARATE, ongoing act, not a one-time setup step: either run
   `factory broker run-once` BY HAND (no `--unattended`) and review+confirm each
   publication as it comes, or pre-approve specific tips with `factory broker pin <sha>`.
   The LaunchAgent runs unattended and will never do this for you.
6. Run the drill-3 procedure below before trusting any of this unattended.

## Verification checklist

- [ ] `bin/factory broker status` runs cleanly from your own account with no errors.
- [ ] `~/.factory-broker.yaml` is `600`, owned by you, and has no `CHANGE-ME` left in an
      entry you intend to use; `require_pin` is `true` unless you have a specific,
      documented reason to disable it for that entry.
- [ ] `~/.factory-broker/` exists, is `700`, owned by you — `ls -la ~/.factory-broker`
      from the FACTORY user's account must fail (Permission denied).
- [ ] `launchctl print gui/$(id -u)/com.factory.broker` shows the agent loaded, pointed at
      your real checkout path, `WatchPaths` on the real outbox dir, `--unattended` in
      the arguments.
- [ ] The factory user's `GH_TOKEN` is gone from `~factory/.factory-secrets/env` and
      `gh auth status` (as factory) reports logged out; a test push (see step 3 above)
      fails for lack of credential.
- [ ] …and a test **fetch** (step 3 above) still SUCCEEDS as the factory user. Push must
      fail; fetch must work. A factory that cannot fetch prepares nothing (fail-closed)
      and every publication after the first would deadlock on "base moved".
- [ ] Both sides resolve the SAME spool: `bin/factory broker status` as the operator and
      as the factory user print identical outbox/receipts paths. (The factory reads
      `autonomy.broker_spool_root` from its config.yaml; the broker reads
      `FACTORY_BROKER_SPOOL` from its LaunchAgent plist. If they disagree, envelopes are
      written where nothing reads them and receipts are read where nothing writes them —
      a silent, permanent no-op in both directions.)
- [ ] `autonomy.publication_broker: true` AND `autonomy.push_approval: true` are BOTH set
      on the branch the factory actually runs — verified by hand (see step 4's note: the
      code does not yet refuse the contradictory combination for you).
- [ ] `python3 scripts/drill3_rehearse.py --dir /tmp/drill3` (the drill-3 procedure below)
      has been run on THIS machine and reported every step green. It rehearses against a
      throwaway stand-in deployment, so it is safe to run whenever you like — and it proves
      the code and policy, never this deployment's credential split, which is what the
      three checklist items above it are for.
- [ ] You have personally run `factory broker run-once` interactively at least once and
      confirmed the diff shown matches what you expected — not just that the flow works.

## Content approval: pinning

`factory broker pin <tip_sha> [--note "why"]` records that YOU reviewed and approve
exactly that commit, in `~/.factory-broker/pins` (700, operator-only). `factory broker
unpin <tip_sha>` removes it. `factory broker pins` lists everything currently pinned.

You will normally never need to pin ahead of time — running `factory broker run-once` by
hand (the default, no `--unattended`) shows you the operator-*derived* diff (read
straight from the bare repo's own git objects: `git log --oneline base..tip`,
`git diff --stat base..tip`, changed paths — never the envelope's own claimed
`range`/`n_commits`, which are labeled as unverified in the prompt) and pins for you on
confirmation. Pre-pinning is for a specific, reviewed tip you already know is coming
(e.g. from a `factory broker status`/Queue-tab preview) and want the unattended
LaunchAgent to be able to publish without you present.

**Pinning a sha you have not actually reviewed defeats the entire point** — the pin store
records an action, not a verified decision. Read the diff.

## Receipt semantics

A receipt (`<nonce>.receipt.json` in `receipts/`, archived out of the shared spool into
`~/.factory-broker/processed/` — operator-owned, NOT the old factory-writable
`outbox/done/` — once finalized) is the broker's **permanent, one-shot** verdict on one
envelope:

- `pushed` — the real push succeeded; `receipt_sha` is the pushed tip. Issue actions from
  the envelope's own preview ran too (best-effort per action, each field-validated before
  ever reaching `gh` — one `gh` failure doesn't undo the push or block the others;
  failures show up in the broker's own log, not the receipt).
- `rejected` — any verification check failed (bad hash, no allowlist entry, base moved,
  not-a-fast-forward, a real push that itself failed, or an interactive decline) —
  `detail` names the reason.
- `expired` — the envelope outlived `autonomy.envelope_ttl_hours` (default 24) before the
  broker ever got to it (e.g. your machine was asleep). Distinct status from `rejected` so
  it's easy to tell "the operator wasn't around in time" from "something was wrong".
- **`pending`** (not a receipt status — no receipt is written for it) — the envelope
  verified clean on destination/liveness but its `tip_sha` isn't pinned, and either the run
  was `--unattended` **or** there was no operator to ask (stdin is not a terminal, or piped
  answers ran out mid-queue — `NoOperatorAvailable`, drill 3's own finding, fixed
  2026-08-16: before that, "nobody was there" was recorded as `rejected`/"operator
  declined", spending the nonce and writing an audit line no operator ever made). Left
  untouched in `outbox/`, NOT marked spent: a later `factory broker pin <sha>` followed by
  any later run (interactive or unattended) can still complete it. This is the ONLY outcome
  that doesn't finalize — everything else is permanent.

**A nonce that reaches ANY terminal outcome is spent forever**, authoritatively via
`~/.factory-broker/spent` (operator-owned, append-only) — `orchestrator/broker.py:
verify_envelope` refuses to re-execute it even if somehow re-presented (the replayed-nonce
drill), and deleting the informational `receipts/` copy does NOT un-spend it (that copy
lives in the same factory-writable tree as the outbox — it is not the authority; see "The
authority line"). The factory-side approval that produced the envelope stays
`pending_approvals.status = 'executing'` until a receipt shows up; `reporting.approvals.
ingest_broker_receipts` resolves it (`approved` on `pushed`, `rejected` otherwise) —
called automatically at every shift start and on demand via `factory broker-receipts`.
`orchestrator/shift.py` also widens the orphan-approval reaper's grace period while the
broker is armed (an envelope legitimately outlives the default 1h "probably crashed"
floor while your broker is simply offline — including while it sits `pending`,
unpinned).

A **terminal rejected/expired envelope is never retried silently.** The operator's
Approve click made local progress (the tip is safely on `factory/auto` and the local bare
repo either way) — re-approving the same graduation from the Queue tab prepares a fresh
envelope with a fresh nonce and fresh shas. A `pending` (unpinned, unattended) envelope is
different: it is NOT dead — pin the tip and rerun.

## Drill-3 procedure

Drill 3 (`docs/plans/2026-08-06-production-hardening-roadmap.md` Part 5): *change the
branch/candidate after approval — execution rejected.*

**There are two boundaries, not one, and they fail differently:**

| | in force when | refuses with |
|---|---|---|
| **factory-side consent gate** — `reporting/approvals.py: execute_approval` | ALWAYS, armed or not. The only one in force at `publication_broker: false` | `preview-stale`: the card pins range + commit count + BOTH endpoint shas; approving re-derives under the repo lock and refuses when any moved |
| **operator-side broker** — `orchestrator/broker.py: verify_envelope` | armed only | `rejected`/`expired`/`pending`: allowlist authorizes the destination, live `ls-remote` the base, the pin store the CONTENT, the spent ledger kills replays |

An unarmed deployment still gets drill 3's guarantee, from the first row alone. Both rows
are drilled together below.

### Rehearse it on a throwaway deployment — never on production

```bash
python3 scripts/drill3_rehearse.py --dir /tmp/drill3            # both halves, ~5s
python3 scripts/drill3_rehearse.py --dir /tmp/drill3 --half broker
```

Nineteen steps, each printing PASS/FAIL with its own evidence, plus `results.json`; exit
non-zero if any guard behaved differently than the authority line requires. It builds a
complete stand-in deployment under `--dir`: the "remote" is a local bare repo, the
allowlist/pins/spent-ledger are throwaway files, and `FACTORY_BROKER_*` points every path
inside it. `tests/test_drill3_rehearse.py` runs the whole thing on every test run, so the
drill cannot rot into a script nobody executes.

**The earlier version of this procedure told you to push a commit directly to the real
remote's base branch (to trip the base-moved check) and then to publish for real (the happy
path).** Both test a guard by performing, on production, the act the guard exists to
prevent — the shape this repo's standing rule forbids: never probe a boundary with
something that acts on success (the rule exists because an early boundary probe cleared the
live killswitch; drill 4's refusal steps needed the same correction). Nothing drilled here
needs the real remote: every check is a property of the code plus your own
allowlist/pins/ledger. The harness additionally sets `allow_issue_ops: false`, writes no
issue actions, and routes the factory half's git through a runner that refuses `gh`, so no
step can reach GitHub even if a guard failed outright.

What the rehearsal therefore does NOT prove — these are deployment properties, and the
"Verification checklist" above is where they're checked: that the credential is really
split (factory cannot push, you can), that `~/.factory-broker/` is unreadable from the
factory account, and that both sides resolve the same spool.

### What each step drills

*Factory half — the candidate/base changed after the card was approved:*

1. an extra commit on `factory/auto` after approval → `preview-stale`, nothing pushed, the
   card's payload refreshed in place and the row returned to `pending` for a re-decision;
2. an **amend** — identical commit count, different content (the case a count-only check
   was blind to before the endpoint shas joined the compare) → `preview-stale`;
3. the base moved under the card (an upstream push) → `preview-stale`, the upstream commit
   untouched;
4. the same swap with the **broker armed** → refused BEFORE the prepare step: no envelope
   is written, nothing is pushed to the local bare, there is nothing for an operator to be
   asked about later;
5. the positive control — nothing changed, the push lands. Without it the four refusals
   above prove only that it refuses.

*Broker half — the envelope changed after the operator pinned the content:*

6. base moved between approval and execution (the live `ls-remote` re-verification);
7. the envelope hand-edited, sidecar left alone → content-hash mismatch;
8. the **branch** swapped — and the envelope even supplies its own `remote_url`/`bare_path`
   to be sure they are ignored (destination comes from the allowlist, never the envelope);
9. the **repository** swapped to one that was never allowlisted;
10. the **content** swapped for something structurally perfect but unreviewed — correct
    live base, fresh nonce, own recomputed hash, a genuine fast-forward. Destination +
    liveness checks alone would have published this; only the pin gate stops it, and it
    stops it as `pending`, not as a failure;
11. a pinned tip that would rewrite rather than extend the base (never a force push);
12. filename nonce desynchronized from the envelope's own nonce (replay-guard identity);
13. expiry;
14. an interactive run with **no tty** — the queue survives it, unspent (see below);
15. the positive control — the operator reads the operator-*derived* diff (with the
    envelope's deliberately false `n_commits: 99` claim shown as unverified) and confirms;
16. + 17. the same envelope re-dropped into the outbox — rejected as spent, and still
    rejected after deleting the receipt copy in `receipts/` (the operator-owned ledger is
    the authority, not the factory-writable receipt);
18. the envelope declined at the prompt is terminal — spent, archived, never pinned.

### Two things about interactive runs, learned by running this

- **An interactive `run-once` walks the WHOLE outbox, oldest first, and prompts per
  envelope.** A deferred (`pending`, unpinned) envelope is therefore prompt #1 the next
  time you run interactively — ahead of the publication you actually sat down to approve.
  The first execution of this drill piped a bare `y` into a queue of two and published the
  candidate nobody had reviewed. Read the nonce and the derived diff in each prompt; run
  `factory broker status` first if you want to know what's queued.
- **Answer per prompt, or pass `--unattended`.** With no tty (a script, a cron entry, ssh
  with stdin closed) the run now leaves everything `pending` rather than declining it —
  but before 2026-08-16 it recorded every queued envelope as "operator declined", spent
  forever.

Record the outcome (pass/fail + evidence) below, or in your own ops log, before marking the
drill complete for a given deployment.

### Executed 2026-08-16 — 19/19, one defect found and fixed

Run on the operator's dev checkout (single-user layout) via
`scripts/drill3_rehearse.py --dir <scratch>`; the harness itself was written by running the
procedure by hand first. Both halves as tabulated above, all nineteen steps behaving as the
authority line requires. Highlights worth keeping:

| check | result |
|---|---|
| factory half, candidate advanced / amended / base moved | `preview-stale` each time; the local remote never moved; the row returned to `pending` with its payload refreshed to reality |
| factory half, armed + swapped | refused before `graduate_and_prepare_envelope` was ever called — `prepare never called: True`, no outbox directory created at all |
| broker half, base moved | `base moved: envelope pinned 6a4303466, live main is a199c2c87`; the intruding commit intact (nothing force-pushed over it) |
| broker half, branch/repo swapped | `no allowlist entry for …/'release'` and `…'ikangai/clive'/'main'`; the envelope's own `remote_url`/`bare_path` were ignored; no `refs/heads/release` was ever created |
| broker half, unreviewed-but-perfect candidate | `pending` — unspent, unarchived, remote untouched. This is the one destination+liveness checks alone would have published |
| broker half, force-push shape | `95f2bcaf3 is not a fast-forward of a199c2c87` even though the tip WAS pinned |
| broker half, replay | `nonce already spent` twice — the second time with the spool receipt deleted |
| positive controls | both halves published exactly the approved tip; the interactive prompt showed the diff derived from the bare repo and labeled the envelope's `n_commits: 99` claim as unverified |

**Defect found (fixed, `cf8663f`).** An interactive `run-once` with no tty — a script, a
cron entry, ssh with stdin closed — read the `EOFError` from `input()` as the operator
answering no. That is terminal: nonce spent forever, envelope archived, and once the
receipt is ingested the factory-side approval resolves `rejected` with the note "operator
declined" — an audit statement no operator ever made, in the one subsystem whose entire
purpose is binding publications to decisions humans actually made. Recoverable (re-approving
prepares a fresh envelope), but the audit trail lied. `watch()` already refuses to run
without `--unattended` for exactly this reason; the same reasoning now applies one level
down. EOF raises `NoOperatorAvailable` and degrades to the soft `pending` outcome; an
explicit `n` still declines terminally. Found because the drill harness's own first run
piped a single `y` into a queue of two envelopes and the second one was declined by EOF.

Not fixed, recorded instead: the queue-order hazard above. It is inherent to a per-envelope
prompt loop, and the prompt does identify its envelope; the fix is the operator reading it.

## Teardown

```bash
launchctl bootout gui/$(id -u)/com.factory.broker
rm -f ~/Library/LaunchAgents/com.factory.broker.plist
# leave the spool + bare repo + operator authority dir in place unless you're fully
# decommissioning — the outbox/receipts/pins/spent history is useful forensics. To
# remove everything:
rm -rf /Users/Shared/factory-broker
rm -f ~/.factory-broker.yaml
rm -rf ~/.factory-broker
```

Disarming without a full teardown: set `autonomy.publication_broker: false` — the factory
returns to pushing directly (re-provision `GH_TOKEN` on the factory user first, or nothing
will publish at all). Existing pins/spent history is harmless to leave in place — it's
simply unused while disarmed.

## Troubleshooting

- **`factory broker run-once` reports nothing but you approved a graduation.** Check
  `outbox/` directly — if it's empty, the factory-side prepare step (`graduate_and_push`
  via `execute_approval`) may have failed before writing the envelope; check the
  `operator_actions` audit table / the dashboard Queue tab note.
- **Every envelope rejects with "no allowlist entry".** `repo_slug`/`base_branch` in
  `~/.factory-broker.yaml` must match EXACTLY what `config.target_repo_slug()` /
  `target.base_branch` resolve to on the factory side — a trailing slash or a stale
  branch name is the usual cause.
- **A publication sits `pending` forever under the LaunchAgent.** This is EXPECTED, not a
  bug — `--unattended` never pins. Review it (`factory broker status`, then compare
  against what you approved on the Queue tab) and either pin it
  (`factory broker pin <sha>`) or run `factory broker run-once` by hand to review+confirm
  interactively.
- **`factory broker pin` fails with "not a sha".** The value must look like a git sha
  (7-40 hex chars) — copy it from `factory broker status`'s pending-envelope listing or
  the Queue tab, not from memory.
- **A test push from the factory account still works after "removing" `GH_TOKEN`.**
  `gh auth logout` only clears `gh`'s own credential helper; if the factory checkout also
  has an SSH key with push access, or a cached credential in `~/.git-credentials` /
  macOS Keychain, those survive independently — check ALL of them, not just `GH_TOKEN`.
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
