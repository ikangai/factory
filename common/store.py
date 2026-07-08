"""The blackboard: a thin, explicit SQLite access layer (spec §8).

Roles never message each other; they read and write this store, and the
orchestrator sequences them. This module is CRUD only — scoring/divergence math
lives in scoring.py so the grader logic is inspectable in one place.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import paths

# A profile slug: lowercase, starts alnum, 2–32 chars. Kept tight so a conductor-generated
# name is always a safe table key / CLI token / filename fragment.
_PROFILE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")

# The generalist is the fail-open default: no persona overlay, the account's default (frontier)
# model — i.e. today's exact dispatch behavior. get_profile('') / get_profile('generalist')
# always resolve to this even before Task 5.2 seeds a real row, so a dispatch never crashes.
_GENERALIST_PROFILE = {
    "name": "generalist",
    "description": "General-purpose developer — the default dispatch: no persona overlay, "
                   "the account's default model.",
    "model": "", "overlay": "", "active": 1, "created_by": "system", "created_at": "",
}


def now_iso() -> str:
    # Microsecond precision so the propose trigger's `created_at > since` boundary
    # and ORDER BY created_at are exact (whole-second granularity dropped same-
    # second failures and made same-second ordering nondeterministic).
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    db_path = db_path or paths.DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # crash/copy-safe by construction (not header
        # luck); best-effort — the switch fails fast (not the 30s busy_timeout) under a
        # concurrent writer on a not-yet-WAL file, and the NEXT connection retries it
    except sqlite3.OperationalError:
        pass
    return conn


class Blackboard:
    """Single source of truth. Construct, use, close (or use as a context manager)."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or paths.DB_PATH
        self.conn = _conn(self.db_path)

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "Blackboard":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    def init_db(self) -> None:
        with open(paths.SCHEMA_SQL, "r", encoding="utf-8") as fh:
            self.conn.executescript(fh.read())
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations for DBs created before a column existed — SQLite has no
        ADD COLUMN IF NOT EXISTS, and CREATE TABLE IF NOT EXISTS won't alter an existing table.
        Idempotent: each ALTER runs only when the column is absent."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if cols and "spec_json" not in cols:           # tasks exists but predates the typed spec
            self.conn.execute("ALTER TABLE tasks ADD COLUMN spec_json TEXT NOT NULL DEFAULT '{}'")
        if cols and "milestone_id" not in cols:        # the plan link (Phase 2)
            self.conn.execute("ALTER TABLE tasks ADD COLUMN milestone_id INTEGER")
        if cols and "est_tokens" not in cols:          # per-task effort estimate (EVM PV)
            self.conn.execute("ALTER TABLE tasks ADD COLUMN est_tokens INTEGER NOT NULL DEFAULT 0")
        if cols and "profile" not in cols:             # worker-profile assignment (Phase 5)
            self.conn.execute("ALTER TABLE tasks ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
        if cols:                                        # index milestone_id AFTER it's guaranteed to exist
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_milestone ON tasks(milestone_id)")
        # budget_ledger gained shift attribution + wall-clock + worker profile (dashboard wishlist).
        bcols = {r[1] for r in self.conn.execute("PRAGMA table_info(budget_ledger)").fetchall()}
        if bcols and "shift_id" not in bcols:
            self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN shift_id INTEGER")
        if bcols and "seconds" not in bcols:
            self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN seconds REAL NOT NULL DEFAULT 0")
        if bcols and "profile" not in bcols:
            self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
        # learnings gained a recurrence counter (Task 0.5): each dedup-hit bumps it instead
        # of silently discarding the report — the frequency signal.
        lcols = {r[1] for r in self.conn.execute("PRAGMA table_info(learnings)").fetchall()}
        if lcols and "hits" not in lcols:
            self.conn.execute("ALTER TABLE learnings ADD COLUMN hits INTEGER NOT NULL DEFAULT 1")
        # learnings hygiene (Task 1.3): retire flag + deterministic-staleness flag.
        if lcols and "archived" not in lcols:
            self.conn.execute(
                "ALTER TABLE learnings ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if lcols and "stale" not in lcols:
            self.conn.execute(
                "ALTER TABLE learnings ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")
        # distill + pinned card ranking (Task 4.2): a pinned lesson renders FIRST and never
        # ages out of the memory_card (capped ~6/role); `factory learn distill --apply` sets it.
        if lcols and "pinned" not in lcols:
            self.conn.execute(
                "ALTER TABLE learnings ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        # consult-telemetry (Task 1.4): each surfaced learning gets the task's OUTCOME
        # attributed back — the effectiveness signal `learn list` renders (suppressed
        # below a minimum denominator; a 2-of-3 "ratio" is noise).
        if lcols and "merged_after" not in lcols:
            self.conn.execute(
                "ALTER TABLE learnings ADD COLUMN merged_after INTEGER NOT NULL DEFAULT 0")
        if lcols and "blocked_after" not in lcols:
            self.conn.execute(
                "ALTER TABLE learnings ADD COLUMN blocked_after INTEGER NOT NULL DEFAULT 0")

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        r = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None

    # -- champion -----------------------------------------------------------
    def get_champion(self) -> Optional[dict]:
        return self._one("SELECT * FROM champion ORDER BY promoted_at DESC LIMIT 1")

    def set_champion(self, id: str, spec_path: str, scores: dict | None = None) -> None:
        self._exec(
            "INSERT OR REPLACE INTO champion(id, spec_path, promoted_at, scores_json) "
            "VALUES (?,?,?,?)",
            (id, spec_path, now_iso(), json.dumps(scores or {})),
        )

    # -- candidates ---------------------------------------------------------
    def add_candidate(self, id: str, parent: str, spec_path: str, *,
                      change_summary: str = "", diff: dict | None = None,
                      stage: str = "proposed") -> None:
        self._exec(
            "INSERT INTO candidates(id, parent, spec_path, stage, created_at, "
            "change_summary, diff_json) VALUES (?,?,?,?,?,?,?)",
            (id, parent, spec_path, stage, now_iso(), change_summary,
             json.dumps(diff or {})),
        )

    def get_candidate(self, id: str) -> Optional[dict]:
        return self._one("SELECT * FROM candidates WHERE id = ?", (id,))

    def list_candidates(self, stage: Optional[str] = None) -> list[dict]:
        if stage:
            return self._all("SELECT * FROM candidates WHERE stage = ? ORDER BY created_at", (stage,))
        return self._all("SELECT * FROM candidates ORDER BY created_at")

    def set_stage(self, id: str, stage: str) -> None:
        self._exec("UPDATE candidates SET stage = ? WHERE id = ?", (stage, id))

    def set_candidate_scores(self, id: str, scores: dict) -> None:
        self._exec("UPDATE candidates SET scores_json = ? WHERE id = ?",
                   (json.dumps(scores), id))

    # -- scenarios ----------------------------------------------------------
    def upsert_scenario(self, id: str, *, cls: str, partition: str, source: str,
                        spec_path: str, goal: str = "", snapshot: str = "",
                        check_path: str = "") -> None:
        existing = self.get_scenario(id)
        leak = existing["leakage_count"] if existing else 0
        self._exec(
            "INSERT OR REPLACE INTO scenarios(id, class, partition, leakage_count, "
            "source, spec_path, goal, snapshot, check_path, active) "
            "VALUES (?,?,?,?,?,?,?,?,?,1)",
            (id, cls, partition, leak, source, spec_path, goal, snapshot, check_path),
        )

    def get_scenario(self, id: str) -> Optional[dict]:
        return self._one("SELECT * FROM scenarios WHERE id = ?", (id,))

    def list_scenarios(self, partition: Optional[str] = None,
                       active_only: bool = True) -> list[dict]:
        q = "SELECT * FROM scenarios WHERE 1=1"
        p: list[Any] = []
        if partition:
            q += " AND partition = ?"
            p.append(partition)
        if active_only:
            q += " AND active = 1"
        q += " ORDER BY id"
        return self._all(q, p)

    def increment_leakage(self, id: str, by: int = 1) -> None:
        self._exec("UPDATE scenarios SET leakage_count = leakage_count + ? WHERE id = ?",
                   (by, id))

    def retire_scenario(self, id: str) -> None:
        self._exec("UPDATE scenarios SET active = 0 WHERE id = ?", (id,))

    # -- runs ---------------------------------------------------------------
    def add_run(self, id: str, candidate_id: str, scenario_id: str, model: str,
                outcome: str, *, evidence_path: str = "", budget_used: int = 0,
                partition: str = "working", clive_claim: str = "",
                check_json: dict | None = None, duration_s: float = 0.0) -> None:
        self._exec(
            "INSERT INTO runs(id, candidate_id, scenario_id, model, outcome, "
            "evidence_path, budget_used, created_at, partition, clive_claim, "
            "check_json, duration_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (id, candidate_id, scenario_id, model, outcome, evidence_path,
             budget_used, now_iso(), partition, clive_claim,
             json.dumps(check_json or {}), duration_s),
        )

    def get_run(self, id: str) -> Optional[dict]:
        return self._one("SELECT * FROM runs WHERE id = ?", (id,))

    def runs_for_candidate(self, candidate_id: str) -> list[dict]:
        return self._all("SELECT * FROM runs WHERE candidate_id = ? ORDER BY created_at",
                         (candidate_id,))

    def all_runs(self) -> list[dict]:
        return self._all("SELECT * FROM runs ORDER BY created_at")

    def recent_failures(self, limit: int = 20) -> list[dict]:
        """Recent failing/erroring runs — the proposer's view of where reality
        surfaced gaps (working set only; the proposer is blind to held-out)."""
        return self._all(
            "SELECT r.*, s.goal AS scenario_goal FROM runs r JOIN scenarios s "
            "ON r.scenario_id = s.id WHERE r.outcome IN ('fail','error','blocked') "
            "AND r.partition = 'working' ORDER BY r.created_at DESC LIMIT ?",
            (limit,),
        )

    # -- judge --------------------------------------------------------------
    def add_judge_note(self, run_id: str, flags: dict) -> None:
        self._exec(
            "INSERT OR REPLACE INTO judge_notes(run_id, flags_json, created_at) "
            "VALUES (?,?,?)", (run_id, json.dumps(flags), now_iso()))

    def judge_note(self, run_id: str) -> Optional[dict]:
        return self._one("SELECT * FROM judge_notes WHERE run_id = ?", (run_id,))

    # -- promotions ---------------------------------------------------------
    def add_promotion(self, candidate_id: str, decision: str, operator: str,
                      rationale: str = "") -> None:
        self._exec(
            "INSERT INTO promotions(candidate_id, decision, operator, rationale, "
            "decided_at) VALUES (?,?,?,?,?)",
            (candidate_id, decision, operator, rationale, now_iso()))

    def promotions(self) -> list[dict]:
        return self._all("SELECT * FROM promotions ORDER BY decided_at DESC")

    # -- recalibrations -----------------------------------------------------
    def add_recalibration(self, note: str, production_window: str = "",
                          proxy_correlation: dict | None = None) -> None:
        self._exec(
            "INSERT INTO recalibrations(at, note, production_window, "
            "proxy_correlation_json) VALUES (?,?,?,?)",
            (now_iso(), note, production_window, json.dumps(proxy_correlation or {})))

    # -- budget -------------------------------------------------------------
    def add_budget(self, role_or_run: str, tokens: int, cost: float = 0.0,
                   notes: str = "", shift_id: Optional[int] = None,
                   seconds: float = 0.0, profile: str = "") -> None:
        self._exec(
            "INSERT INTO budget_ledger(at, role_or_run, tokens, cost, notes, shift_id, seconds, profile) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now_iso(), role_or_run, tokens, cost, notes, shift_id, seconds, profile))

    def budget_totals(self) -> dict:
        r = self._one("SELECT COALESCE(SUM(tokens),0) AS tokens, "
                      "COALESCE(SUM(cost),0) AS cost FROM budget_ledger")
        return r or {"tokens": 0, "cost": 0}

    def shift_spend(self, shift_id: int) -> dict:
        """Total tokens/cost/seconds attributed to one shift via the ledger."""
        r = self._one(
            "SELECT COALESCE(SUM(tokens),0) AS tokens, COALESCE(SUM(cost),0) AS cost, "
            "COALESCE(SUM(seconds),0) AS seconds FROM budget_ledger WHERE shift_id = ?",
            (shift_id,))
        return r or {"tokens": 0, "cost": 0.0, "seconds": 0.0}

    def ledger_rows(self, limit: int = 200, shift_id: Optional[int] = None) -> list[dict]:
        """Shift-attributed ledger engagements (conductor-loop only), newest first — the
        timesheet source (reporting/timesheets.py shapes them; SQL stays in this CRUD layer).
        `shift_id` filters to one shift IN THE QUERY, so `timesheet --shift N` never loses an
        older shift's rows to the LIMIT truncation. `id DESC` breaks same-timestamp ties so
        newest-first is deterministic even within one microsecond."""
        if shift_id is not None:
            return self._all(
                "SELECT at, role_or_run, tokens, cost, seconds, notes, shift_id, profile "
                "FROM budget_ledger WHERE shift_id = ? ORDER BY at DESC, id DESC LIMIT ?",
                (shift_id, limit))
        return self._all(
            "SELECT at, role_or_run, tokens, cost, seconds, notes, shift_id, profile "
            "FROM budget_ledger WHERE shift_id IS NOT NULL ORDER BY at DESC, id DESC LIMIT ?",
            (limit,))

    def ledger_by_role(self) -> list[dict]:
        """All-time per-role rollup over the WHOLE ledger (incl. legacy old-loop rows),
        highest-spend first. role = the part before ':' in role_or_run (developer:<task> → developer)."""
        return self._all(
            "SELECT CASE WHEN instr(role_or_run,':')>0 "
            "         THEN substr(role_or_run,1,instr(role_or_run,':')-1) "
            "         ELSE role_or_run END AS role, "
            "COUNT(*) AS engagements, COALESCE(SUM(tokens),0) AS tokens, "
            "COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(seconds),0) AS seconds "
            "FROM budget_ledger GROUP BY role ORDER BY tokens DESC")

    def budget_entries(self) -> list[dict]:
        return self._all("SELECT * FROM budget_ledger ORDER BY at")

    # -- safety -------------------------------------------------------------
    def add_safety_flag(self, run_id: str, kind: str, detail: str,
                        severity: str) -> None:
        self._exec(
            "INSERT INTO safety_flags(run_id, kind, detail, severity) "
            "VALUES (?,?,?,?)", (run_id, kind, detail, severity))

    def safety_flags_for_candidate(self, candidate_id: str) -> list[dict]:
        return self._all(
            "SELECT sf.*, r.scenario_id, r.model FROM safety_flags sf "
            "JOIN runs r ON sf.run_id = r.id WHERE r.candidate_id = ? "
            "ORDER BY sf.id", (candidate_id,))

    def all_safety_flags(self) -> list[dict]:
        return self._all(
            "SELECT sf.*, r.candidate_id, r.scenario_id, r.model FROM safety_flags sf "
            "JOIN runs r ON sf.run_id = r.id ORDER BY sf.id DESC")

    # =======================================================================
    # The conductor loop (design: docs/plans/2026-06-25-conductor-loop-design.md)
    # =======================================================================

    # -- mission: the human's single steer ----------------------------------
    def set_mission(self, statement: str, target_repo: str = "") -> int:
        """Set the active mission; any prior active mission steps down. Returns its id.
        ATOMIC: deactivate + insert commit together, so a concurrent reader never sees a
        window with zero active missions (the conductor reads active_mission() every shift)."""
        self.conn.execute("UPDATE mission SET active = 0 WHERE active = 1")
        cur = self.conn.execute(
            "INSERT INTO mission(statement, target_repo, created_at, active) VALUES (?,?,?,1)",
            (statement, target_repo, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def active_mission(self) -> Optional[dict]:
        return self._one("SELECT * FROM mission WHERE active = 1 ORDER BY id DESC LIMIT 1")

    # -- tasks: the backlog -------------------------------------------------
    def add_task(self, id: str, title: str, *, source: str, detail: str = "",
                 source_ref: str = "", spec: Optional[dict] = None) -> None:
        ts = now_iso()
        self._exec(
            "INSERT INTO tasks(id, title, detail, source, source_ref, status, spec_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,'open',?,?,?)",
            (id, title, detail, source, source_ref, json.dumps(spec or {}), ts, ts))

    def set_task_spec(self, id: str, spec: Optional[dict]) -> None:
        """Persist a task's typed spec (target_surface/acceptance/…) — durable across shifts,
        so the scope check's verdict outlives the shift it ran in."""
        self._exec("UPDATE tasks SET spec_json = ?, updated_at = ? WHERE id = ?",
                   (json.dumps(spec or {}), now_iso(), id))

    def set_task_estimate(self, id: str, est_tokens: int) -> None:
        """The conductor's per-task effort estimate (EVM task-level PV; Phase 2/4)."""
        self._exec("UPDATE tasks SET est_tokens = ?, updated_at = ? WHERE id = ?",
                   (int(est_tokens), now_iso(), id))

    def set_task_profile(self, id: str, profile: str) -> None:
        """Assign the worker profile a task should dispatch with ('' = generalist; Phase 5)."""
        self._exec("UPDATE tasks SET profile = ?, updated_at = ? WHERE id = ?",
                   (profile, now_iso(), id))

    @staticmethod
    def _with_spec(row: Optional[dict]) -> Optional[dict]:
        if row is not None and "spec_json" in row:
            try:
                row["spec"] = json.loads(row.get("spec_json") or "{}")
            except Exception:  # noqa: BLE001 — a corrupt blob degrades to an empty spec
                row["spec"] = {}
        return row

    def get_task(self, id: str) -> Optional[dict]:
        return self._with_spec(self._one("SELECT * FROM tasks WHERE id = ?", (id,)))

    def list_tasks(self, status: Optional[str] = None) -> list[dict]:
        if status:
            rows = self._all("SELECT * FROM tasks WHERE status = ? ORDER BY created_at", (status,))
        else:
            rows = self._all("SELECT * FROM tasks ORDER BY created_at")
        return [self._with_spec(r) for r in rows]

    def recent_blocked_tasks(self, limit: int = 8) -> list[dict]:
        """The last N blocked tasks NEWEST-FIRST by updated_at (Task 1.1's {BLOCKED} seam).
        Dedicated query: list_tasks orders by created_at ASC, which buries the freshest
        failures — exactly the rows the conductor must react to first."""
        rows = self._all("SELECT * FROM tasks WHERE status = 'blocked' "
                         "ORDER BY updated_at DESC LIMIT ?", (int(limit),))
        return [self._with_spec(r) for r in rows]

    def set_task_detail(self, id: str, detail: str) -> None:
        """Replace a task's brief (the `task reopen` verb narrows a blocked task's detail;
        callers own exact-id discipline — this is a plain UPDATE)."""
        self._exec("UPDATE tasks SET detail = ?, updated_at = ? WHERE id = ?",
                   (detail, now_iso(), id))

    def set_task_status(self, id: str, status: str, *, result: Optional[str] = None,
                        shift_id: Optional[int] = None) -> None:
        sets, params = ["status = ?", "updated_at = ?"], [status, now_iso()]
        if result is not None:
            sets.append("result = ?"); params.append(result)
        if shift_id is not None:
            sets.append("shift_id = ?"); params.append(shift_id)
        params.append(id)
        self._exec(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)

    # -- shifts: bounded sessions that resume -------------------------------
    def start_shift(self, *, token_budget: int, mission_id: Optional[int] = None) -> int:
        cur = self._exec(
            "INSERT INTO shifts(mission_id, token_budget, status, started_at) "
            "VALUES (?,?,'running',?)", (mission_id, token_budget, now_iso()))
        return cur.lastrowid

    def end_shift(self, shift_id: int, *, status: str, report: str = "",
                  resume_note: str = "", tokens_used: int = 0) -> None:
        self._exec(
            "UPDATE shifts SET status = ?, report = ?, resume_note = ?, tokens_used = ?, "
            "ended_at = ? WHERE id = ?",
            (status, report, resume_note, tokens_used, now_iso(), shift_id))

    def get_shift(self, shift_id: int) -> Optional[dict]:
        """One shift row by id — the rail reads its token_budget on the MAIN THREAD (Task 3.2's
        retry_budget_ok, brake-honest) without a private-query into another module."""
        return self._one("SELECT * FROM shifts WHERE id = ?", (shift_id,))

    def last_shift(self) -> Optional[dict]:
        return self._one("SELECT * FROM shifts ORDER BY id DESC LIMIT 1")

    def prior_shift(self, before_id: int) -> Optional[dict]:
        """The most recent shift BEFORE before_id — the conductor's resume anchor (the
        current shift, just started by the harness, isn't where the resume note lives)."""
        return self._one("SELECT * FROM shifts WHERE id < ? ORDER BY id DESC LIMIT 1",
                         (before_id,))

    def list_shifts(self, limit: int = 50) -> list[dict]:
        """All shifts, newest first — for the fleet view / a run history."""
        return self._all("SELECT * FROM shifts ORDER BY id DESC LIMIT ?", (limit,))

    def count_shifts(self) -> int:
        """The TRUE total shift count (the fleet view lists only the last N)."""
        return self._one("SELECT COUNT(*) AS n FROM shifts")["n"]

    def shifts_token_total(self) -> int:
        """Cumulative tokens_used across ALL shifts — the truthful lifetime token total for
        the board KPI (the fleet view sums only its last-N window, which undercounts once the
        history grows beyond it)."""
        return int(self._one("SELECT COALESCE(SUM(tokens_used),0) AS n FROM shifts")["n"])

    def set_shift_tokens(self, shift_id: int, tokens_used: int) -> None:
        """Keep a shift's spend current DURING the shift — so a hard ceiling-kill (which
        skips end_shift) still leaves a usable figure for the next shift's resume math."""
        self._exec("UPDATE shifts SET tokens_used = ? WHERE id = ?", (tokens_used, shift_id))

    def running_shifts(self) -> list[dict]:
        """Shifts still marked 'running'. At startup (shifts run one-at-a-time) any such
        row is a CRASHED shift — the harness kills from outside, so end_shift never ran."""
        return self._all("SELECT * FROM shifts WHERE status = 'running' ORDER BY id")

    # -- milestones: the plan (conductor-maintained, revised each shift) -----
    def add_milestone(self, title: str, *, mission_id: Optional[int] = None,
                      deliverable: str = "", acceptance: str = "", budget_tokens: int = 0,
                      planned_order: int = 0, created_by: str = "conductor") -> int:
        cur = self._exec(
            "INSERT INTO milestones(mission_id, title, deliverable, acceptance, "
            "planned_order, budget_tokens, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (mission_id, title, deliverable, acceptance, planned_order, budget_tokens,
             created_by, now_iso()))
        return cur.lastrowid

    def list_milestones(self, status: Optional[str] = None,
                        mission_id: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM milestones"
        conds, params = [], []
        if status is not None:
            conds.append("status = ?"); params.append(status)
        if mission_id is not None:
            conds.append("mission_id = ?"); params.append(mission_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY planned_order, id"
        return self._all(sql, tuple(params))

    def get_milestone(self, milestone_id: int) -> Optional[dict]:
        return self._one("SELECT * FROM milestones WHERE id = ?", (milestone_id,))

    def set_milestone_status(self, milestone_id: int, status: str) -> None:
        """Set a milestone's status; stamp delivered_at when it is delivered."""
        if status == "delivered":
            self._exec("UPDATE milestones SET status = ?, delivered_at = ? WHERE id = ?",
                       (status, now_iso(), milestone_id))
        else:
            self._exec("UPDATE milestones SET status = ? WHERE id = ?", (status, milestone_id))

    def set_task_milestone(self, task_id: str, milestone_id: Optional[int]) -> None:
        self._exec("UPDATE tasks SET milestone_id = ?, updated_at = ? WHERE id = ?",
                   (milestone_id, now_iso(), task_id))

    def milestone_progress(self, milestone_id: int) -> dict:
        r = self._one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
            "FROM tasks WHERE milestone_id = ?", (milestone_id,))
        return {"done": int(r["done"] or 0), "total": int(r["total"] or 0)}

    def milestone_open_task_ids(self, milestone_id: int) -> list[str]:
        """Task 3.3: the ids of a milestone's linked tasks that are NOT yet resolved. RESOLVED =
        `done` OR `dropped` — BOTH are terminal, so a 'dropped' (a legal RESOLVED task status)
        never makes delivery unreachable; everything else (open/claimed/in_progress/blocked) is
        in-flight. The milestone-delivery grader refuses a premature 'delivered' while this list is
        non-empty and derives the render-time '(unverified)' label from it. Ordered by created_at
        for a stable, exact-id refusal message."""
        rows = self._all(
            "SELECT id FROM tasks WHERE milestone_id = ? "
            "AND status NOT IN ('done','dropped') ORDER BY created_at", (milestone_id,))
        return [r["id"] for r in rows]

    def milestone_effort(self, milestone_id: int) -> dict:
        """Estimated vs actual tokens for a milestone's linked tasks: est = SUM(tasks.est_tokens);
        actual = SUM(ledger tokens for those tasks' developer engagements). The estimate-vs-reality
        signal the conductor revises the plan against (Task 2.4)."""
        est = self._one(
            "SELECT COALESCE(SUM(est_tokens),0) AS n FROM tasks WHERE milestone_id = ?",
            (milestone_id,))["n"]
        actual = self._one(
            "SELECT COALESCE(SUM(bl.tokens),0) AS n FROM budget_ledger bl "
            "JOIN tasks t ON bl.role_or_run = 'developer:' || t.id WHERE t.milestone_id = ?",
            (milestone_id,))["n"]
        return {"est_tokens": int(est or 0), "actual_tokens": int(actual or 0)}

    # -- worker capability profiles: the conductor's on-demand workforce -----
    def add_profile(self, name: str, *, description: str, model: str = "", overlay: str = "",
                    created_by: str = "operator", replace: bool = True) -> None:
        """Create/replace a worker capability profile (DATA only: persona overlay + model tier —
        it can never change the toolset, sandbox, frozen surface or gates). `replace=True`
        (the CLI/board path) reactivates+overwrites via INSERT OR REPLACE; `replace=False` (the
        seeder) uses INSERT OR IGNORE so a re-seed can't clobber a conductor-tuned overlay or
        resurrect a retired profile. Rejects a bad slug so a generated name is always safe."""
        if not _PROFILE_SLUG_RE.match(name or ""):
            raise ValueError(f"invalid profile slug {name!r} — need ^[a-z0-9][a-z0-9-]{{1,31}}$")
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        self._exec(
            f"{verb} INTO worker_profiles(name, description, model, overlay, active, "
            "created_by, created_at) VALUES (?,?,?,?,1,?,?)",
            (name, description, model, overlay, created_by, now_iso()))

    def get_profile(self, name: str) -> Optional[dict]:
        """Resolve a profile by name. '' and 'generalist' ALWAYS resolve — to a synthetic
        generalist when no row exists yet — so a dispatch with an empty/absent profile never
        crashes pre-seed. Any other missing name returns None (the caller fails open to
        generalist). Retired profiles still resolve (history references their names)."""
        key = name or "generalist"
        row = self._one("SELECT * FROM worker_profiles WHERE name = ?", (key,))
        if row is None and key == "generalist":
            return dict(_GENERALIST_PROFILE)
        return row

    def list_profiles(self, active_only: bool = False) -> list[dict]:
        """Profiles, name-ordered. active_only hides retired ones (the board's live bench);
        the full list is the timesheet/audit view. The synthetic generalist is NOT listed until
        a real row is seeded (Task 5.2) — it exists as a fallback, not a managed profile."""
        if active_only:
            return self._all("SELECT * FROM worker_profiles WHERE active = 1 ORDER BY name")
        return self._all("SELECT * FROM worker_profiles ORDER BY name")

    def retire_profile(self, name: str) -> None:
        """Deactivate a profile (never DELETE — the ledger/timesheet reference its name)."""
        self._exec("UPDATE worker_profiles SET active = 0 WHERE name = ?", (name,))

    def profile_stats(self) -> list[dict]:
        """Per-profile developer-outcome rollup (grouped by budget_ledger.profile over developer
        rows): engagements, merged/blocked counts (notes = the verdict), tokens, cost — the
        workforce-evolution signal (Task 5.7). est_accuracy is layered on in reporting.timesheets
        (it needs the per-task est join)."""
        # 'halted' is a STOP-brake artifact (the task is requeued, not failed), so it counts
        # toward engagements + spend but NOT as a blocked failure — else a mid-round STOP would
        # phantom-fail a healthy profile and could drive a wrongful retire.
        return self._all(
            "SELECT profile, COUNT(*) AS engagements, "
            "SUM(CASE WHEN notes='merged' THEN 1 ELSE 0 END) AS merged, "
            "SUM(CASE WHEN notes NOT IN ('merged','','halted') THEN 1 ELSE 0 END) AS blocked, "
            "COALESCE(SUM(tokens),0) AS tokens, COALESCE(SUM(cost),0) AS cost "
            "FROM budget_ledger WHERE role_or_run LIKE 'developer:%' AND profile <> '' "
            "GROUP BY profile ORDER BY tokens DESC")

    def profile_task_actuals(self) -> list[dict]:
        """Per (profile, task) actual developer tokens joined to tasks.est_tokens — reporting
        derives each profile's estimate accuracy (median actual/est) from this. 'developer:' is
        10 chars, so substr(...,11) is the task id."""
        return self._all(
            "SELECT b.profile, substr(b.role_or_run,11) AS task_id, "
            "COALESCE(SUM(b.tokens),0) AS actual, COALESCE(MAX(t.est_tokens),0) AS est "
            "FROM budget_ledger b LEFT JOIN tasks t ON t.id = substr(b.role_or_run,11) "
            "WHERE b.role_or_run LIKE 'developer:%' AND b.profile <> '' "
            "GROUP BY b.profile, task_id")

    # -- settings: whitelisted runtime overrides (Phase 6.1) ----------------
    def get_setting(self, key: str) -> Optional[str]:
        r = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return r["value"] if r else None

    def set_setting(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?,?,?)",
                   (key, str(value), now_iso()))

    def all_settings(self) -> dict:
        return {r["key"]: r["value"] for r in self._all("SELECT key, value FROM settings")}

    def milestone_task_rows(self, milestone_id: int) -> list[dict]:
        """Per linked task: id, title, est_tokens, status, and the ledgered ACTUAL developer
        tokens/cost (the developer:<task_id> rows). One query so EVM (reporting/evm.py) can do
        est-weighted partial credit AND the est-vs-actual table without duplicating SQL — the
        SQL stays here in the CRUD layer (the Phase 3 convention)."""
        return self._all(
            "SELECT t.id, t.title, t.est_tokens, t.status, "
            "COALESCE((SELECT SUM(b.tokens) FROM budget_ledger b "
            "          WHERE b.role_or_run = 'developer:' || t.id),0) AS actual_tokens, "
            "COALESCE((SELECT SUM(b.cost) FROM budget_ledger b "
            "          WHERE b.role_or_run = 'developer:' || t.id),0) AS actual_cost "
            "FROM tasks t WHERE t.milestone_id = ? ORDER BY t.created_at", (milestone_id,))

    def tasks_in_flight(self, shift_id: Optional[int] = None) -> list[dict]:
        """Tasks a shift had claimed/started (and would orphan if it died)."""
        if shift_id is not None:
            rows = self._all(
                "SELECT * FROM tasks WHERE status IN ('claimed','in_progress') "
                "AND shift_id = ? ORDER BY created_at", (shift_id,))
        else:
            rows = self._all(
                "SELECT * FROM tasks WHERE status IN ('claimed','in_progress') ORDER BY created_at")
        return [self._with_spec(r) for r in rows]

    def requeue_shift_tasks(self, shift_id: int) -> int:
        """Return a shift's in-flight (claimed/in_progress) tasks to 'open' so the next
        shift re-picks them. Used by crash-reap AND by abnormal shift ends (a conductor
        that times out or errors after claiming work). Returns how many were requeued."""
        n = 0
        for t in self.tasks_in_flight(shift_id):
            self.set_task_status(t["id"], "open")
            n += 1
        return n

    def current_shift_id(self) -> Optional[int]:
        """The id of the shift currently running (for the conductor's `task` CLI to stamp
        onto the work it claims/finishes), or None if no shift is in progress."""
        sh = self.last_shift()
        return sh["id"] if sh and sh["status"] == "running" else None

    def reap_orphaned_shifts(self, *, reason: str = "killed before a clean end") -> list[dict]:
        """Crash recovery, called on startup before a new shift. Any shift still 'running'
        was killed from outside (a ceiling trip): close it 'error' with a synthetic resume
        note and return its in-flight tasks to 'open' so the next shift re-picks them.
        Returns the reaped shift rows (as they were, for the report)."""
        orphans = self.running_shifts()
        for sh in orphans:
            self.requeue_shift_tasks(sh["id"])
            note = sh["resume_note"] or f"shift {sh['id']} {reason}; reconciled on resume"
            # The ledger holds the crashed shift's real spend (researcher/conductor rows land
            # before the kill), so fold it in — the stale in-row figure is usually 0.
            ledgered = int(self.shift_spend(sh["id"])["tokens"])
            self.end_shift(sh["id"], status="error", resume_note=note,
                           tokens_used=max(int(sh["tokens_used"] or 0), ledgered))
        return orphans

    # -- digests: the research<->dev feedback loop --------------------------
    def add_digest(self, *, shift_id: Optional[int], shipped: list,
                   summary: str = "") -> int:
        cur = self._exec(
            "INSERT INTO digests(shift_id, shipped_json, summary, consumed, created_at) "
            "VALUES (?,?,?,0,?)", (shift_id, json.dumps(shipped or []), summary, now_iso()))
        return cur.lastrowid

    def unconsumed_digests(self) -> list[dict]:
        rows = self._all("SELECT * FROM digests WHERE consumed = 0 ORDER BY id")
        for r in rows:
            r["shipped"] = json.loads(r.pop("shipped_json"))
        return rows

    def mark_digest_consumed(self, id: int) -> None:
        self._exec("UPDATE digests SET consumed = 1 WHERE id = ?", (id,))

    # -- mission status: the advancing / steady_state / blocked timeline ----
    def record_mission_status(self, *, shift_id: Optional[int], status: str,
                              rationale: str = "", metrics: dict | None = None) -> None:
        self._exec(
            "INSERT INTO mission_status(shift_id, status, rationale, metrics_json, at) "
            "VALUES (?,?,?,?,?)",
            (shift_id, status, rationale, json.dumps(metrics or {}), now_iso()))

    def latest_mission_status(self) -> Optional[dict]:
        r = self._one("SELECT * FROM mission_status ORDER BY id DESC LIMIT 1")
        if r:
            r["metrics"] = json.loads(r.pop("metrics_json"))
        return r

    def mission_status_history(self, limit: int = 5) -> list[dict]:
        """The last `limit` statuses, newest first — for steady-state/plateau detection."""
        rows = self._all("SELECT * FROM mission_status ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["metrics"] = json.loads(r.pop("metrics_json"))
        return rows

    # -- factory memory: per-role learnings ---------------------------------
    def add_learning(self, role: str, content: str, *, agent: str = "",
                     scope: str = "general", shift_id: Optional[int] = None) -> int:
        """Append a learning for `role`. Returns its id. CRUD only — dedup/format live
        in reporting.factory_memory."""
        cur = self._exec(
            "INSERT INTO learnings(role, agent, scope, content, shift_id, uses, hits, "
            "created_at) VALUES (?,?,?,?,?,0,1,?)",
            (role, agent, scope, content, shift_id, now_iso()))
        return cur.lastrowid

    def get_learning(self, learning_id: int) -> Optional[dict]:
        """One learning by exact integer id, or None — the id is the PRIMARY KEY, so
        there is no partial-match ambiguity here."""
        return self._one("SELECT * FROM learnings WHERE id = ?", (learning_id,))

    def bump_learning_hits(self, learning_id: int) -> None:
        """Increment ONE learning's recurrence counter — called when a fresh report
        dedups onto it (Task 0.5: count the recurrence instead of destroying it)."""
        self._exec("UPDATE learnings SET hits = hits + 1 WHERE id = ?", (learning_id,))

    def archive_learning(self, learning_id: int) -> None:
        """Retire a learning (`factory learn retire`, Task 1.3): archived=1 hides it from
        prompts (learnings_for_role default view) — the row itself is kept as history AND
        stays visible to record_learning's dedup window so a re-report of the same lesson
        dedups onto it (hidden, hits-bumped) instead of resurrecting it as a live row.
        Callers refuse unknown ids via get_learning FIRST (exact-id discipline)."""
        self._exec("UPDATE learnings SET archived = 1 WHERE id = ?", (learning_id,))

    def set_learning_stale(self, learning_id: int, stale: bool) -> None:
        """Flag / clear the deterministic-staleness bit (`factory learn verify`, Task 1.3).
        Advisory only: a stale row still surfaces, with a card suffix warning the reader."""
        self._exec("UPDATE learnings SET stale = ? WHERE id = ?",
                   (1 if stale else 0, learning_id))

    def pin_learning(self, learning_id: int) -> None:
        """Pin a learning (`factory learn distill --apply`, Task 4.2): pinned=1 makes it
        render FIRST in the role's memory_card and never age out of the newest-N window.
        Callers refuse unknown ids via get_learning FIRST (exact-id discipline)."""
        self._exec("UPDATE learnings SET pinned = 1 WHERE id = ?", (learning_id,))

    def pinned_for_role(self, role: str, limit: int = 6) -> list[dict]:
        """A role's LIVE pinned learnings, newest first — the pinned leg of memory_card
        (Task 4.2). archived=0 so a pinned-then-retired row never resurfaces; `limit` caps
        the leg (~6/role) so unbounded pins can't regrow the card the phase shrinks."""
        return self._all(
            "SELECT * FROM learnings WHERE role = ? AND pinned = 1 AND archived = 0 "
            "ORDER BY id DESC LIMIT ?", (role, limit))

    def learnings_for_role(self, role: str, limit: int = 10, *,
                           include_archived: bool = False) -> list[dict]:
        """A role's LIVE (non-retired) learnings, newest first (id DESC is stable even
        within one tick). Retired rows (archived=1) never surface by default — prompts
        read this seam as-is (Task 1.3). include_archived=True is the DEDUP window's
        view (record_learning): a retired lesson must keep absorbing re-reports (bump
        hits, stay hidden) instead of resurrecting as a fresh live row — the factory
        auto-records templated lessons on recurring failures, so the exact lesson an
        operator retired WILL be reported again verbatim."""
        where = "role = ?" if include_archived else "role = ? AND archived = 0"
        return self._all(
            f"SELECT * FROM learnings WHERE {where} ORDER BY id DESC LIMIT ?",
            (role, limit))

    def all_learnings(self, limit: int = 50) -> list[dict]:
        """Every role's learnings, newest first — the factory-wide memory view."""
        return self._all("SELECT * FROM learnings ORDER BY id DESC LIMIT ?", (limit,))

    def bump_learning_uses(self, ids: Iterable[int]) -> None:
        """Increment the surfaced-count for the given learnings (cheap relevance signal). One
        batched UPDATE+commit (not one transaction per id) — a memory_card surfaces up to 16
        rows on every prompt build, so the per-id loop was 16 commits per build."""
        ids = list(ids)
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._exec(f"UPDATE learnings SET uses = uses + 1 WHERE id IN ({placeholders})", ids)

    def bump_learning_outcomes(self, ids: Iterable[int], *, merged: bool) -> None:
        """Attribute a task's close-out outcome to the learnings surfaced into its worker
        card (Task 1.4): ONE batched UPDATE bumping merged_after (merged=True) or
        blocked_after. MAIN thread only (single-writer connection). Signal, not proof —
        the card is one input among many, so readers suppress the ratio below a minimum
        denominator (reporting.factory_memory.effectiveness)."""
        ids = list(ids)
        if not ids:
            return
        col = "merged_after" if merged else "blocked_after"
        placeholders = ",".join("?" * len(ids))
        self._exec(f"UPDATE learnings SET {col} = {col} + 1 WHERE id IN ({placeholders})", ids)

    # -- task evidence: per-task failure forensics (Task 0.4, P6 stage 1) ---
    def add_task_evidence(self, task_id: str, *, shift_id: Optional[int] = None,
                          action: str = "", stage: str = "", tests_report: str = "",
                          reply_head: str = "") -> int:
        """Persist WHY a task failed at close-out — the full tests_report + the worker's
        reply head, which the ≤200-char tasks.result reason cannot carry. Returns the row
        id. Passive write, MAIN thread only (single-writer connection); zero LLM."""
        cur = self._exec(
            "INSERT INTO task_evidence(task_id, shift_id, action, stage, tests_report, "
            "reply_head, created_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, shift_id, action, stage, tests_report, reply_head, now_iso()))
        return cur.lastrowid

    def task_evidence(self, task_id: str) -> list[dict]:
        """One task's failure-evidence rows, newest first (id DESC is stable within a
        tick). Reader for the `factory task evidence` verb + the investigator (Task 4.1)."""
        return self._all(
            "SELECT * FROM task_evidence WHERE task_id = ? ORDER BY id DESC", (task_id,))

    # -- gate-eval outcomes: per-golden-case results (Task 2.1, P12) --------
    def add_gate_eval_result(self, gate: str, case_id: str, ok: bool,
                             verdict: str = "") -> int:
        """Persist one golden case's outcome from `factory eval-gates` — the durable
        per-case history the flip detector (ok→fail = a gate regression) compares
        against on the next run. Append-only, MAIN thread only (single-writer)."""
        cur = self._exec(
            "INSERT INTO gate_eval_results(gate, case_id, ok, verdict, created_at) "
            "VALUES (?,?,?,?,?)",
            (gate, case_id, 1 if ok else 0, verdict, now_iso()))
        return cur.lastrowid

    def latest_gate_eval_results(self, gate: str) -> list[dict]:
        """Each case's MOST RECENT outcome for one gate — the previous run's scorecard.
        MAX(id) per case_id (ids are monotonic), so a case dropped from the fixture
        file simply stops appearing in new runs but keeps its history."""
        return self._all(
            "SELECT * FROM gate_eval_results WHERE id IN ("
            "SELECT MAX(id) FROM gate_eval_results WHERE gate = ? GROUP BY case_id)",
            (gate,))

    # -- auto issue-sync: idempotency ledger --------------------------------
    def issue_sync_seen(self, issue_number: int, commit_sha: str) -> bool:
        """True if this (issue, commit) pair has already been synced — the guard that
        keeps a resume/re-run from double-commenting or re-closing an issue."""
        return self._one(
            "SELECT 1 FROM issue_sync WHERE issue_number = ? AND commit_sha = ?",
            (issue_number, commit_sha)) is not None

    def record_issue_sync(self, issue_number: int, commit_sha: str, action: str,
                          url: str = "") -> None:
        """Record that (issue, commit) was synced. INSERT OR REPLACE so a re-record of
        the same pair (e.g. a retried sync) is a harmless no-op rather than an error."""
        self._exec(
            "INSERT OR REPLACE INTO issue_sync(issue_number, commit_sha, action, url, "
            "created_at) VALUES (?,?,?,?,?)",
            (issue_number, commit_sha, action, url, now_iso()))
