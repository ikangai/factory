# Production-hardening roadmap — from supervised R&D to unattended guest-house operation (2026-08-06)

**Provenance:** an external production review (2026-08-06) of the factory architecture, the
operator's guest-house question ("the factory contained as a dedicated user on a machine"),
the reviewer's revised guest-house verdict, and ada's code-grounded assessment of all three.
The review's verdict stands: strong governance for supervised R&D, not yet unattended
production. This roadmap is the path to the reviewer's sign-off condition:

> Assume the guest house will eventually be trashed. Design things so it cannot enter the
> main house, forge the operator's approval, or publish anything other than the exact
> artifact that was approved.

Target end-state: **controlled unattended operation against noncritical repositories**, on a
dedicated user (ideally a dedicated/sacrificial machine), passing all six adversarial drills
(Part 5).

---

## Part 1 — Findings scorecard (review claims vs the code as of e15c64c)

Graded before planning, so phases attack real gaps rather than re-building what exists.

| Review finding | Status today | Disposition |
|---|---|---|
| P0 external actions not transactional with SQLite | OPEN. Merge lock + resume notes + idempotent-ish git ops are mitigation, not design | **Phase 2** |
| P0 develop-rail isolation (hostile tests) | OPEN inside the user boundary: workers share the factory user, can open blackboard.db; no per-worker sandbox | **Phase 0** (perimeter) + **Phase 3** (interior) |
| P0 approvals not bound to immutable artifacts | PARTIAL: graduation push re-derives + compares base/tip sha under the push lock (reporting/approvals.py:137-186); harness apply re-validates target+value. Missing: expiry/nonce, promotion + issue-closure binding, credential separation | **Phase 1** |
| P1 "one blackboard" contradicted by git/YAML/agora/dashboard | MOSTLY DOCS: bus is notification-only, dashboard writes whitelisted, store is workflow-canonical de facto. Missing: declared canonicality matrix + startup reconciliation | **Phase 2** (reconciler) + runbook |
| P1 repeated-search overfitting of held-out | PARTIAL: divergence alarm, held-out model, leakage retirement exist; no min-N/bounds/flake handling | **Phase 4** |
| P1 learnings/routing feedback poisoning | PARTIAL: outcome counters, is_counterproductive suppression, corrective provenance, clean_line hygiene (built after a real 2026-07 self-poisoning). Missing: quarantine-until-supported, difficulty-confound correction | **Phase 4** |
| "require a test" is gameable | OPEN: require_test proves a test file exists, not that it discriminates | **Phase 1** (red-proof) |
| Dead-worker claims | OPEN: no lease/expiry on task claims | **Phase 1** |
| SQLite hardening | LARGELY CLOSED: WAL, busy-timeout, FKs, migrations, backup daemon (deploy/user-factory/com.factory.backup.plist). Missing: restore drill | **Phase 2** |
| Ambiguous git state must escalate | CLOSED: exit-128 masking fixed at both diff sites; recurrence = guard regression by policy | — |

## Part 2 — Binding principles

1. **Perimeter ≠ authority.** The dedicated user bounds *blast radius*; it does not enforce
   the internal authority model. Both are required: Phase 0 completes the perimeter, Phases
   1+3 move authority (credentials, DB access) out of reach of what runs inside it.
2. **The strongest approval is one the guest house cannot use.** Publication credentials end
   up outside the factory user; the factory *prepares* envelopes, an operator-side broker
   *executes* them. Even total guest-house compromise cannot silently publish.
3. **Right-sized ops.** Intent rows + a startup reconciler + idempotency keys — not a
   distributed operation machine with heartbeats and compensation DSLs. At-least-once with
   idempotent effects, verified by kill-drills; never a claim of exactly-once.
4. **Honest platform labels.** The Windows path ships EXPERIMENTAL until the drills run on
   real Windows hardware; the label is removed by evidence, not by time.
5. **Deterministic doctors over prose checklists.** Every setup rule that can be audited by
   code gets a checker (`guesthouse_check`); the runbook explains, the checker verifies.
6. **No phase adds a new autonomous authority.** Everything here narrows or verifies; the
   authority table in the architecture schematic stays true throughout.

## Part 3 — Phased plan

### Phase 0 — Guest-house guided install, Mac + Windows  *(this branch)*

The operator's explicit want: a single line that guides a user through the guest-house
setup. Builds ON the existing pieces — `install.sh` (10-phase instance installer) and
`deploy/user-factory/01..03` (standard user, bare-repo code transfer, PAT hygiene,
launchd daemons, headless-auth go/no-go) — as orchestration, not reinvention.

Deliverables:
- **`install.sh --guest-house`** (macOS): an interactive wizard mode. Preflights (Darwin,
  admin rights available via sudo, git/CLT, disk); then guides, step by step with plain
  explanations and confirmations: 01-create-user (standard, no admin) → fast-user-switch
  instructions for `claude login` → 02-bootstrap (PAT minting guidance, 600-perm env file)
  → 03-daemons (board/fleet/backup) → brakes verification (STOP present, mode shift) →
  final doctor run + printed next-steps (supervised smoke shift per runbook §4 before any
  always-on). Idempotent and resumable like the kit scripts it wraps. One line:
  `curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | bash -s -- --guest-house`
- **`install.ps1`** (Windows, EXPERIMENTAL): the same guidance for the WSL2 route — check
  Windows/WSL2, create or import a dedicated distro (`factory-guesthouse`), write
  `/etc/wsl.conf` hardening (`[automount] enabled=false`, `[interop] enabled=false`,
  `appendWindowsPath=false` — no Windows drives, no Windows exec, no host PATH), create the
  in-distro dedicated user, then run `install.sh --guest-house --wsl` inside as that user.
  One line: `irm https://raw.githubusercontent.com/ikangai/factory/main/install.ps1 | iex`
- **`scripts/guesthouse_check.py`** — the deterministic doctor, runnable any time: audits
  the guest-house rules as PASS/FAIL/SKIP rows — standard (non-admin) user; no sudo grant;
  home dir 700; no SSH keys/agent, no operator Keychain reach, no Docker socket; factory
  runtime read-only to the factory user (bare-repo ownership split); credentials file
  600 and factory-owned; brakes engaged (STOP file, mode=shift) for a fresh install;
  dashboard bound to localhost; WSL: automount/interop off. Pure probe functions + a
  renderer, unit-tested with monkeypatched probes.
- **`docs/runbooks/guest-house.md`** — both platforms; the rules table from the review
  response (what the boundary does and does NOT give); teardown; residual-risk section
  copied forward honestly (same-user interior stays soft until Phase 3).
- README Install section: the two one-liners + when to choose guest-house mode.

Explicitly OUT of Phase 0: publication-credential separation (Phase 1), worker sandboxing
(Phase 3). The runbook says so — the guest house at Phase 0 bounds the blast radius; it
does not yet split interior authority.

### Phase 1 — Approval integrity + the publication broker

**SHIPPED** (feat/publication-broker, per `docs/plans/2026-08-06-publication-broker-
design.md`): envelope + spool + broker core, factory-side broker mode + issue-preview
consent-compare, red-proof tests, claim leases, and the deploy kit + runbook —
`docs/runbooks/publication-broker.md`. **Hardened 2026-08-07** after a probe-executed
security review found the first cut only verified an envelope's internal consistency +
destination liveness, never that a human had actually approved the *content* — see the
design doc's amended authority line: an operator-owned pin store now authorizes content
(default `require_pin: true`), an operator-owned append-only ledger is the real
replay-guard authority (not the factory-writable receipt copy), `watch`/the LaunchAgent
require an explicit `--unattended` opt-in and never bypass the pin gate, and `gh` issue
actions are field-validated before ever reaching argv.

- **Operator-side broker.** The factory user loses its push credential. Graduation/publish
  prepares an **approval envelope** — action, repository, base sha, tip/candidate sha,
  evidence-bundle hash, policy (config) hash, expiry, nonce — into the queue; a small
  broker running as the OPERATOR (launchd agent in the operator session, reusing the
  Queue) re-verifies every field against live git state immediately before executing the
  push/issue-closure with operator-held credentials. Mismatch or expiry ⇒ envelope
  invalidated, never retried silently. Extends the existing sha-binding
  (reporting/approvals.py) to promotion and issue closure; issue-closing keywords get a
  preview in the envelope (generated commit text must not close issues sight-unseen).
  Destination liveness alone is NOT content authenticity — the operator's own pin store
  (`~/.factory-broker/pins`) is the gate that actually binds a publication to a reviewed
  decision; see the design doc's amended authority line.
- **Red-proof tests.** In the code round, the shipped test must FAIL against the pristine
  base and PASS against the candidate (where the harness can run it both ways); a test
  that passes on base is flagged `stage=no_test` evidence, not merged silently.
- **Claim leases.** `claimed_at` + lease TTL on tasks; an expired claim is reclaimable;
  shift close-out reports leases it released. Kills the dead-worker-holds-task-forever
  state.

### Phase 2 — Crash consistency + canonical state

- **Intent rows.** A new `operations` table: idempotency key, operation kind
  (merge / graduate-push / issue-sync), input hashes + base sha,
  `planned → executing → applied → reconciled`, external receipt (result sha). The three
  external effects are wrapped: record `planned` before acting, `applied` with receipt
  after, and nothing retries while a row sits `executing`.
- **Startup reconciler.** On `factory run`/loop start: compare `executing`/`planned` rows
  against actual git/GitHub state (fetch first — the stale-origin lesson is already a
  memory) and either mark `reconciled` or surface a human escalation. At-least-once +
  idempotent, drill-verified (Part 5, drill 1).
- **Canonicality matrix** in the runbook + asserted where cheap: SQLite = workflow +
  decisions; git/content-addressed evidence = immutable artifacts; GitHub = published
  state; agora = notifications only; dashboard = authenticated command submission only.
- **Restore drill.** The backup daemon exists; add the documented, rehearsed restore path
  (runbook §: restore from snapshot, run reconciler, verify counts) — and run it once for
  real (drill 4).

### Phase 3 — Worker job-bundle isolation  *(own design doc when reached)*

The interior authority split — the biggest refactor, deliberately last of the structural
phases: workers stop sharing the factory user's ambient authority. A worker receives a
**job bundle** (target snapshot, brief, memory card, bounded toolset) in a disposable
sandbox and returns a **result bundle** (patch, test results, structured evidence) through
an inbox; it never holds the blackboard, the factory source tree, control-plane config, or
any credential. The orchestrator ingests results and remains the only store-writer.
Platform mechanism per boundary strength: separate POSIX user per worker, or containers
where Docker is acceptable (never the host Docker socket inside the guest house).

### Phase 4 — Evaluation validity + memory hygiene

- Flake detection + quarantine (a scenario flipping without a code change is quarantined,
  not scored); minimum sample sizes and minimum practical effect sizes before a
  merge/promotion delta counts; confidence bounds instead of raw score differences in
  `scoring.py` / the smoke-grade delta.
- **Quarantine-before-auto-revert:** a single noisy post-merge regression re-runs for
  confirmation before reverting.
- Second held-out model (config-optional) for model-specific overfitting; a query-budget
  note on repeated candidate search against the same held-out set.
- **Information-flow rule, asserted in tests:** held-out outcomes never enter learnings,
  routing evidence, or proposer context.
- Learnings quarantine-until-supported (a new lesson rides in shadow until independently
  supported); routing/fit statistics gain a task-class difficulty adjustment so
  blocked-rates stop being read as causal.

## Part 4 — Parked or explicitly rejected

- **Distributed operation state machine** (heartbeats, compensation DSL): rejected for a
  single-host system — Phase 2's intent rows + reconciler buy the safety without the
  machinery. Revisit only if the factory ever spans hosts.
- **Per-run random seeds:** rejected as stated — LLM panel runs are not seedable in any
  meaningful sense; repeats + confidence bounds (Phase 4) are the honest equivalent.
- **Hash-chained tamper-evident audit logs:** parked. Append-only tables + external
  backups cover the single-operator threat model; chaining adds ceremony without a
  distinct adversary here (an interior adversary is Phase 3's job).
- **Two-person approval, signed releases:** parked until there are two persons / external
  installers beyond the operator's own instances.

## Part 5 — Acceptance gate: the six adversarial drills

Unattended-production sign-off = a guest-house install (Phase 0) passing all six, each
drill run after its enabling phase:

| # | Drill | Enabled by |
|---|---|---|
| 1 | Kill -9 immediately before/after merge, DB commit, approval, push, promotion, revert — no duplicated or lost action | Phase 2 |
| 2 | Malicious repo attempts network, Keychain, process escape, symlinks, dependency substitution, host writes | Phase 0 (perimeter) → Phase 3 (interior) |
| 3 | Change branch/candidate after approval — execution rejected | Phase 1 |
| 4 | Corrupt + restore the DB; reconcile against git and published state | Phase 2 |
| 5 | Many adaptively selected candidates — thresholds and hidden-set protections hold | Phase 4 |
| 6 | Hostile instructions injected into task evidence / repo output — never become privileged memory or alter governance | Phase 4 (+ existing hygiene) |

Windows: the same drills on a real Windows/WSL2 box before `install.ps1` drops its
EXPERIMENTAL label.

## Rollout & verification protocol

Each phase: design detail in this doc (or its own doc for Phase 3) → feature branch →
adversarial review (probe-executed, per this repo's convention) → merge with full suite
green → the drill(s) it enables, executed and recorded in `docs/runbooks/` before the next
phase starts. The factory's own self-harness loop stays OFF for drills 1–4 (a clean
substrate under test), and is part of the system under test for drills 5–6.
