# PMO dashboard redesign — CompassAI grammar for Mission Control

**Date:** 2026-07-03 · **Source studies:** CompassAI `mvp-workflow-preview.html` +
`feature-design-preview.html` (PMO/PM perspective) · **Operator goal:** the factory
dashboard should *work in a similar fashion*.

## What the studies embody

The two CompassAI previews encode a consistent PM/PMO working grammar:

1. **Work Queue is home.** "The PM starts from required actions, not from editing
   scattered tables." Each queue item opens the *owning surface* for the object.
2. **KPI strip with semantic tinting** — white cards; amber/red/green tint when a
   number needs attention.
3. **Owning surfaces.** Every object has exactly one screen where it is acted on;
   lists elsewhere are for selection and link back.
4. **Readiness checklists gate transitions** (closure checks, conversion checks).
5. **RAG status pills** (On Track / At Risk / Blocked) everywhere a health signal fits.
6. **Status report / executive brief** — a one-page steering artifact: KPIs +
   3-box narrative (achievements / blockers / next steps) + top attention items.
7. **Visual language:** light gray page, dark slate topbar with main tabs, white
   cards with thin borders, Inter-ish type, uppercase micro-labels, status pills.

## Mapping to the factory (operator = the PM; the fleet = the project team)

| CompassAI | Factory |
|---|---|
| Work Queue (required PM actions) | **Queue** — derived operator actions (STOP engaged, autopilot idle, no mission, blocked tasks, @human pings, over-budget milestones, staged briefs) |
| Projects / phases | **Plan** — milestones + backlog + estimate calibration |
| Execution (EVM, approvals, invoices) | **Execution** — the loop, live workers, collaboration bus, shifts |
| Timesheets | **Timesheets** — engagements = payroll actuals (already modeled) |
| Finance cockpit (BAC/AC/CTC/EAC) | **Finance** — EVM cockpit: PV/EV/AC/CPI, burn by shift, per-milestone budget, question shortcuts |
| Cost Sheets (pre-project baseline) | **Research** — staged briefs are the factory's cost sheets (staged work → converted to tasks) + funnel + digests + upstream issues |
| Status report / portfolio brief | **Report** — executive brief rendered from live data |

RAG mapping: `advancing → On Track (green)`, `steady_state → Steady (amber)`,
`blocked → Blocked (red)`, `reached → Delivered (purple)`, halted → `Halted (red)`.

## Architecture

- **Server:** one addition to `/api/fleet` — `queue`: a list of derived operator
  actions, computed by a pure `derive_queue(payload) -> list[dict]` in
  `reporting/fleet_viz.py` (hermetic, unit-tested). Each action:
  `{id, title, sub, severity: blue|amber|red|green, tab}` where `tab` is the owning
  surface. Also add `resume_note` (latest shift's) for the Report's "next steps".
  No new endpoints; all existing GET/POST endpoints and guards stay untouched.
- **Client:** rewrite `dashboard/static/fleet.html` in the CompassAI visual +
  information grammar. Tabs: **Queue (home) · Plan · Execution · Resources ·
  Timesheets · Finance · Research · Report**, hash-routed like today. All existing
  controls survive: mission editor, mode toggle, STOP/resume, knobs form, profile
  add/retire, shift-row expand. The header keeps the 10s global tick; the active
  tab keeps its own 2s/10s tick.
- **Queue derivation (v1 rules, ordered by severity):**
  - red: STOP engaged → "Resume the fleet" (header action, shown for awareness)
  - red: mission status `blocked` → open Plan
  - red: no mission set → set the mission (header editor)
  - amber: mode=auto but autopilot not running
  - amber: each blocked task (cap 5) → "Narrow/reframe: <title>" → Plan
  - amber: recent @human mention on the bus (best-effort, from collab messages) → Execution
  - (milestone burn > budget is flagged on the Finance tab's milestone table
    client-side, where /api/evm data lives — not a queue rule in v1)
  - blue: staged research briefs waiting → Research
  - blue: shift mode + idle → "Fleet is parked — run a shift or switch to Auto"
  - green: nothing needs you → "All clear"
- **Report tab (client-side render, no new endpoint):** mission + RAG pill, KPI row
  (CPI, % complete, shipped, cost), narrative boxes — Achievements (recent built),
  Blockers (blocked tasks + halted/escalations), Next steps (latest resume_note or
  active milestone) — and top attention items (top queue entries).

## Constraints

- STOP stays engaged throughout (operator's brake) — never clear it.
- `node --check` the inline JS before claiming the page works (project memory).
- Every `el()` id must exist in the DOM (Phase-7 lesson).
- All 503 existing tests stay green; new tests for `derive_queue` + payload keys.
- Repo stays local: feature branch `feat/pmo-dashboard-redesign`, no push.

## Testing

1. Unit: `derive_queue` — halted, autopilot-idle, blocked tasks capped at 5,
   no-mission, briefs, all-clear; severity ordering.
2. Unit: `fleet_json` carries `queue` + `resume_note`.
3. Static: `node --check` on extracted inline JS; el() id cross-check.
4. Integration: boot server on a spare port against a temp store; curl `/` +
   all six GET endpoints → 200.
