# Dashboard Wishlist Implementation Roadmap

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the human-interface wishlist gaps found in the 2026-07-02 gap analysis: truthful token/cost accounting, mission steering from MISSION.md, a first-class plan (milestones/deliverables with effort estimates), worker capability profiles with per-task model routing and on-demand generation, agent timesheets, agent-adapted EVM, a resources/role-management surface, and a multi-view Mission Control dashboard.

**Architecture:** Everything builds on the existing blackboard (`store/blackboard.db`) + deterministic-rail pattern: new data lands in SQLite via `common/store.py` CRUD, pure gather/compute functions live in `reporting/`, the fleet server (`dashboard/fleet_server.py`) exposes them as JSON endpoints, and the self-contained `dashboard/static/fleet.html` renders them as tabs. LLM roles (conductor) gain new *levers* (CLI subcommands) but no new authority; all writes stay on the main thread (single-writer SQLite). **Worker specialization is data, not code:** a capability profile = a persona overlay + a model tier, instantiated by the rail per task — the conductor can generate new profiles at runtime without any code change, which is the workforce-evolution seam.

**Model-tier policy (the standing rule, enforced by construction):** the **frontier model** (the operator's default — Fable/Opus class) is reserved for *judgment* work: the conductor (planning, estimating, assigning), the scope/decompose judges, and the pre-merge reviewer. **Super-workers** run on the tier their task profile names (standard/fast/frontier), resolved through a config whitelist; unknown or unavailable tiers fail open to `standard` (never silently up to frontier). A super-worker may further right-size internally (its Task/Workflow tools can fan mechanical subtasks out to cheaper models), but the rail assigns its primary model — the worker never picks its own tier.

**Tech stack:** Python 3 stdlib (sqlite3, http.server), pytest, vanilla JS/inline SVG in one self-contained HTML file. No new dependencies.

---

## Operational ground rules (read before every phase)

1. **The factory may be live.** Before touching rail code (`orchestrator/`, `roles/`, `common/store.py`): engage the brake and confirm no shift is running:
   ```bash
   touch STOP && ./bin/factory mode shift
   sqlite3 store/blackboard.db "SELECT id,status FROM shifts ORDER BY id DESC LIMIT 1"
   # expect status != 'running'
   ```
   After the phase's tests pass, restore the prior mode and remove STOP **only if the operator had it clear** (it was engaged as of 2026-07-02 — check first, leave it as you found it).
2. **Single-writer SQLite:** every new `store.*` write must happen on the main thread (the pattern in `execute_claimed_tasks` — workers return dicts, the main loop writes). Never call the store from inside the `ThreadPoolExecutor` workers. **Dashboard clause (Tasks 1.2/6.2):** the fleet server becomes the first *second process* writing the store (today it makes zero store writes — its docstring says so). Handlers must open a fresh per-request `Blackboard` (connections are `check_same_thread=True` by default), keep transactions short, and answer a busy `sqlite3.OperationalError` with HTTP 503 — never share a store instance across `ThreadingHTTPServer` handler threads. WAL (`store/schema.sql:9`) + `sqlite3.connect(timeout=30)` make cross-process writes safe.
3. **Schema changes go in BOTH places:** `store/schema.sql` (fresh DBs, `CREATE TABLE IF NOT EXISTS`) **and** `Blackboard._migrate()` in `common/store.py:61` (existing DBs, guarded `ALTER TABLE`). Follow the existing `spec_json` example exactly.
4. **Inline JS must be `node --check`ed** before claiming the dashboard works (a syntax error silently freezes the page while the server stays green). Use the extraction snippet in the Verification section.
5. **TDD:** every task below is test-first. Run the full suite (`python3 -m pytest tests/ -q`) at the end of each phase, not just the new tests.
6. Commit after every green task (small commits, `feat(...)`/`fix(...)` style as in `git log`).

**Dependency order:** Phase 0 → (1, 2 independent) → 3 needs 0 → 4 needs 0+2 (richer with 5) → 5 needs 2 (task columns; Task 5.2 pulls the `settings` table forward from 6.1) → 6 needs 5 (profile management) → 7 integrates all → 8 optional.

---

## Phase 0 — Truthful accounting (foundation; unblocks timesheets, EVM, cost views)

**Problem:** developer tokens/cost are computed (`roles/common.py:283` returns `{branch, reply, tokens, cost}`) and then **dropped** — `develop_and_merge` (`orchestrator/develop.py:205`) returns only the round result. Nothing from the conductor loop reaches `budget_ledger`; `shifts.tokens_used` holds conductor tokens only; the fleet board shows no USD and sums tokens over only the last 30 shifts.

### Task 0.1: `budget_ledger` gains `shift_id` + `seconds` + `profile`; store helpers

**Files:**
- Modify: `store/schema.sql:96-103` (budget_ledger DDL)
- Modify: `common/store.py:61-67` (`_migrate`), `common/store.py:216-228` (budget methods)
- Test: `tests/test_store.py` (append)

**Step 1: Write the failing test.** `tests/conftest.py` has NO fixtures today (it is a 9-line `sys.path` shim) — first promote a shared `store` fixture into it (copy the `Blackboard`-on-`tmp_path` fixture from `tests/test_promotion_scoring.py:26`); every test in this plan assumes `def test_x(store):` works suite-wide:

```python
def test_budget_ledger_shift_attribution(store):
    store.add_budget("conductor", 100, 0.01, shift_id=7, seconds=12.5)
    store.add_budget("developer:task-ab", 400, 0.04, shift_id=7, profile="python-dev")
    store.add_budget("researcher", 50, 0.005)              # no shift — old-loop style still works
    spend = store.shift_spend(7)
    assert spend == {"tokens": 500, "cost": 0.05, "seconds": 12.5}
    assert store.shift_spend(99) == {"tokens": 0, "cost": 0.0, "seconds": 0.0}
    row = store._one("SELECT profile FROM budget_ledger WHERE role_or_run='developer:task-ab'")
    assert row["profile"] == "python-dev"
```

**Step 2:** `python3 -m pytest tests/test_store.py -q` → FAIL (unexpected keyword `shift_id`).

**Step 3: Implement.**
- `store/schema.sql`: add to the `budget_ledger` CREATE:
  ```sql
      shift_id    INTEGER,                     -- conductor-loop attribution (NULL for the old loop)
      seconds     REAL NOT NULL DEFAULT 0,     -- wall-clock the spend took (timesheets)
      profile     TEXT NOT NULL DEFAULT ''     -- worker profile that earned the spend (Phase 5)
  ```
- `common/store.py::_migrate` (same guarded pattern as `spec_json`):
  ```python
  bcols = {r[1] for r in self.conn.execute("PRAGMA table_info(budget_ledger)").fetchall()}
  if bcols and "shift_id" not in bcols:
      self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN shift_id INTEGER")
  if bcols and "seconds" not in bcols:
      self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN seconds REAL NOT NULL DEFAULT 0")
  if bcols and "profile" not in bcols:
      self.conn.execute("ALTER TABLE budget_ledger ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
  ```
- `common/store.py::add_budget` — extend (backward-compatible defaults):
  ```python
  def add_budget(self, role_or_run: str, tokens: int, cost: float = 0.0,
                 notes: str = "", shift_id: Optional[int] = None,
                 seconds: float = 0.0, profile: str = "") -> None:
      self._exec(
          "INSERT INTO budget_ledger(at, role_or_run, tokens, cost, notes, shift_id, seconds, profile) "
          "VALUES (?,?,?,?,?,?,?,?)",
          (now_iso(), role_or_run, tokens, cost, notes, shift_id, seconds, profile))
  ```
- New helper next to `budget_totals`:
  ```python
  def shift_spend(self, shift_id: int) -> dict:
      r = self._one(
          "SELECT COALESCE(SUM(tokens),0) AS tokens, COALESCE(SUM(cost),0) AS cost, "
          "COALESCE(SUM(seconds),0) AS seconds FROM budget_ledger WHERE shift_id = ?",
          (shift_id,))
      return r or {"tokens": 0, "cost": 0.0, "seconds": 0.0}
  ```

**Step 4:** tests pass. **Step 5:** Commit: `feat(store): shift-attributed budget ledger (shift_id + seconds + profile)`.

### Task 0.2: `develop_and_merge` carries tokens/cost/seconds on every result path

**Files:**
- Modify: `orchestrator/develop.py:205-273`
- Test: `tests/test_develop_glue.py` (append; reuse its fake-adapter/monkeypatch idiom)

**Step 1: Failing test** — monkeypatch `roles.common.develop_candidate` to return `{"branch": b, "reply": "", "tokens": 1234, "cost": 0.05}`, fake adapter with `branch_exists → False`; assert the `no_candidate` result contains `tokens == 1234`, `cost == 0.05`, `seconds >= 0`. Add a second case (branch exists, `run_code_round` monkeypatched to `{"action": "merged"}`) asserting the merged result carries the same keys.

**Step 3: Implement** in `develop_and_merge` — wrap the developer call and stamp every return after it:

```python
        import time
        t0 = time.monotonic()
        dev = develop_candidate(dev_clone, task=task, branch=branch,
                          test_cmd=" ".join(adapter.test_command()),
                          frozen=adapter.frozen_paths(), as_user=as_user,
                          claude_bin=claude_bin, memory=memory)
        spend = {"tokens": int(dev.get("tokens") or 0), "cost": float(dev.get("cost") or 0.0),
                 "seconds": round(time.monotonic() - t0, 1)}
```

then add `**spend` (or `res.update(spend)`) to the three `no_candidate` returns (`develop.py:242,246,248`) and to `res` before `return res` (`develop.py:266`). The pre-`dev` returns (`halted` at `:216`, and the chown failure at `:225-230` — note its shape is `action="discarded", stage="chown"`, not `action="chown"`) carry no spend — correct, nothing was spent.

**Step 5:** Commit: `fix(develop): stop dropping developer tokens/cost — thread spend through every result`.

### Task 0.3: the rail ledgers every worker verdict

**Files:**
- Modify: `orchestrator/develop.py:154-202` (`execute_claimed_tasks` close-out loop)
- Test: `tests/test_develop_glue.py` (it already drives `execute_claimed_tasks` with stub `develop_fn`s; `test_target_dev.py` does NOT — it covers `run_tests`/clone only)

**Step 1: Failing test** — drive `execute_claimed_tasks` with a `develop_fn` stub returning `{"action": "merged", "merge_sha": "abc", "tokens": 500, "cost": 0.02, "seconds": 3.0}`; assert a `budget_ledger` row exists with `role_or_run == f"developer:{task_id}"`, `shift_id == sid`, tokens 500. Add a `no_candidate` stub case (also ledgered) and a `halted` case (NOT ledgered).

**Step 3: Implement** — in the close-out loop (`develop.py:155`, main thread, after the learnings block):

```python
        if action != "halted":                     # a halted dispatch never ran — nothing spent
            store.add_budget(f"developer:{task['id']}", int(res.get("tokens") or 0),
                             float(res.get("cost") or 0.0), notes=action or "",
                             shift_id=shift_id, seconds=float(res.get("seconds") or 0.0))
```

(Leave `profile` at its `""` default here — Task 5.4 starts passing the real name.)

**Step 5:** Commit: `feat(rail): ledger every developer dispatch (tokens/cost/seconds per task, per shift)`.

### Task 0.4: the conductor ledgers itself (and stops discarding cost)

**Files:**
- Modify: `roles/conductor.py:59-91` (`run_conductor`)
- Test: `tests/test_conductor.py` (append; it already monkeypatches `common.claude_super`)

**Step 1: Failing test** — monkeypatch `claude_super` to return `("{\"status\":\"completed\"}", 777, 0.03)`; run `run_conductor`; assert a ledger row `role_or_run == "conductor"`, tokens 777, cost 0.03, `shift_id` stamped.

**Step 3: Implement** — `run_conductor` already receives `store` and `shift_id`. Capture the cost (currently `_cost`, discarded at `conductor.py:67`), wrap the `claude_super` call in the same `time.monotonic()` timing as Task 0.2 (otherwise every conductor engagement shows as a 0-minute timesheet row forever), and, just before both returns:

```python
    store.add_budget("conductor", tokens, cost, notes="shift lead",
                     shift_id=shift_id, seconds=round(time.monotonic() - t0, 1))
```

(One call placed after the `claude_super` return, before the sentinel branch, covers both paths.)

**Step 5:** Commit: `feat(conductor): record own tokens/cost in the budget ledger`.

### Task 0.5: ledger the auxiliary roles (scope judge, decomposer, research refill)

**Files:**
- Modify: `reporting/scope_check.py` (`scope_judge`, `decompose_judge`), `roles/research_feed.py` (`propose_directions`)
- Test: `tests/test_scope_check.py`, `tests/test_research_feed.py` (append)

**Step 0 (investigate first):** read each function and confirm where the `(reply, tokens, cost)` triple from `claude_super` is available and whether a `store` handle is in scope. `propose_directions(st, ...)` has the store; the two judges are called as closures from `cmd_run` (`orchestrator/orchestrator.py:760-765`) *without* a store — for those, return `tokens`/`cost` up in their result dicts and ledger from the call sites that DO have the store (`scope_check.prefilter` and `scope_check.decompose_no_candidate` both receive `store` and `shift_id`). Also verify `shift_id` is actually in scope where `propose_directions` is invoked (its signature is `propose_directions(store, *, limit=5, …)` — if it is not, thread it through or ledger with `shift_id=None` rather than inventing one).

**Roles to ledger:** `role_or_run` = `"scope_check"`, `"decompose"`, `"researcher"` — with `shift_id` where available. Test-first per function, same monkeypatch style as the existing tests in those files.

**Step 5:** Commit: `feat(accounting): ledger scope-check, decompose and research-refill spend`.

### Task 0.6: `shifts.tokens_used` becomes the full shift spend

**Files:**
- Modify: `orchestrator/shift.py:70-79`
- Test: `tests/test_shift_harness.py` (append)

**Step 1: Failing test** — run `run_shift` with a stub conductor returning `tokens_used=100` and a stub executor that ledgers `store.add_budget("developer:t1", 400, shift_id=<sid>)`; assert the closed shift row has `tokens_used == 500` (conductor row must be ledgered by the stub too, or assert `>= 400 + outcome` per your wiring — mirror the real flow: conductor ledgers itself as of Task 0.4, so the harness should trust the ledger).

**Step 3: Implement** — in `run_shift`, before `end_shift`:

```python
    ledgered = store.shift_spend(sh)["tokens"]
    tokens_total = max(int(outcome.get("tokens_used", 0)), int(ledgered))
    store.end_shift(sh, status=status, report=outcome.get("report", ""),
                    resume_note=outcome.get("resume_note", ""),
                    tokens_used=tokens_total)
    return {"action": status, "shift_id": sh, "reaped": len(reaped), "shipped": shipped,
            "tokens_used": tokens_total}
```

(`max()` keeps the old behavior when nothing is ledgered — e.g. hermetic tests — and the honest total when it is. **Note the intended behavior change:** `cmd_run_loop`'s cumulative `loop_token_budget` ceiling now counts worker spend too, so the brake trips sooner. That is the point; mention it in the commit message.)

**Step 5:** Commit: `feat(shift): tokens_used = conductor + workers via the ledger (loop brake now sees real spend)`.

### Task 0.7: fleet board — cost KPI, per-shift cost, accurate totals

**Problem detail:** `fleet_json` (`reporting/fleet_viz.py:132-228`) computes `kpi.shifts = len(shifts)` and `total_tokens` from `list_shifts(limit=30)` — both wrong past 30 shifts (live count is 91).

**Files:**
- Modify: `common/store.py` (one helper), `reporting/fleet_viz.py:158-176,221-224`, `dashboard/static/fleet.html` (KPI strip `:248-255`, shifts panel `:288`)
- Test: `tests/test_fleet_viz.py` (append)

**Step 1: Failing test** — seed a store with 2 shifts + ledger rows (`shift_spend` totals + one USD cost); assert `fleet_json(store)["kpi"]["total_cost_usd"] == pytest.approx(...)`, `kpi["shifts"] == <true count>`, and each entry in `["shifts"]` carries a `cost` key.

**Step 3: Implement.**
- `common/store.py`: `def count_shifts(self) -> int: return self._one("SELECT COUNT(*) AS n FROM shifts")["n"]`
- `fleet_viz.fleet_json`:
  ```python
  totals = store.budget_totals()                      # ledger-wide (now includes the new loop)
  spend_by_shift = {s["id"]: store.shift_spend(s["id"]) for s in shifts}
  kpi["shifts"] = store.count_shifts()
  kpi["total_tokens"] = max(total_tokens, int(totals["tokens"]))
  kpi["total_cost_usd"] = round(float(totals["cost"]), 2)
  ```
  and add `"cost": round(spend_by_shift[s["id"]]["cost"], 2)` to each dict in the `"shifts"` list comprehension.
- `fleet.html`: add a `💵 Cost` tile to the KPI strip rendering `kpi.total_cost_usd` (follow the existing tile markup exactly), and a cost column in the shifts panel renderer.

**Step 4:** run the JS check (Verification section) + `python3 -m pytest tests/test_fleet_viz.py -q`.

**Step 5:** Commit: `feat(fleet): USD cost KPI + per-shift cost; true shift/token totals beyond the last 30`.

---

## Phase 1 — Mission steering: MISSION.md drives the live loop + a mission lever on the board

**Problem:** the conductor loop reads `store.active_mission()` only; MISSION.md steers just the legacy `daily`/research paths. The dashboard displays the mission but can't change it.

### Task 1.1: sync MISSION.md → store mission at `cmd_run` start

**Files:**
- Create: nothing — extend `orchestrator/orchestrator.py::cmd_run` (`:718`)
- Reuse: `research/focus.py::read_mission` (already parses the `## Mission` section — verify its exact return; if it returns the whole file, add a small section-extractor there rather than a new module)
- Test: `tests/test_run_cli.py` (append)

**Step 1: Failing test** — point `MISSION.md` (tmp `FACTORY_ROOT` per the conftest idiom) at text A, store mission at text B; call `cmd_run` with stub conductor/executor/refill; assert `store.active_mission()["statement"]` == A. Second case: call `cmd_run` again with the file unchanged → `set_mission` NOT called again (the sync compares normalized file text against the ACTIVE statement, so an unchanged file can never re-steer; no new mission row, mission id stable).

**Step 3: Implement** — at the top of `cmd_run`, before the idle check:

```python
    if mission is None:                       # an explicit --mission always wins
        file_mission = _read_mission_md()     # research/focus.read_mission, normalized/stripped
        active = store.active_mission()
        if file_mission and (not active or file_mission != active["statement"].strip()):
            store.set_mission(file_mission)
            print(f"[run] mission re-steered from MISSION.md: {file_mission[:80]}…")
```

Normalization: compare `" ".join(text.split())` on both sides so whitespace edits don't re-steer.

**Semantics — the file wins at every run start (by design):** any mission set via `store.set_mission` between runs is overwritten at the next `cmd_run` start whenever MISSION.md differs; the human's file is the steering wheel. Note `--mission` is weaker than it looks today: `run_shift` only applies it when NO active mission exists (`shift.py:31`) — keep that guard, and make the flag durable by also routing it through Task 1.2's `## Mission` rewrite helper (wired there, where the helper lands) so a CLI steer reaches the file and survives the next sync.

**Step 5:** Commit: `feat(mission): MISSION.md steers the live conductor loop (synced at run start)`.

### Task 1.2: mission editor on the fleet board

**Files:**
- Modify: `dashboard/fleet_server.py:64-91` (add `/api/mission` to the POST allowlist), `dashboard/static/fleet.html` (header mission block `:142` area)
- Test: `tests/test_fleet_viz.py` or a new `tests/test_fleet_server.py` (the handler methods are testable by calling them on a stub; follow `tests/test_autopilot.py`'s style for anything process-shaped)

**Step 3: Implement** — POST `/api/mission` `{statement}`: CSRF-guarded (`_local_origin`), rejects empty/`>2000` chars, then (a) rewrites the `## Mission` section body of `MISSION.md` (small helper; keep the rest of the file byte-identical) and (b) `store.set_mission(statement)`. Front end: an ✏️ button next to the mission text opens an inline `<textarea>` + Save. **Do not use `window.prompt`** — keep it an inline form. fleet.html runs one unconditional `setInterval(tick,2000)` (`:308`) and has NO existing pause pattern to copy (the mode toggle just POSTs and re-ticks) — build the pause here: a module-level `editing` flag that `tick()` checks, set while the textarea is open. Server-side, follow ground rule 2's dashboard clause (fresh per-request `Blackboard`, busy → 503). Also route the `--mission` CLI path through the new `## Mission` rewrite helper (see Task 1.1 semantics) so a CLI steer survives the next file sync.

**Step 5:** Commit: `feat(board): mission editor — the human steer lives on the dashboard`.

### Task 1.3: "Material from the human" feeds the live researcher

**Files:**
- Modify: `roles/research_feed.py::propose_directions` (prompt assembly, `:59` area)
- Reuse: the MISSION.md material parser in `research/ingest.py` / `research/focus.py` (verify which function extracts the `## Material from the human` bullet list)
- Test: `tests/test_research_feed.py` (append: material lines appear in the prompt handed to the monkeypatched `claude_super`)

**Step 3:** append a `## Material from the human` block to the researcher prompt when the file section is non-empty. **Step 5:** Commit: `feat(research): human-dropped material reaches the live refill researcher`.

---

## Phase 2 — The plan: milestones, deliverables & effort estimates (conductor-planned, revised every shift)

The plan is a **loop, not a document**: the conductor re-reads and revises it at every shift (milestones re-ordered/dropped, estimates corrected, profiles re-assigned) — the contract in Task 2.3 makes that explicit.

### Task 2.1: schema + store CRUD

**Files:**
- Modify: `store/schema.sql` (append after `learnings`), `common/store.py` (`_migrate` + a new section)
- Test: `tests/test_store.py` (append)

**Step 1: Failing test:**

```python
def test_milestones_crud_and_progress(store):
    mid = store.add_milestone("M1: reliable recovery", mission_id=1,
                              deliverable="recovery corpus green under eval",
                              acceptance="all recovery scenarios pass 3x",
                              budget_tokens=800_000, planned_order=1)
    store.add_task("task-x", "slice 1", source="research")
    store.set_task_milestone("task-x", mid)
    store.set_task_status("task-x", "done", result="abc123")
    ms = store.list_milestones()
    assert ms[0]["status"] == "planned" and ms[0]["budget_tokens"] == 800_000
    assert store.milestone_progress(mid) == {"done": 1, "total": 1}
    store.set_milestone_status(mid, "delivered")
    assert store.list_milestones(status="delivered")[0]["id"] == mid
```

**Step 3: Implement.**
- `store/schema.sql`:
  ```sql
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
  ```
  plus `tasks.milestone_id INTEGER REFERENCES milestones(id)` — in the CREATE **and** as a `_migrate` ALTER (`ALTER TABLE tasks ADD COLUMN milestone_id INTEGER`), with `CREATE INDEX IF NOT EXISTS idx_tasks_milestone ON tasks(milestone_id)`.
- `common/store.py`: `add_milestone`, `list_milestones(status=None, mission_id=None)` ordered by `planned_order, id`, `set_milestone_status` (stamps `delivered_at` when status=='delivered'), `set_task_milestone(task_id, milestone_id)`, and:
  ```python
  def milestone_progress(self, milestone_id: int) -> dict:
      r = self._one(
          "SELECT COUNT(*) AS total, "
          "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
          "FROM tasks WHERE milestone_id = ?", (milestone_id,))
      return {"done": int(r["done"] or 0), "total": int(r["total"] or 0)}
  ```

**Step 5:** Commit: `feat(store): milestones/deliverables — the persisted plan entity`.

### Task 2.2: task-level effort + assignment columns

**Files:**
- Modify: `store/schema.sql` (tasks CREATE), `common/store.py` (`_migrate`, `set_task_estimate`, `set_task_profile`)
- Test: `tests/test_store.py` (append)

**Step 1: Failing test** — `store.set_task_estimate("task-x", 60_000)`, `store.set_task_profile("task-x", "python-dev")`; assert `get_task` returns both.

**Step 3: Implement** — two new `tasks` columns (CREATE + guarded ALTERs, exactly like `spec_json`):
```sql
    est_tokens INTEGER NOT NULL DEFAULT 0,   -- conductor's effort estimate (EVM task-level PV)
    profile    TEXT NOT NULL DEFAULT ''      -- worker_profiles.name to dispatch with ('' = generalist)
```
plus the two setters (same shape as `set_task_spec`). Consumption comes in Phases 4 (EVM) and 5 (dispatch).

**Step 5:** Commit: `feat(store): per-task effort estimate + worker-profile assignment columns`.

### Task 2.3: `factory plan` CLI (the conductor's lever)

**Files:**
- Modify: `orchestrator/orchestrator.py` — new `cmd_plan` next to `cmd_task` (`:691`), argparse registration (`:1204-1309`) + dispatch (`:1336-1409`)
- Test: `tests/test_run_cli.py` (append — follow the existing `cmd_task` test)

Subcommands (mirror `cmd_task`'s shape exactly):
```
factory plan add "<title>" [--deliverable D] [--acceptance A] [--budget-tokens N] [--order N]
factory plan list [--status planned|active|delivered|dropped]
factory plan status <id> <planned|active|delivered|dropped>
factory plan link <task-id> <milestone-id>
factory plan estimate <task-id> <est-tokens> [--profile NAME]
```
`plan list` prints progress via `milestone_progress`. **Full-id discipline:** like `task claim`, `plan link`/`plan estimate` must match the task id exactly and print how many rows changed (the silent-no-op bug class is known from `task claim`, but `tests/test_run_cli.py` has NO such regression test yet — write the first one here: partial id → error + "0 rows", full id → "1 row").

**Step 5:** Commit: `feat(cli): factory plan — add/list/status/link/estimate`.

### Task 2.4: the conductor maintains and *revises* the plan

**Files:**
- Modify: `roles/conductor/prompt.md`, `roles/conductor.py::build_conductor_prompt` (`:33-56`)
- Test: `tests/test_conductor.py` (append: `{PLAN}` is filled; prompt mentions the plan CLI)

**Step 3:** add a `{PLAN}` placeholder to the prompt template — rendered from `store.list_milestones()` with per-milestone progress + linked-task estimates vs actuals (`_bullets` helper, same as `{BACKLOG}`), `"(no plan yet — draft 2-4 milestones with `./bin/factory plan add …`)"` when empty. Prompt contract additions (keep the conductor's existing voice/format):
- maintain 2–5 milestones toward the mission; each has a deliverable + acceptance + a token budget;
- **estimate every task you claim** (`plan estimate <task-id> <tokens> --profile <name>`) and link it to a milestone (`plan link`);
- **revise the plan every shift**: correct estimates that proved wrong (the timesheet shows actuals), re-order or drop milestones invalidated by research/blocks — say why in the report;
- mark a milestone `delivered` only when its acceptance is verifiably met (cite evidence in the shift report).

**Step 5:** Commit: `feat(conductor): the shift lead plans, estimates, assigns — and revises the plan each shift`.

### Task 2.5: plan data on the board (read-only for now; the Plan tab UI lands in Phase 7)

**Files:**
- Modify: `reporting/fleet_viz.py::fleet_json`, `dashboard/fleet_server.py` (GET `/api/plan`)
- Test: `tests/test_fleet_viz.py`

`fleet_json` gains `"plan": [{id, title, status, deliverable, order, budget_tokens, progress: {done,total}}]`; `/api/plan` serves the same list standalone (the tab will poll it lazily). **Step 5:** Commit: `feat(board): plan (milestones) in the fleet state`.

---

## Phase 3 — Timesheets (agent-adapted)

A timesheet row = one agent engagement: who (role / `developer:<task>`), when (`at`), how long (`seconds`), spend (tokens, cost), on what (task title), verdict (`notes` = merged/no_candidate/…), in which shift. All of that exists in the ledger after Phase 0.

### Task 3.1: `reporting/timesheets.py` (pure gather)

**Files:**
- Create: `reporting/timesheets.py`
- Test: `tests/test_timesheets.py` (new; copy the store fixture from `tests/test_fleet_viz.py`)

**Step 1: Failing test** — seed shifts + ledger rows incl. one `developer:task-a` row and the matching task; assert `timesheet(store)` returns newest-first rows with `{shift, agent, role, task_title, at, seconds, tokens, cost, profile, verdict}` and that `by_agent(store)` aggregates totals per role.

**Step 3: Implement:**

```python
"""Agent timesheets — who worked when, for how long, at what spend, to what verdict.
Pure reads over budget_ledger (+ tasks for titles). The rail writes the rows (Phase 0);
this module only shapes them for the CLI, the board and EVM."""

def timesheet(store, limit: int = 200) -> list[dict]:
    rows = store._all(
        "SELECT b.at, b.role_or_run, b.tokens, b.cost, b.seconds, b.notes, b.shift_id, b.profile "
        "FROM budget_ledger b WHERE b.shift_id IS NOT NULL "
        "ORDER BY b.at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        role, _, ref = r["role_or_run"].partition(":")
        task = store.get_task(ref) if role == "developer" and ref else None
        out.append({"shift": r["shift_id"], "agent": r["role_or_run"], "role": role,
                    "task_title": (task or {}).get("title", ""), "at": r["at"],
                    "seconds": r["seconds"], "tokens": r["tokens"], "cost": r["cost"],
                    "profile": r["profile"], "verdict": r["notes"]})
    return out

def by_agent(store) -> list[dict]:
    return store._all(
        "SELECT CASE WHEN instr(role_or_run,':')>0 "
        "         THEN substr(role_or_run,1,instr(role_or_run,':')-1) "
        "         ELSE role_or_run END AS role, "
        "COUNT(*) AS engagements, COALESCE(SUM(tokens),0) AS tokens, "
        "COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(seconds),0) AS seconds "
        "FROM budget_ledger GROUP BY role ORDER BY tokens DESC")
```

(If reaching into `store._all` from reporting feels off, add thin `Blackboard.ledger_rows()`/`ledger_by_role()` wrappers instead — match whichever pattern `reporting/fleet_viz.py` ends up using; do not duplicate SQL in two modules.)

Scope note: `timesheet()` filters `shift_id IS NOT NULL` (conductor-loop engagements only) while `by_agent()` rolls up the WHOLE ledger including legacy old-loop rows. That asymmetry is intended, but label the rollup "all-time (incl. legacy)" wherever it renders so the two views don't look contradictory.

**Step 5:** Commit: `feat(timesheets): agent timesheet + per-role rollup (pure gather)`.

### Task 3.2: surface — CLI + endpoint

**Files:**
- Modify: `orchestrator/orchestrator.py` (`cmd_timesheet`, argparse `factory timesheet [--shift N] [--limit N]`), `dashboard/fleet_server.py` (GET `/api/timesheets`)
- Test: `tests/test_run_cli.py`, `tests/test_timesheets.py`

CLI prints an aligned table (shift | agent | task | min | tokens | $ | verdict) + the `by_agent` rollup. Endpoint returns `{"rows": timesheet(store), "by_agent": by_agent(store)}`. **Step 5:** Commit: `feat(timesheets): CLI + /api/timesheets`.

---

## Phase 4 — EVM adapted to agents

**Mapping (state it verbatim in the module docstring so the semantics are inspectable):**
- **Value unit = planned tokens.** A milestone's `budget_tokens` is its Planned Value; task-level `est_tokens` (Task 2.2) refines it where present.
- **PV** = Σ `budget_tokens` over non-dropped milestones (the baseline).
- **EV** = Σ `budget_tokens` of `delivered` milestones + partial credit for `active` ones. Partial credit uses `est_tokens` weighting when the linked tasks carry estimates (`budget × Σest(done)/Σest(all)`), else plain `done/total`.
- **AC** = actual tokens (and USD) attributed to each milestone via `tasks.milestone_id` → ledger `developer:<task_id>` rows; unattributed spend (conductor, research) is reported as **overhead**, not smeared across milestones.
- **SPI = EV/PV-consumed-so-far is not computable without a time-phased baseline** — v1 reports **CPI = EV/AC**, percent-complete (EV/PV), and an AC-per-shift cumulative series. A time-phased PV (planned shift ranges per milestone) is a deliberate v2; do not fake it.
- **Estimate quality is a first-class output:** per-task `est_tokens` vs ledgered actuals (the conductor's feedback loop for revising the plan — Task 2.4).

### Task 4.1: `reporting/evm.py`

**Files:**
- Create: `reporting/evm.py`
- Test: `tests/test_evm.py` (new)

**Step 1: Failing test** — seed: milestone A (budget 100k, delivered, 1 linked done task with a 40k ledger row), milestone B (budget 200k, active, 1 of 2 linked tasks done, 30k ledgered), conductor overhead 10k. Assert `evm(store)` returns `pv == 300_000`, `ev == 200_000` (100k + 200k·½), `ac_tokens == 70_000`, `overhead_tokens == 10_000`, `cpi == pytest.approx(200/70, rel=1e-3)`, and a `milestones` list with per-row pv/ev/ac. Add a case with `est_tokens` set asserting the est-weighted partial credit.

**Step 3: Implement** `evm(store) -> dict` per the mapping (SQL for AC:
`SELECT COALESCE(SUM(b.tokens),0), COALESCE(SUM(b.cost),0) FROM budget_ledger b JOIN tasks t ON b.role_or_run = 'developer:' || t.id WHERE t.milestone_id = ?`). Include `series: {shift_ids, ac_cumulative}` from `shift_spend` over `list_shifts`, and `estimates: [{task, est, actual}]` for tasks with both.

**Step 5:** Commit: `feat(evm): agent-adapted earned value (PV/EV/AC/CPI) over milestones + ledger`.

### Task 4.2: surface — CLI + endpoint (+ Plan-tab chart in Phase 7)

`factory evm` prints the totals + per-milestone table + the est-vs-actual list; GET `/api/evm` serves `evm(store)`. Tests in `tests/test_run_cli.py`. Commit: `feat(evm): CLI + /api/evm`.

---

## Phase 5 — Worker capability profiles, model routing, on-demand generation

**Problem:** every developer dispatch today is the same generalist prompt on the operator's default model. The wishlist wants *specialists* (Python developer, ML expert, LLM-prompt pro, test engineer, …) on *right-sized models*, assigned by the conductor during planning, and **generatable on demand** — the workforce itself evolves.

**Design (the invariants):**
- A **profile is data, not code**: `{name, description, model tier, persona overlay}`. The rail instantiates a worker from it by injecting the overlay at a `{PROFILE}` seam and passing the resolved model to the transport. Profiles can therefore be created/retired by the conductor at runtime with zero code change.
- **The bench is a function of the target.** The stack-specialist seeds are *derived from the target repo* (Task 5.2), not hardcoded: point the factory at a Python repo and it staffs `python-dev`; re-point it at a TypeScript repo and `ts-dev` joins the bench automatically. Domain specialists (`ml-expert`, `prompt-pro`, …) are NOT seeded deterministically — inferring domain from files is guesswork; generating those is the conductor's job (Task 5.6), informed by the mission and the work itself.
- A profile **cannot** change the toolset, the sandbox boundary, the frozen surface, or the gates — model + persona only. Capability stays fixed by the rail; only *style and model* vary. This keeps profile generation safe enough to hand to the conductor.
- **Model whitelist:** profiles name a **tier alias** (`frontier|standard|fast`), never a raw model id. `config.yaml` maps aliases to ids; an unknown/unresolvable alias fails open to `standard` with a printed warning (fail DOWNWARD — a typo must never silently upgrade a worker to the frontier tier; fall back to `''` = the account default only if `standard` itself is unmapped) — a bad profile can never brick dispatch.
- **Frontier is for judgment:** the conductor, scope/decompose judges and reviewer stay on the default (frontier) model; developer profiles default to `standard`. A super-worker may internally fan out to cheaper models via its own Task/Workflow tools — the overlay text should encourage that for mechanical subtasks — but its primary model is rail-assigned.

### Task 5.1: `worker_profiles` table + store CRUD + seeds

**Files:**
- Modify: `store/schema.sql`, `common/store.py` (`_migrate` not needed — new table; CRUD section)
- Test: `tests/test_store.py` (append)

**Step 1: Failing test:**

```python
def test_worker_profiles_crud(store):
    store.add_profile("python-dev", description="Python/pytest specialist for the clive codebase",
                      model="standard", overlay="You are a senior Python engineer…")
    store.add_profile("prompt-pro", description="LLM prompt engineering", model="frontier",
                      overlay="…", created_by="conductor")
    assert {p["name"] for p in store.list_profiles()} == {"python-dev", "prompt-pro"}
    store.retire_profile("prompt-pro")
    assert [p["name"] for p in store.list_profiles(active_only=True)] == ["python-dev"]
    assert store.get_profile("python-dev")["model"] == "standard"
```

**Step 3: Implement.**
- `store/schema.sql`:
  ```sql
  -- Worker capability profiles: the conductor's on-demand workforce. A profile is DATA
  -- (persona overlay + model tier) the rail instantiates per task — it can never change
  -- the toolset, sandbox boundary, frozen surface or gates (those stay rail-fixed).
  CREATE TABLE IF NOT EXISTS worker_profiles (
      name        TEXT PRIMARY KEY,          -- slug: 'python-dev', 'ml-expert', 'prompt-pro'
      description TEXT NOT NULL,             -- capabilities, for the conductor + the board
      model       TEXT NOT NULL DEFAULT '',  -- tier alias: ''|'frontier'|'standard'|'fast'
      overlay     TEXT NOT NULL DEFAULT '',  -- persona/emphasis block injected at {PROFILE}
      active      INTEGER NOT NULL DEFAULT 1,
      created_by  TEXT NOT NULL DEFAULT 'operator',
      created_at  TEXT NOT NULL
  );
  ```
- `common/store.py`: `add_profile` (validate slug `^[a-z0-9][a-z0-9-]{1,31}$`; give it a `replace: bool = True` switch — INSERT OR REPLACE for the CLI/board path, INSERT OR IGNORE when `replace=False`; the seeder MUST use `replace=False` so a re-seed can never clobber a conductor-tuned overlay or resurrect a retired profile), `get_profile`, `list_profiles(active_only=False)`, `retire_profile` (sets `active=0` — never DELETE, timesheet history references names).
- Seeding is **not** hardcoded here — it is target-derived (Task 5.2). This task only guarantees `generalist` exists (model `''`, empty overlay — the exact current dispatch behavior), created lazily by `get_profile("")`/`get_profile("generalist")` fallback or a one-line ensure in `init_db`.

**Step 5:** Commit: `feat(store): worker capability profiles (data-only, retire-not-delete)`.

### Task 5.2: target-derived workforce seeding (the bench follows the target)

The stack specialists a target needs are a property of the *target*, not of the factory. Derive them deterministically from the target's manifests, and re-derive whenever the factory is re-pointed — so switching targets automatically staffs the right bench without deleting anything the conductor built.

**Files:**
- Create: `reporting/staffing.py` (matches the `factory_memory`/`scope_check` precedent: rail-invoked store logic lives in `reporting/`)
- Modify: `orchestrator/orchestrator.py::cmd_run` (one call, next to the Task 1.1 mission sync)
- Test: `tests/test_staffing.py` (new)

**Step 1: Failing test:**

```python
def test_seed_profiles_follow_the_target(store, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    added = staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo")
    names = {p["name"] for p in store.list_profiles(active_only=True)}
    assert {"generalist", "python-dev", "test-engineer", "docs-writer"} <= names
    assert "ts-dev" not in names
    assert staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo") == []   # idempotent

def test_repoint_adds_missing_stack_never_retires(store, tmp_path):
    staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo")   # py target first
    ts = tmp_path / "ts"; ts.mkdir()
    (ts / "package.json").write_text("{}"); (ts / "tsconfig.json").write_text("{}")
    staffing.ensure_seeded(store, str(ts), "acme/ts-repo")         # re-point
    names = {p["name"] for p in store.list_profiles(active_only=True)}
    assert "ts-dev" in names and "python-dev" in names             # additive, not destructive

def test_reseed_never_resurrects_or_clobbers(store, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo")
    store.retire_profile("python-dev")                              # the conductor's call
    staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo-2")  # slug change forces a re-run
    assert "python-dev" not in {p["name"] for p in store.list_profiles(active_only=True)}
```

**Step 3: Implement** `reporting/staffing.py`:

```python
"""Target-derived workforce seeding. The bench is a function of the TARGET: stack
specialists are detected from the target's manifests (deterministic — no LLM in the
seeding path) and re-derived whenever the factory is re-pointed. Additive only: a
re-point adds missing stack profiles; retiring stale ones is the CONDUCTOR's call
(it has the outcome data). Domain specialists (ml-expert, prompt-pro, …) are never
seeded — the conductor generates those on demand (factory worker add)."""

_STACK_MARKERS = {          # marker file present at target root → seed this profile
    "python-dev":  ("pyproject.toml", "setup.py", "requirements.txt"),
    "ts-dev":      ("tsconfig.json",),
    "node-dev":    ("package.json",),       # superseded by ts-dev when both match
    "rust-dev":    ("Cargo.toml",),
    "go-dev":      ("go.mod",),
}
_UNIVERSAL = ("generalist", "test-engineer", "docs-writer")   # every target gets these

def detect_stacks(root: str) -> list[str]: ...   # marker scan; ts-dev suppresses node-dev

def ensure_seeded(store, target_root: str, target_slug: str) -> list[str]:
    """Idempotent; returns the profile names newly added. Guarded by the settings key
    'staffing.seeded_for' (Phase 6.1 table) — re-runs only when the slug changes or
    the profile table is missing a detected stack. 'Missing' is checked against
    list_profiles(active_only=False): a RETIRED profile counts as PRESENT (the
    conductor retired it deliberately — never resurrect it). Inserts go through
    add_profile(replace=False) so a re-seed can't clobber tuned overlays.
    Overlays come from a small
    _OVERLAYS dict in this module (one tight persona paragraph per profile,
    model tier 'standard' for stack profiles, '' for generalist)."""
```

Wire into `cmd_run` right after the mission sync (the adapter already knows the target root — `config.get_adapter().entry()[0]`):

```python
    from ..reporting import staffing
    added = staffing.ensure_seeded(store, config.get_adapter().entry()[0],
                                   config.target_repo_slug())
    if added:
        print(f"[run] staffing: seeded {', '.join(added)} for this target")
```

**Sequencing note:** `ensure_seeded` uses the `settings` table from Phase 6.1 for its guard key. Either pull Task 6.1's schema+CRUD forward when executing this task, or (simpler) store the guard under a dedicated one-column read in `worker_profiles`-adjacent code — pick pulling 6.1's table forward; it has no other dependencies.

**Step 5:** Commit: `feat(staffing): target-derived workforce seeding — the bench follows the target`.

### Task 5.3: model-tier whitelist in config + transport support

**Files:**
- Modify: `config.yaml` (new `models:` block), `common/config.py` (accessor), `roles/common.py` (`_super_worker_argv` + `claude_super` gain `model: str = ""`)
- Test: `tests/test_super_worker.py` (append — argv assembly is already tested there)

**Step 1: Failing test** — `_super_worker_argv(..., model="claude-sonnet-4-6")` includes `["--model", "claude-sonnet-4-6"]`; `model=""` adds nothing; `claude_super(..., model=…)` threads it through (assert via the argv builder monkeypatch idiom used in that file).

**Step 3: Implement.**
- `config.yaml`:
  ```yaml
  # --- model tiers (worker routing) -------------------------------------------
  # Profiles name a TIER, never a raw model id. '' = the account's default model
  # (frontier — reserved for judgment: conductor, judges, reviewer). Aliases must be
  # models the WORKER account (Guest-House user's subscription) can actually run;
  # an unresolvable alias fails open to standard with a warning ('' only if
  # standard itself is unmapped — never silently up to frontier).
  models:
    frontier: ""
    standard: "claude-sonnet-4-6"
    fast: "claude-haiku-4-5"
  ```
- `common/config.py`: `def resolve_model(tier: str) -> str` — `''`/`'frontier'` → `""`; known alias → id; unknown → the `standard` id + `print` warning (fail open DOWNWARD, never silently up to frontier; return `""` only if `standard` itself is unmapped).
- `roles/common.py`: `claude_super(..., model: str = "")` appends `["--model", model]` in `_super_worker_argv` when non-empty. (The `claude` CLI accepts `--model`; the sentinel-on-failure path already covers a rejected flag.)

**Step 5:** Commit: `feat(transport): whitelisted model-tier routing for super-workers`.

### Task 5.4: the rail dispatches by profile

**Files:**
- Modify: `roles/developer/prompt.md` (add a `{PROFILE}` line near the top), `roles/common.py::develop_candidate` (`:258-283`), `orchestrator/develop.py` (`develop_task`/`execute_claimed_tasks` threading)
- Test: `tests/test_developer.py` + `tests/test_develop_glue.py` (append)

**Step 1: Failing test** — task row with `profile="python-dev"`; profile in store with overlay "PERSONA-MARKER" and model `standard`; drive `execute_claimed_tasks` with the transport monkeypatched; assert the developer prompt contains "PERSONA-MARKER" and `claude_super` received the resolved standard model id. Second case: `profile=""` → empty overlay + default model; unknown profile name → empty overlay, the `standard` model id (fail-open downward) and a printed warning.

**Step 3: Implement.**
- `develop_candidate(..., profile_overlay: str = "", model: str = "")` — `.replace("{PROFILE}", profile_overlay)` (template default: `"(generalist)"`), pass `model` to `claude_super`.
- `execute_claimed_tasks`: resolve once per task **on the main thread** before dispatch (profiles are store reads):
  ```python
  prof = store.get_profile(task.get("profile") or "") or {}
  overlay, model = prof.get("overlay", ""), config.resolve_model(prof.get("model", ""))
  ```
  and thread both through `run(...)` → `develop_task` → `develop_and_merge` → `develop_candidate` (same pattern as `memory=`). Ledger attribution: pass `profile=prof.get("name", "generalist")` into the Task 0.3 ledger call (the `budget_ledger.profile` column landed in Task 0.1) — keep `notes` as the clean verdict; never smuggle structured data into it.

**Step 5:** Commit: `feat(rail): per-task worker instantiation from capability profiles (overlay + model)`.

### Task 5.5: `factory worker` CLI — on-demand generation (the conductor's lever)

**Files:**
- Modify: `orchestrator/orchestrator.py` (`cmd_worker` + argparse/dispatch)
- Test: `tests/test_run_cli.py` (append)

```
factory worker list                       # profiles + active flag + per-profile spend/outcomes
factory worker add <name> --description D --overlay O [--model frontier|standard|fast]
factory worker retire <name>
```

Guardrails (test each): slug validation; `--model` must be a known tier (else exit 2, print the whitelist); **cap active profiles** at `super_worker.max_profiles` (config, default 12) — `add` beyond the cap fails with "retire one first" (prevents profile sprawl); `generalist` cannot be retired (the fail-open target must exist); overlay capped at 2000 chars (same bound as the mission editor — enforce it in the shared validation function so board POSTs inherit it).

**Step 5:** Commit: `feat(cli): factory worker — generate/retire capability profiles on demand`.

### Task 5.6: the conductor staffs the shift

**Files:**
- Modify: `roles/conductor/prompt.md`, `roles/conductor.py::build_conductor_prompt`
- Test: `tests/test_conductor.py` (append: `{WORKERS}` filled)

**Step 3:** add a `{WORKERS}` placeholder rendered from `store.list_profiles(active_only=True)` + per-profile outcome stats (from the timesheets rollup: engagements, merged-rate, tokens). Contract additions:
- when you claim a task, assign the best-fitting profile (`plan estimate <task-id> <tokens> --profile <name>`); default to `generalist` when unsure;
- if no profile fits a task's domain, **generate one** (`factory worker add …`) with a tight one-paragraph overlay — persona and emphasis only, never instructions to bypass tests/gates;
- retire profiles that repeatedly under-perform (low merge rate, est blowouts) — the timesheet is your evidence; say so in the report.

**Step 5:** Commit: `feat(conductor): staffing in the contract — assign, generate, retire worker profiles`.

### Task 5.7: per-profile outcomes (the evolution feedback signal)

**Files:**
- Modify: `reporting/timesheets.py` (add `by_profile(store)` grouping on the `budget_ledger.profile` column from Task 0.1), `reporting/fleet_viz.py` (`fleet_json` gains a compact `profiles` list)
- Test: `tests/test_timesheets.py`

`by_profile` returns `{profile, engagements, merged, blocked, tokens, cost, est_accuracy}` (est_accuracy = median actual/est over tasks with both). This is what the conductor's `{WORKERS}` block and the Resources tab render — the loop that makes profile generation/retirement *informed* rather than decorative.

**Step 5:** Commit: `feat(timesheets): per-profile outcome rollup — the workforce-evolution signal`.

---

## Phase 6 — Resources view + worker/role management (settings substrate)

### Task 6.1: `settings` overrides in the store (the safe management substrate)

Config.yaml stays the defaults file (human-edited, git-tracked); the board writes **whitelisted runtime overrides** to the store, consumed where `cmd_run` resolves knobs.

**Files:**
- Modify: `store/schema.sql` + `common/store.py` (`get_setting/set_setting/all_settings`), `orchestrator/orchestrator.py::cmd_run` (`:755-765` knob resolution)
- Test: `tests/test_store.py`, `tests/test_run_cli.py`

```sql
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,   -- e.g. 'super_worker.max_parallel'
    value      TEXT NOT NULL,      -- stringly; the consumer casts
    updated_at TEXT NOT NULL
);
```

Whitelist (one module-level tuple, imported by both the CLI and the server — single source):
`super_worker.max_parallel`, `super_worker.max_tasks_per_shift`, `super_worker.refill_threshold`, `super_worker.scope_check`, `super_worker.require_test`, `super_worker.auto_decompose`, `super_worker.max_profiles`, `super_worker.reviewer` (Phase 8).

In `cmd_run`, resolve each knob as `store override → config.yaml → hardcoded default` via one helper:

```python
def _knob(store, sw: dict, key: str, default, cast):
    v = store.get_setting(f"super_worker.{key}")
    if v is not None:
        return cast(v)
    return cast(sw.get(key, default))
```

**`require_test` note:** it is currently re-read from config inside `develop_and_merge` (`develop.py:258`). Thread it instead: `execute_claimed_tasks` gains `require_test: bool`, passes it into `develop_fn`, `develop_task` and `develop_and_merge` accept it as a parameter (default reads config, so existing tests stay green). One task, test-first in `tests/test_acceptance.py` (that is where the require_test gate is actually tested — `test_round_rejects_untested_source_when_require_test:77`; `test_code_gate.py` never touches require_test).

**Step 5:** Commits: `feat(settings): store-backed runtime overrides (whitelisted)` and `refactor(develop): thread require_test from the run entry (one knob resolution point)`.

### Task 6.2: resources gather + endpoints

**Files:**
- Create: `reporting/resources.py`
- Modify: `dashboard/fleet_server.py` (GET `/api/resources`, POST `/api/settings`, POST `/api/worker`)
- Test: `tests/test_resources.py` (new)

`resources(store) -> dict`:
- `roles`: the live-loop role registry — a static list in this module (`conductor`, `developer`, `researcher (refill)`, `scope_check`, `decompose`) each with `{name, transport, model_tier, wired: bool, prompt_path}` + existence check against `roles/<dir>/prompt.md`; legacy roles listed under `legacy: [...]` (from the remaining `roles/` dirs) so the two engines stay visibly distinct.
- `profiles`: `timesheets.by_profile(store)` merged with `store.list_profiles()` (description, model tier, active, outcomes).
- `spend_by_role`: `timesheets.by_agent(store)` (reuse — do not re-query).
- `caps`: resolved knob values (store override marked `overridden: true`).
- `live`: `fleet_viz.live_workers()` (reuse).

POST `/api/settings` `{key, value}`: CSRF guard + whitelist + type validation (ints ≥ 0, bools "true"/"false"); anything else → 400. Writes `store.set_setting` via a fresh per-request `Blackboard` (ground rule 2's dashboard clause; busy DB → 503). **The change takes effect at the next shift** (that's when `cmd_run` resolves knobs) — return `{"applied_at": "next shift"}` so the UI can say so honestly.

POST `/api/worker` `{action: add|retire, name, description?, overlay?, model?}`: same guardrails as the CLI (shared validation function — one source of truth), so the human can also generate/retire profiles from the board.

**Step 5:** Commit: `feat(resources): role/profile inventory + whitelisted runtime knobs via the board`.

---

## Phase 7 — Mission Control becomes multi-view

### Task 7.1: tab shell in `fleet.html`

**Files:**
- Modify: `dashboard/static/fleet.html`
- Test: JS syntax check (below) + `tests/test_fleet_viz.py` stays green (server-side state unchanged)

Design constraints (keep the file self-contained, no framework):
- Tab bar under the header: **Overview | Plan | Resources | Timesheets | Research | Shifts**. Overview = the existing panels minus the shifts list (which moves to its tab); the header (mission, mode toggle, STOP/Resume, KPI strip) stays visible on every tab.
- Polling: the header (mission, mode, STOP, KPI strip) is visible on EVERY tab, so a slow global `/api/fleet` tick (10 s) always runs to keep it live — without it the header goes stale outside Overview. Overview additionally polls `/api/fleet` at the existing 2 s tick while active; other tabs fetch their own endpoint on activation + every 10 s while active (`setInterval` swapped in one `activateTab(name)` function — one tab interval alive at a time, cleared on switch).
- Render functions per tab, one `<section id="tab-...">` each; hash-route (`location.hash`) so a view is linkable/bookmarkable.

### Task 7.2: Plan tab

Renders `/api/plan` + `/api/evm`: milestone cards (status-colored like `_SHIFT_COLOR`, progress bar from `progress.done/total`, budget vs AC bar) and the EVM totals (CPI, %complete, overhead) + the AC-cumulative sparkline (NOTE: the momentum sparkline in the file is a flex DIV bar chart — `el('spark').innerHTML = …<div class="s">…`, `:266` — not an SVG polyline; copy that div-bar pattern, there is no SVG sparkline to copy) + the est-vs-actual table.

### Task 7.3: Resources tab

Renders `/api/resources`: **profile cards** (description, model tier, engagements, merge rate, spend, active toggle → POST `/api/worker`), an "add profile" inline form, role cards (live count, spend, transport, wired badge), and the caps form (number inputs / checkboxes for the whitelist, Save → POST `/api/settings`, shows "applies next shift"). Legacy roles in a collapsed group.

### Task 7.4: Timesheets tab

Renders `/api/timesheets`: the rollup strip (per-role + per-profile totals) + the engagement table (shift, agent, profile, task, minutes, tokens, $, verdict; verdict colored via `_TASK_COLOR` semantics).

### Task 7.5: Research tab

**Files:** also modify `dashboard/fleet_server.py` (GET `/api/research`) reusing `reporting/summary.py::_gather_research_briefs` (`:125-142`) — make it importable (rename to `gather_research_briefs` with a back-compat alias) rather than copying it. Tab shows: staged briefs (title/technique/citation), research-sourced backlog tasks (already in `/api/fleet`), digests, proposed→shipped funnel.

### Task 7.6: Shifts tab

The existing shifts panel, moved, plus per-shift cost (Phase 0.7) and a click-to-expand full `report` + `resume_note` (fetch from a new `/api/shift?id=N` or embed truncated + `title` attribute — pick embed-first, it avoids an endpoint).

**Each 7.x step:** implement → JS check → manual browse (`./bin/factory viz --serve --no-open`, then open `http://127.0.0.1:8788`) → commit (`feat(board): <tab> view`).

---

## Phase 8 (optional, config-gated OFF) — the reviewer/tester role

The conductor-loop design planned a tester/reviewer; it was folded into the developer's TDD. Reinstate it as a cheap pre-merge gate so "role management" has a second manageable role and merges get an independent eye — **on the frontier tier** (review is judgment work; see the model-tier policy).

**Files:**
- Create: `roles/reviewer/prompt.md`
- Modify: `orchestrator/develop.py::develop_and_merge` (between `changed_paths` and the merge-lock section), `config.yaml` (`super_worker.reviewer: false`), whitelist in Phase 6.1 already carries `super_worker.reviewer`
- Test: `tests/test_develop_glue.py` (append — it already drives `develop_and_merge` with a FakeAdapter; `test_code_gate.py` covers the gate module, not this seam)

Mechanism: when enabled, run an **isolated `claude_p`** (not a super-worker — the diff is text; blindness/cheapness by construction, same transport as the judge, `roles/common.py:59`) over `git diff <base>..<branch>` + the task text + the spec block. Contract: one fenced JSON `{"approve": bool, "reason": "..."}`. Reject → `{"action": "discarded", "stage": "review", "error": reason}` (the existing close-out already turns any `discarded` stage into a blocked-task lesson). Fail-open on transport failure (like `scope_check`), ledger as `role_or_run="reviewer"`. TDD: approve path, reject path, transport-failure path.

Commit: `feat(reviewer): config-gated pre-merge review role (isolated, frontier-tier, fail-open)`.

---

## Explicitly out of scope (so nobody gold-plates)

- **Time-phased PV / SPI, Gantt charts** — needs planned shift ranges per milestone; revisit after the plan entity has real usage.
- **Teams (multiple roles collaborating on one task)** — the one-worker-per-task model stays; squads remain isolation-only. Profiles specialize the *one* worker; they don't create co-editing.
- **Profiles changing toolsets/boundaries** — a profile is persona + model tier only, by construction. New *capabilities* (tools, boundaries) remain human code changes.
- **Workers choosing their own model mid-task** — the rail assigns; internal fan-out to cheaper models via the worker's own Task/Workflow tools is allowed and encouraged in overlays, but the primary tier is fixed at dispatch.
- **Factory self-modification (loop A)** — separate design doc; this roadmap only makes the telemetry that loop will need.
- **Auth/remote access for the board** — stays localhost-only, CSRF-guarded.
- **Editing `roles.super_workers`, Guest-House `user`, or the model whitelist from the UI** — security-relevant; file-only.

## Verification (end-to-end, after each phase and at the end)

```bash
# 1. Full test suite
python3 -m pytest tests/ -q

# 2. Inline-JS syntax check (MANDATORY after any fleet.html change)
rm -f /tmp/fleet_js_*.js   # stale files from a prior run make the loop fail confusingly
python3 - <<'EOF'
import re
html = open('dashboard/static/fleet.html').read()
for i, m in enumerate(re.findall(r'<script>(.*?)</script>', html, re.S)):
    open(f'/tmp/fleet_js_{i}.js', 'w').write(m)
    print(f'/tmp/fleet_js_{i}.js')
EOF
for f in /tmp/fleet_js_*.js; do node --check "$f" && echo "OK $f"; done

# 3. Live board smoke (read-only; safe with STOP engaged)
./bin/factory viz --serve --no-open &   # then browse http://127.0.0.1:8788
curl -s http://127.0.0.1:8788/api/fleet | python3 -m json.tool | head -40
curl -s http://127.0.0.1:8788/api/timesheets | python3 -m json.tool | head
curl -s http://127.0.0.1:8788/api/plan | python3 -m json.tool
curl -s http://127.0.0.1:8788/api/resources | python3 -m json.tool | head -40
kill %1

# 4. Accounting truth check after the first REAL shift post-Phase-0
sqlite3 store/blackboard.db "SELECT role_or_run, tokens, cost, shift_id, profile, notes FROM budget_ledger ORDER BY id DESC LIMIT 10"
sqlite3 store/blackboard.db "SELECT id, tokens_used FROM shifts ORDER BY id DESC LIMIT 3"
# expect: conductor + developer:<task> rows with the shift id + the profile column set;
#         shifts.tokens_used >= conductor-only values

# 5. CLI surfaces
./bin/factory plan list && ./bin/factory worker list && ./bin/factory timesheet --limit 10 && ./bin/factory evm

# 6. Profile routing truth check after the first profiled dispatch
sqlite3 store/blackboard.db "SELECT name, model, active, created_by FROM worker_profiles"
# then confirm the dispatched claude -p argv carried --model (visible via ps during a shift,
# or assert in the develop_glue tests — do not trust the profile row alone)

# 7. Bench-follows-the-target check: point config at a repo with a different stack
#    (or fake one in a tmp dir via the staffing tests) and confirm the seeder is additive:
#    new stack profile appears, existing profiles remain active, and
#    sqlite3 store/blackboard.db "SELECT value FROM settings WHERE key='staffing.seeded_for'"
#    shows the new target slug.
```
