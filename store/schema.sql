-- clive-harness-factory blackboard schema (spec §8)
-- Single source of truth. Inspectable, version-controllable (sqlite3 db .dump),
-- and the data source for the board. Roles never message each other; they read
-- and write this store, and the orchestrator sequences them.
--
-- Conventions: all timestamps are ISO-8601 UTC strings. JSON columns hold
-- machine-readable blobs the board and roles deserialize.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- The reigning harness spec.
CREATE TABLE IF NOT EXISTS champion (
    id          TEXT PRIMARY KEY,         -- spec content hash or "champion-<n>"
    spec_path   TEXT NOT NULL,            -- factory/specs/champion.yaml (or archived copy)
    promoted_at TEXT NOT NULL,
    scores_json TEXT NOT NULL DEFAULT '{}' -- aggregate scores at time of promotion
);

-- Proposed harness specs (one bounded change to the open block each).
CREATE TABLE IF NOT EXISTS candidates (
    id             TEXT PRIMARY KEY,
    parent         TEXT NOT NULL,         -- candidate id or "champion"
    spec_path      TEXT NOT NULL,         -- factory/specs/candidates/<id>.yaml
    stage          TEXT NOT NULL          -- proposed|evaluating|scored|awaiting_gate|promoted|rejected
                     CHECK (stage IN ('proposed','evaluating','scored','awaiting_gate','promoted','rejected')),
    created_at     TEXT NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',  -- one-line human description of the bounded change
    diff_json      TEXT NOT NULL DEFAULT '{}',-- structural diff of open vs parent (proposer-visible history)
    scores_json    TEXT NOT NULL DEFAULT '{}',-- computed scores after evaluation
    notes          TEXT NOT NULL DEFAULT ''
);

-- Scenario corpus mirror (the YAML files on disk are authoritative; this row
-- carries board + leakage state). Working set is proposer-visible; held-out is not.
CREATE TABLE IF NOT EXISTS scenarios (
    id            TEXT PRIMARY KEY,
    class         TEXT NOT NULL CHECK (class IN ('single','multi-clive')),
    partition     TEXT NOT NULL CHECK (partition IN ('working','held-out')),
    leakage_count INTEGER NOT NULL DEFAULT 0,  -- promotion decisions this held-out scenario influenced
    source        TEXT NOT NULL CHECK (source IN ('seed','mined')),
    spec_path     TEXT NOT NULL,               -- factory/scenarios/<part>/<id>.yaml
    goal          TEXT NOT NULL DEFAULT '',
    snapshot      TEXT NOT NULL DEFAULT '',     -- env image / provisioning spec
    check_path    TEXT NOT NULL DEFAULT '',     -- deterministic acceptance check
    active        INTEGER NOT NULL DEFAULT 1
);

-- One run = one (candidate, scenario, model) evaluation. The candidate clive's
-- own claim of success is recorded in clive_claim but NEVER scored (principle 2).
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    candidate_id  TEXT NOT NULL REFERENCES candidates(id),
    scenario_id   TEXT NOT NULL REFERENCES scenarios(id),
    model         TEXT NOT NULL,               -- panel model name
    outcome       TEXT NOT NULL                -- pass|fail|error|budget_exceeded|blocked
                    CHECK (outcome IN ('pass','fail','error','budget_exceeded','blocked')),
    evidence_path TEXT NOT NULL DEFAULT '',    -- logs/runs/<run_id>/
    budget_used   INTEGER NOT NULL DEFAULT 0,  -- tokens consumed by the candidate clive
    created_at    TEXT NOT NULL,
    partition     TEXT NOT NULL DEFAULT 'working', -- denormalized for divergence math
    clive_claim   TEXT NOT NULL DEFAULT '',    -- clive's own success report (recorded, not scored)
    check_json    TEXT NOT NULL DEFAULT '{}',  -- acceptance-check evidence
    duration_s    REAL NOT NULL DEFAULT 0
);

-- Judge's semantic annotations on the surface deterministic checks cannot reach.
-- The Judge does NOT set pass/fail; it annotates.
CREATE TABLE IF NOT EXISTS judge_notes (
    run_id     TEXT NOT NULL REFERENCES runs(id),
    flags_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id)
);

-- Human promotion decisions at the gate (the one write action of the board).
CREATE TABLE IF NOT EXISTS promotions (
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    decision     TEXT NOT NULL CHECK (decision IN ('promote','reject','defer')),
    operator     TEXT NOT NULL,
    rationale    TEXT NOT NULL DEFAULT '',
    decided_at   TEXT NOT NULL,
    PRIMARY KEY (candidate_id, decided_at)
);

-- Log of re-grounding proxies against production (arbitration loop, §9).
CREATE TABLE IF NOT EXISTS recalibrations (
    at                    TEXT NOT NULL,
    note                  TEXT NOT NULL DEFAULT '',
    production_window      TEXT NOT NULL DEFAULT '',
    proxy_correlation_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (at)
);

-- Token / compute spend, for the cost-burn meter and budget caps.
CREATE TABLE IF NOT EXISTS budget_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    role_or_run TEXT NOT NULL,               -- 'proposer' | 'run:<run_id>' | 'judge' | ...
    tokens      INTEGER NOT NULL DEFAULT 0,
    cost        REAL NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    shift_id    INTEGER,                      -- conductor-loop attribution (NULL for the old loop)
    seconds     REAL NOT NULL DEFAULT 0,      -- wall-clock the spend took (timesheets)
    profile     TEXT NOT NULL DEFAULT ''      -- worker profile that earned the spend (Phase 5)
);

-- Negative-safety check trips. Any high/critical severity blocks promotion.
CREATE TABLE IF NOT EXISTS safety_flags (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   TEXT NOT NULL REFERENCES runs(id),
    kind     TEXT NOT NULL,                  -- out_of_scope_path|grader_heldout_access|unrequested_port|destructive_op|budget_exceeded|...
    detail   TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL CHECK (severity IN ('info','low','medium','high','critical'))
);

CREATE INDEX IF NOT EXISTS idx_runs_candidate ON runs(candidate_id);
CREATE INDEX IF NOT EXISTS idx_runs_scenario  ON runs(scenario_id);
CREATE INDEX IF NOT EXISTS idx_safety_run     ON safety_flags(run_id);
CREATE INDEX IF NOT EXISTS idx_candidates_stage ON candidates(stage);

-- ===========================================================================
-- The conductor loop (design: docs/plans/2026-06-25-conductor-loop-design.md).
-- Five tables make bounded shifts RESUMABLE and the MISSION the terminator.
-- ===========================================================================

-- The human's single steer. Only one row is active (re-steering deactivates the
-- prior mission). The factory runs until this is reached — not until a queue empties.
CREATE TABLE IF NOT EXISTS mission (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    statement   TEXT NOT NULL,
    target_repo TEXT NOT NULL DEFAULT '',     -- optional: a repo with issues to work
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

-- The backlog. Work comes from the target's issues, the researchers, the workers
-- themselves (found-but-not-fixed), or the human/mission.
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL CHECK (source IN ('issue','research','worker','human','mission')),
    source_ref TEXT NOT NULL DEFAULT '',       -- github issue number, research brief id, …
    status     TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open','claimed','in_progress','done','dropped','blocked')),
    result     TEXT NOT NULL DEFAULT '',       -- merge sha / outcome / why-dropped
    spec_json  TEXT NOT NULL DEFAULT '{}',      -- GSD typed spec: target_surface/acceptance/out_of_scope
    est_tokens INTEGER NOT NULL DEFAULT 0,      -- conductor's effort estimate (EVM task-level PV)
    profile    TEXT NOT NULL DEFAULT '',        -- worker_profiles.name to dispatch with ('' = generalist)
    milestone_id INTEGER REFERENCES milestones(id),  -- the plan link (EVM derives PV/EV/AC from this)
    -- the shift that last worked it. NULL until a shift picks it up; FK is safe because
    -- shifts are never DELETEd (a killed shift is UPDATEd to 'error', so it still exists).
    shift_id   INTEGER REFERENCES shifts(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Each bounded conductor session. State persists here so the next shift resumes.
CREATE TABLE IF NOT EXISTS shifts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id   INTEGER REFERENCES mission(id),  -- missions are never deleted, so this is safe
    token_budget INTEGER NOT NULL DEFAULT 0,
    tokens_used  INTEGER NOT NULL DEFAULT 0,       -- kept current via set_shift_tokens (not only at end)
    status       TEXT NOT NULL DEFAULT 'running'   -- 'running' + no ended_at after startup = a CRASHED shift
                   CHECK (status IN ('running','completed','halted','timed_out','budget_exhausted','error')),
    report       TEXT NOT NULL DEFAULT '',      -- the daily report text / path
    resume_note  TEXT NOT NULL DEFAULT '',      -- what the next shift should pick up
    started_at   TEXT NOT NULL,
    ended_at     TEXT
);

-- What shipped each shift, handed to the researchers (the research<->dev loop).
CREATE TABLE IF NOT EXISTS digests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id     INTEGER REFERENCES shifts(id),
    shipped_json TEXT NOT NULL DEFAULT '[]',    -- task ids / shas merged this shift
    summary      TEXT NOT NULL DEFAULT '',      -- prose digest for the researchers
    consumed     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

-- The mission-progress timeline: never a silent binary "done".
CREATE TABLE IF NOT EXISTS mission_status (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id     INTEGER REFERENCES shifts(id),
    status       TEXT NOT NULL CHECK (status IN ('advancing','steady_state','blocked','reached')),
    rationale    TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',    -- backlog size, research-dry streak, score deltas
    at           TEXT NOT NULL
);

-- Factory memory: durable learnings each agent role / super-worker reads back to
-- improve. role in {conductor,developer,researcher,factory}; the factory itself
-- learns via the 'factory' role. (design: docs/plans/2026-06-27-factory-memory-design.md)
CREATE TABLE IF NOT EXISTS learnings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT NOT NULL,
    agent      TEXT NOT NULL DEFAULT '',     -- optional handle/identity
    scope      TEXT NOT NULL DEFAULT 'general',
    content    TEXT NOT NULL,
    shift_id   INTEGER,
    uses       INTEGER NOT NULL DEFAULT 0,   -- times surfaced into a prompt (relevance signal)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learnings_role ON learnings(role);

-- The plan: conductor-maintained milestones with deliverables. Tasks link via
-- tasks.milestone_id; EVM (reporting/evm.py) derives PV/EV/AC from these rows.
CREATE TABLE IF NOT EXISTS milestones (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id    INTEGER REFERENCES mission(id),
    title         TEXT NOT NULL,
    deliverable   TEXT NOT NULL DEFAULT '',   -- the artifact/state that proves it
    acceptance    TEXT NOT NULL DEFAULT '',   -- how the human/conductor verifies delivery
    status        TEXT NOT NULL DEFAULT 'planned'
                    CHECK (status IN ('planned','active','delivered','dropped')),
    planned_order INTEGER NOT NULL DEFAULT 0, -- sequence within the mission
    budget_tokens INTEGER NOT NULL DEFAULT 0, -- planned effort (the EVM value unit)
    created_by    TEXT NOT NULL DEFAULT 'conductor',
    created_at    TEXT NOT NULL,
    delivered_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_milestones_status ON milestones(status);
-- NOTE: idx_tasks_milestone is created in Blackboard._migrate, NOT here — on an existing DB
-- tasks.milestone_id is added by a migration ALTER that runs AFTER this script, so indexing it
-- here would fail with "no such column". _migrate creates it once the column is guaranteed.

-- Auto issue-sync ledger: which (target-repo issue, graduated commit) pairs the
-- factory has already commented/closed on, so a resume/re-run never double-posts.
-- (design: docs/plans/2026-06-27-factory-auto-issue-sync-design.md)
CREATE TABLE IF NOT EXISTS issue_sync (
    issue_number INTEGER NOT NULL,
    commit_sha   TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('comment','close')),
    url          TEXT NOT NULL DEFAULT '',     -- comment/issue URL gh returned
    created_at   TEXT NOT NULL,
    PRIMARY KEY (issue_number, commit_sha)
);

-- Worker capability profiles: the conductor's on-demand workforce. A profile is DATA
-- (persona overlay + model tier) the rail instantiates per task — it can NEVER change the
-- toolset, sandbox boundary, frozen surface or gates (those stay rail-fixed), so generating
-- one at runtime is safe. Retire-not-delete: the timesheet/ledger reference profile names.
CREATE TABLE IF NOT EXISTS worker_profiles (
    name        TEXT PRIMARY KEY,          -- slug: 'python-dev', 'ml-expert', 'prompt-pro'
    description TEXT NOT NULL,             -- capabilities, for the conductor + the board
    model       TEXT NOT NULL DEFAULT '',  -- tier alias: ''|'frontier'|'standard'|'fast'
    overlay     TEXT NOT NULL DEFAULT '',  -- persona/emphasis block injected at {PROFILE}
    active      INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL DEFAULT 'operator',
    created_at  TEXT NOT NULL
);

-- Whitelisted runtime overrides (Phase 6.1, pulled forward — Task 5.2's staffing guard lives
-- here). config.yaml stays the git-tracked defaults file; the board/CLI write bounded overrides
-- consumed where cmd_run resolves knobs (store override → config.yaml → hardcoded default).
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,   -- e.g. 'super_worker.max_parallel' | 'staffing.seeded_for'
    value      TEXT NOT NULL,      -- stringly; the consumer casts
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_shift    ON tasks(shift_id);   -- find a dead shift's orphaned work
CREATE INDEX IF NOT EXISTS idx_shifts_status  ON shifts(status);    -- find crashed 'running' shifts on resume
CREATE INDEX IF NOT EXISTS idx_mission_active ON mission(active);
-- The single-active-mission invariant as a SCHEMA guarantee (not just app code):
-- a double-activation raises IntegrityError instead of being silently masked.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mission_active ON mission(active) WHERE active = 1;
