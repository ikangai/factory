"""The blackboard: a thin, explicit SQLite access layer (spec §8).

Roles never message each other; they read and write this store, and the
orchestrator sequences them. This module is CRUD only — scoring/divergence math
lives in scoring.py so the grader logic is inspectable in one place.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from . import paths


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
        # budget_ledger gained shift attribution + wall-clock + worker profile (dashboard wishlist).
        bcols = {r[1] for r in self.conn.execute("PRAGMA table_info(budget_ledger)").fetchall()}
        if bcols and "shift_id" not in bcols:
            self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN shift_id INTEGER")
        if bcols and "seconds" not in bcols:
            self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN seconds REAL NOT NULL DEFAULT 0")
        if bcols and "profile" not in bcols:
            self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN profile TEXT NOT NULL DEFAULT ''")

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

    def set_shift_tokens(self, shift_id: int, tokens_used: int) -> None:
        """Keep a shift's spend current DURING the shift — so a hard ceiling-kill (which
        skips end_shift) still leaves a usable figure for the next shift's resume math."""
        self._exec("UPDATE shifts SET tokens_used = ? WHERE id = ?", (tokens_used, shift_id))

    def running_shifts(self) -> list[dict]:
        """Shifts still marked 'running'. At startup (shifts run one-at-a-time) any such
        row is a CRASHED shift — the harness kills from outside, so end_shift never ran."""
        return self._all("SELECT * FROM shifts WHERE status = 'running' ORDER BY id")

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
            self.end_shift(sh["id"], status="error", resume_note=note,
                           tokens_used=sh["tokens_used"])
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
            "INSERT INTO learnings(role, agent, scope, content, shift_id, uses, "
            "created_at) VALUES (?,?,?,?,?,0,?)",
            (role, agent, scope, content, shift_id, now_iso()))
        return cur.lastrowid

    def learnings_for_role(self, role: str, limit: int = 10) -> list[dict]:
        """A role's learnings, newest first (id DESC is stable even within one tick)."""
        return self._all(
            "SELECT * FROM learnings WHERE role = ? ORDER BY id DESC LIMIT ?",
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
