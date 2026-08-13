# Crash-recovery runbook

Companion to `docs/plans/2026-08-08-crash-consistency-design.md` (Phase 2 of
`docs/plans/2026-08-06-production-hardening-roadmap.md`). Read the design doc's "honest
goal" section first — this runbook assumes it:

> SQLite cannot transact with git or GitHub. Exactly-once is unachievable. What is
> achievable: never silently LOSE an effect, never harmfully REPEAT an effect, never LIE
> about an effect — an unresolvable state resolves `unknown` and escalates, it never
> guesses in either direction.

## 1. The `operations` table — what each kind means

Every external effect the factory takes (a merge, a graduation push, a broker-armed
graduation prepare) gets a durable row in `operations` (`store/schema.sql`) BEFORE it
happens and after it resolves. `status` moves `planned`/`executing` (the effect is in
flight, or a crash interrupted it) → `applied`/`reconciled`/`failed` (a known outcome) →
`unknown` only when nothing — not git, not a receipt, not the ledger — could answer the
question honestly.

One subtlety, which is the whole of drill 1's finding (§4): **for a `merge`, `applied` is
not terminal.** It is written the instant the merge lands, while the round still has its
entire re-baseline — a full grade plus the target's suite — to run before keeping or
auto-reverting that merge. A round that FINISHES now resolves its own row to `reconciled`,
so a `merge` row left at `applied` whose task never closed out is by construction a crashed
one, and the reconciler sweeps exactly those (`reconcile._crashed_applied_merges`). For
every other kind `applied` IS terminal and is never swept.

| `kind` | What it tracks | `idem_key` shape | Landed = | Not landed = |
|---|---|---|---|---|
| `merge` | A super-worker's candidate merging into `factory/auto` (`orchestrator/code_round.py`) | `merge:<task_id>:<cand_tip_sha>` | a commit on the auto branch carries `Factory-Task: <task_id>` and nothing later carries a matching `Factory-Revert:` trailer | no such commit, or one exists but was auto-reverted |
| `graduate_push` | The unarmed graduation push (`reporting/issue_sync.py graduate_and_push`) | `grad:<repo>:<base_sha>:<tip_sha>` | `tip_sha` is an ancestor of `origin/<base>` (a fresh `git fetch` + `merge-base --is-ancestor`) | it isn't |
| `graduate_prepare` | The broker-armed prepare step (`graduate_and_prepare_envelope`) — writes an envelope for the operator's broker, never pushes origin itself | `gradprep:<nonce>` | the broker's own receipt says `pushed`, or (no receipt yet) git shows the tip landed anyway | a `rejected`/`expired` receipt, or nothing found and the paired approval is no longer in flight |
| `issue_sync` | A GitHub issue comment/close action | `issue:<number>:<sha>` | the pair is present in the `issue_sync` ledger table | absent — **the reconciler never asks GitHub**; absent always resolves `unknown`, never "assumed not posted" |

A row's `receipt` column carries the resulting sha/nonce once known; `detail` carries the
operator-facing why (or the exact command to check by hand, for an `unknown`). `attempts`
and `updated_at` are bookkeeping, not authority — git/receipts/the ledger are.

The `merge` row's "landed" rule above is how an `executing` row is read. A crashed
`applied` row is read from its **receipt** instead — the sha git itself returned from the
merge — because the `Factory-Task:` trailer is written only when the task carries a ref,
and reading its absence as "never landed" would flip a genuinely applied row to `failed`.

## 2. Reading `factory reconcile --dry-run`

```
$ factory reconcile --dry-run
  #14 [merge] merge:task-a1b2c3d4:9f8e7d6 — executing
  #15 [graduate_push] grad:ikangai/clive:abc123:def456 — planned
[reconcile] 2 row(s) would be examined (dry-run — nothing changed)
```

This lists every row still `planned`/`executing`, oldest first, bounded to the first 50 —
exactly what a real sweep (`factory reconcile`, or the automatic one at shift start)
would attempt to resolve. `--dry-run` resolves nothing; it's the pre-flight check before
a `factory reconcile` for real, or a way to see whether a suspicious shift left anything
behind.

A real sweep prints what it did:

```
$ factory reconcile
  #14 [merge] -> reconciled: landed: 9f8e7d6a...
  #15 [graduate_push] -> unknown: no fetch credential / not-on-base; verify manually: ...
[reconcile] examined 2, resolved 1, escalated 1 unknown — see `factory learn list --role factory` / the backlog for the escalation(s)
```

Every `unknown` also lands a deduped backlog task (`source_ref = reconcile:<kind>-unknown`)
and a factory learning — `factory task list --status open` and `factory learn list --role
factory` both surface it. The task's `detail` carries the SAME exact command printed
above; that command is always answerable with read-only git/`gh` — run it, then resolve
the row by hand if needed (`sqlite3 store/blackboard.db "UPDATE operations SET
status='reconciled', detail='manually verified: <what you found>' WHERE id=<id>"` — a
direct SQL edit is the sanctioned manual override; there is no CLI verb for it because a
human override should leave an unmistakable trace in the row itself).

The reconciler runs automatically at the START of every shift (`orchestrator/shift.py`,
right after `reap_orphaned_shifts`, BEFORE broker receipt ingestion and the approval
reaper — it has to see an `executing` row before the reaper turns it into the lossier
`'stale'`). It has its own STOP check independent of the shift's later one, so it never
runs while the fleet is halted (`factory reconcile` the CLI honors the same brake) —
**except** during `factory db-restore`, which explicitly bypasses it (§4) because
restore's own precondition requires STOP engaged.

## 3. Restore procedure, step by step

Prerequisite: a backup exists. `scripts/backup_blackboard.sh` (scheduled via
`deploy/user-factory/com.factory.backup.plist`) takes one with `sqlite3 .backup` +
`PRAGMA integrity_check` every 6h by default, into `~/factory-db-backups/` (override with
`FACTORY_BACKUP_DIR`).

1. **Stop the fleet.** `touch STOP` (or the dashboard's brake). Confirm no runner is
   alive — `factory autopilot status`, or `ps aux | grep 'run --loop'`.
2. **Pick a snapshot.** `ls -t ~/factory-db-backups/blackboard-*.db | head -5` — the
   newest one is usually right; older ones matter if the newest itself was taken
   post-corruption.
3. **Restore.**
   ```
   # Drill / rehearse anywhere — an explicit target is always safe:
   factory db-restore ~/factory-db-backups/blackboard-<STAMP>.db --db /tmp/drill.db --yes

   # The real store. --yes does NOT authorize this; you type the target path when asked
   # (or pass --i-mean-the-real-store to answer it up front):
   factory db-restore ~/factory-db-backups/blackboard-<STAMP>.db
   ```
   **STOP being engaged is not consent.** STOP means "the factory is braked" and is often
   ambient for unrelated reasons — an agent developing this tool once had a restore hit the
   real store on exactly that technicality. Overwriting the default target therefore needs a
   second, deliberate act that `--yes` cannot supply.

   The command, in order:
   - refuses unless STOP is engaged AND no runner is alive AND nothing holds a write lock on
     the target (it tries `BEGIN IMMEDIATE` — a PID file only knows about `run --loop`, not
     the dashboard, a second CLI, a worker, or an open `sqlite3` shell);
   - `PRAGMA integrity_check`s the **snapshot**, then checks it is actually a blackboard
     (the signature tables must be present). Integrity alone is not enough: SQLite reports a
     0-byte file as a valid, healthy, EMPTY database, so a truncated download or an unrelated
     `chat.db` would otherwise pass every gate and wipe the store while reporting success;
   - **prints a preflight sheet — always, even with `--yes`**: target, snapshot path/size/date,
     and row counts for both, so a wipe is visible *before* it happens rather than inferred
     afterwards;
   - checkpoints the live db (`wal_checkpoint(TRUNCATE)`), then moves it aside as
     `blackboard.db.bak-<UTC-STAMP>` (+ `…-wal`/`…-shm` if any) — **never deleted**, and never
     clobbering an earlier backup taken in the same second. The sidecar suffix goes *after*
     the stamp deliberately: SQLite looks for `<name>-wal` beside `<name>`, so the old
     `<db>-wal.bak-<STAMP>` naming orphaned the WAL and opening the backup after a real crash
     silently returned a checkpoint-old database that reported itself healthy;
   - copies the snapshot in, `PRAGMA integrity_check`s the **result**, and on failure moves
     the bad copy aside as `.failed-<STAMP>` and puts the previous db **and its sidecars**
     back;
   - re-runs `init_db()` (additive migrations — a snapshot predating a schema change, e.g.
     before the `operations` table existed, gains it here);
   - runs the reconciler (`ignore_stop=True`, see §2) so anything the corruption window left
     ambiguous is resolved or escalated now, not silently at the next shift;
   - prints a counts summary. If anything fails *after* the copy, it prints the exact command
     to put your previous database back, before surfacing the error.
4. **Cross-check against git.** The counts summary is a sanity sheet, not proof:
   ```
   git -C <target>.factory-auto log --oneline | wc -l      # commits on the auto branch
   factory task list --status open                          # anything the reconciler reopened
   factory learn list --role factory                        # any 'unknown' escalations
   ```
   A snapshot older than the crash legitimately shows FEWER tasks/shifts than git's history —
   the reconciler cannot invent rows the snapshot never had. **But check it against the
   preflight sheet you were shown**: if the snapshot's counts were wildly below the live db's,
   you restored the wrong file, and the previous db is still sitting next to it as
   `.bak-<STAMP>`.
5. **Resume.** `rm STOP` (or the dashboard) once satisfied. The `.bak-*`/`.failed-*` siblings
   in `store/` are gitignored (verified: the main file and both sidecars) and yours to keep or
   clear — `scripts/backup_blackboard.sh` doesn't touch them.

## 4. Drill 1 — kill at each wrapped boundary

The acceptance test for Component B (`docs/plans/2026-08-08-crash-consistency-design.md`
§Acceptance / `docs/plans/2026-08-06-production-hardening-roadmap.md` Part 5, drill 1).
Run it against a **throwaway** clone/dispatch, never a real graduation:

1. Pick a boundary: before/after `adapter.merge_branch` (`orchestrator/code_round.py`),
   before/after the auto-revert (`adapter.revert_commit`), before/after the unarmed push
   (`reporting/issue_sync.py graduate_and_push`), before/after `write_envelope` (armed
   prepare), before/after `resolve_approval` in `ingest_broker_receipts` (armed
   ingestion).
2. `kill -9` the factory process at that exact point (a debugger breakpoint, or a
   monkeypatched hook that raises `os.kill(os.getpid(), signal.SIGKILL)` for a scripted
   drill — `tests/test_crash_consistency_wrap.py`'s raising-store tests are the unit-level
   equivalent of "the write after this point never happens").
3. Restart the factory (`factory reconcile --dry-run` first to see what's stuck, then
   `factory reconcile` for real, or just start a shift — it runs automatically).
4. Assert, per the design's three properties:
   - **No lost effect**: if the boundary was AFTER the external action (git already has
     the merge/push; the envelope is already on disk), the reconciler finds it via
     git/receipt and marks it `reconciled`/`applied` — nothing about it disappears.
   - **No duplicated effect**: re-dispatching the SAME task produces a NEW candidate
     (a fresh branch/tip), so `begin_operation`'s idem-key skip only fires on a literal
     retry of the identical attempt (e.g. the reconciler re-entering mid-resolution) —
     verify the git log has exactly the merges you expect, not one extra.
   - **Surfaced, never guessed**: a boundary the reconciler genuinely cannot resolve
     (no reachable worktree, no fetch credential, no receipt and no git evidence) must
     show up as `unknown` with a backlog task — never silently marked done or discarded.

### Executed 2026-08-13 — the merge boundaries, 37/37

**Harness.** A throwaway git repo (a `factory/auto` branch plus a candidate branch), a
throwaway blackboard, and the REAL `run_code_round` driven with a `TargetAdapter` subclass:
every git operation under test — `merge_branch`, `current_commit`, `revert_commit` — is the
shipped base-class implementation, and only `frozen_paths`/`test_command` are stubbed, so
the drill needs no `config.yaml` and no target repo. The crash is a real
`os.kill(os.getpid(), SIGKILL)` inside `adapter.merge_branch` / `grade_fn`; every child
process exited `-9`. Two deliberate monkeypatches, both outside what is being tested:
`killswitch.is_halted` (this checkout keeps STOP engaged on purpose, and the brake is not
the subject) and the kill hook itself. Nothing touched `store/blackboard.db`, the target
repo, or any remote.

| kill point | git afterwards | row after the crash | after `factory reconcile` |
|---|---|---|---|
| before `merge_branch` | no merge | `executing` | `failed` "not landed"; task → `open`; retry NOT suppressed |
| after `merge_branch`, before the receipt | merge present | `executing`, no receipt | `reconciled` "landed: `<sha>`"; task repaired to `done`; identical retry skipped, a NEW candidate not skipped |
| after the merge, worktree unreachable | merge present | `executing` | `unknown` + escalation carrying the exact `git log --grep` command |
| mid-re-baseline | merge standing | `applied` | `unknown`, escalated as UNVERIFIED with a per-merge dedup ref; task never claimed `done` |
| before the auto-revert of a REGRESSING merge | merge standing | `applied` | same — git shows the same thing whether the lost re-baseline would have kept or reverted it, so it escalates rather than guessing |
| after the auto-revert | merge + revert | `applied` | `reconciled` "landed then auto-reverted"; task → `open`; no escalation |
| no crash (control) | merge present | `reconciled` | nothing to examine |

**What it found.** The first three rows behaved as designed — the runbook's three
properties held wherever the row was still `executing`. The last three did not exist as
behavior at all: `applied` was never swept, so a crash anywhere after the receipt left the
merge's *fate* unresolved. The task was never repaired (the factory re-dispatches work it
has already landed — the exact consequence the design's seam map attributed to this window)
and a regressing merge could stay standing with nothing flagging it. Fixed in the same pass
(`fix/crash-consistency-applied-merge`): the table above is the POST-fix behavior, and the
last three rows are what the fix added. Regression tests live in
`tests/test_reconcile.py` ("kind: merge, status 'applied'") and
`tests/test_crash_consistency_wrap.py`.

**Still unexercised.** Three of the five boundary pairs: the unarmed push, the armed
envelope prepare, and the armed receipt ingestion. Each needs graduation/broker fixtures
rather than the merge fixture above, so none of them is covered by this run.

## 5. Drill 4 — corrupt, restore, reconcile, verify

Runs entirely on a throwaway database. Nothing here touches `store/blackboard.db`; that
is what `--db` exists for, and it is why the drill is safe to rehearse whenever you like.

```bash
# 1. a disposable copy of a real snapshot, and a "live" db to be clobbered
cp ~/factory-db-backups/blackboard-<STAMP>.db /tmp/drill-snap.db
cp store/blackboard.db /tmp/drill-live.db          # the factory may be running; this is a copy

# 2. corrupt the drill's live db, and prove the corruption is real.
#    Overwrite the HEADER: scribbling on a random interior page usually still reports
#    "ok", because integrity_check only reads pages it can reach from the schema.
dd if=/dev/urandom of=/tmp/drill-live.db bs=100 count=1 conv=notrunc
sqlite3 /tmp/drill-live.db "PRAGMA integrity_check;"     # "file is not a database"

# 3. restore into it (STOP must be engaged; --db keeps the real store out of reach)
touch STOP
factory db-restore /tmp/drill-snap.db --db /tmp/drill-live.db --yes
```

**What to verify, all of it visible in what the command prints:**

- the **preflight sheet** appeared *before* anything was written, and its snapshot row
  counts are what you expected — this is the check that catches restoring the wrong file;
- `moved aside -> /tmp/drill-live.db.bak-<STAMP>` and the `to undo:` line are printed;
  the corrupted original is still on disk, exactly as promised;
- the reconciler ran rather than skipping for STOP (`reconciler: examined N, resolved N,
  escalated N unknown`);
- the counts summary matches the snapshot's preflight numbers.

Then prove the safety net for real — open the moved-aside backup and confirm it still has
its own content (this is the property that silently failed before the sidecar-naming fix):

```bash
sqlite3 /tmp/drill-live.db.bak-<STAMP> "PRAGMA integrity_check; SELECT COUNT(*) FROM tasks;"
```

**Refusal drills** (each should refuse and change nothing — the interesting half):

```bash
: > /tmp/empty.db
factory db-restore /tmp/empty.db --db /tmp/drill-live.db --yes    # snapshot-not-a-blackboard
rm STOP && factory db-restore /tmp/drill-snap.db --db /tmp/drill-live.db --yes   # stop-not-engaged
```

Clean up with `rm -f /tmp/drill-*`; re-engage or clear STOP as you intend.

## 6. Canonicality matrix (Component E)

Which store is the authority for which kind of fact — asserted where cheap
(`tests/test_canonicality.py`), not enforced by a runtime gate everywhere (some of these
are structural facts about the codebase, not something a check can veto at request time):

| Store | Canonical for | How it's enforced/checked |
|---|---|---|
| SQLite blackboard (`store/blackboard.db`) | Workflow state + decisions (task status, shifts, approvals, learnings) | The only store any role/rail writes through — `common/store.py`'s CRUD |
| git (content-addressed) | Artifacts — what actually landed | The reconciler treats git as truth OVER the DB for `merge`/`graduate_push`/`graduate_prepare` (§1); `Factory-Task:`/`Factory-Revert:` trailers make history self-describing even with zero DB |
| GitHub | Published issue state | Broker receipts + `ls-remote`/`merge-base --is-ancestor` — the reconciler NEVER probes GitHub directly for `issue_sync` (§1); a wrong close is unrecoverable, so an unanswerable row stays `unknown` rather than guess |
| agora bus (`chat.db`) | Notifications only, never workflow state | `common/bus.py` never imports `common.store`/opens the blackboard — tested statically (`tests/test_canonicality.py`) |
| dashboard | Authenticated command submission only | `dashboard/fleet_server.py`'s `do_POST` whitelist (a fixed tuple of paths, checked before any body is even read) — tested statically |

Three read-only `reporting/` modules explicitly claim "never writes WORKFLOW state" in their
own docstrings (`summary.py`, `diary.py`, `blog.py`) — asserted by AST-scanning them for
any call to a `Blackboard` write method (`tests/test_canonicality.py`). This is
deliberately NOT a blanket "nothing under reporting/ writes" claim: `reporting/
approvals.py`, `issue_sync.py`, `factory_memory.py`, `scope_check.py`, and
`human_queue.py` are documented, legitimate writers (workflow modules that happen to live
in that package) — see each module's own docstring for its actual contract.

## Out of scope (restated, so nobody re-litigates it here)

Leases/heartbeats on `operations` rows, a compensation DSL, distributed anything,
hash-chained audit logs, and any claim of exactly-once. The `develop.py` `merge_lock` is
a `threading.Lock` (in-process only) — two concurrent `factory run` *processes* could
still race the shared `factory/auto` worktree; this is a pre-existing, documented,
unfixed gap (relevant to drill 1 if the drill runs two loops at once).
