"""The fleet visualization (presentation layer) — a self-contained HTML view of the
factory's (super) worker instances and their activities.

The factory's workers ARE its records: each **shift** is a conductor instance; each task it
**claimed → done/blocked** is a developer-worker dispatch; `source='research'` tasks are
researcher output. This module reads that ground truth from the blackboard (read-only) and
renders one self-contained HTML file — no server, inspectable, regenerable. It also snaps
the LIVE `claude -p` processes running at view time, labelled by role.

`build_fleet_state` (pure, hermetic) gathers the store state; `render_fleet_html` (pure)
turns a state + a live-worker list into HTML; `generate_fleet_html` ties them together and
writes the file. Never writes to the store; never crashes the caller."""
from __future__ import annotations

import html
import os
import subprocess
from datetime import datetime
from typing import Optional

from ..common import killswitch as _killswitch
from ..common import mode as _mode
from ..common import paths


def _autopilot_status() -> dict:
    """Is the AUTO runner actually alive? (lazy import — keep fleet_viz importable anywhere)."""
    try:
        from ..orchestrator import autopilot
        return autopilot.status()
    except Exception:  # noqa: BLE001
        return {"running": False, "pid": None}


def _collab_state() -> dict:
    """The agora collaboration view (who's working + who @mentions whom). Crash-proof."""
    try:
        from . import collab
        return collab.agora_state()
    except Exception:  # noqa: BLE001
        return {"active": False, "total": 0, "mentions": 0, "senders": 0,
                "agents": [], "messages": [], "edges": []}


def _parse_iso(ts: str):
    try:
        return datetime.strptime(ts or "", "%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, TypeError):
        return None


def _shift_seconds(sh: dict):
    """Wall-clock seconds a shift took (ended − started), or None while it's still running."""
    a, b = _parse_iso(sh.get("started_at", "")), _parse_iso(sh.get("ended_at") or "")
    return max(0.0, (b - a).total_seconds()) if (a and b) else None

_SHIFT_COLOR = {"running": "#3b82f6", "completed": "#22c55e", "timed_out": "#f59e0b",
                "budget_exhausted": "#f59e0b", "halted": "#a855f7", "error": "#ef4444"}
_TASK_COLOR = {"open": "#64748b", "in_progress": "#3b82f6", "done": "#22c55e",
               "blocked": "#ef4444", "dropped": "#475569", "claimed": "#3b82f6"}
_MISSION_COLOR = {"advancing": "#22c55e", "steady_state": "#f59e0b",
                  "blocked": "#ef4444", "reached": "#a855f7"}
_TASK_COLUMNS = ("open", "in_progress", "done", "blocked")


def build_fleet_state(store) -> dict:
    """Read-only gather of the fleet's state: the mission, the shifts (each with the tasks
    it worked), the backlog grouped by status, the mission-status timeline, and the
    research digests. Deterministic — no live processes here (those are layered on
    separately so this stays hermetically testable)."""
    mission = store.active_mission()
    shifts = store.list_shifts(limit=30)
    tasks = store.list_tasks()
    by_shift: dict = {}
    for t in tasks:
        if t.get("shift_id"):
            by_shift.setdefault(t["shift_id"], []).append(t)
    for sh in shifts:
        sh["tasks"] = by_shift.get(sh["id"], [])
    by_status = {s: [t for t in tasks if t["status"] == s] for s in _TASK_COLUMNS}
    return {
        "mission": mission,
        "shifts": shifts,
        "tasks_by_status": by_status,
        "task_total": len(tasks),
        "mission_status": store.mission_status_history(limit=24),
        "digests": store.unconsumed_digests(),
    }


def fleet_json(store) -> dict:
    """JSON-serializable live state for the `--serve` frontend to poll: the mission, summary
    counts, the DERIVED current loop phase, the shifts (compact), live workers, the
    mission-status timeline, and digests. (build_fleet_state + live_workers, distilled.)"""
    state = build_fleet_state(store)
    live = live_workers()
    by = state["tasks_by_status"]
    running = next((s for s in state["shifts"] if s["status"] == "running"), None)
    dev_live = [w for w in live if w["role"] == "developer worker"]
    res_live = [w for w in live if w["role"] == "researcher"]
    if dev_live:
        phase = "develop"            # the rail is running a developer worker
    elif res_live:
        phase = "research"
    elif running:
        phase = "plan"               # a shift is up but no worker yet → the conductor is planning
    else:
        phase = "idle"
    ms = state["mission_status"]
    m = state["mission"]
    shifts = state["shifts"]
    done, blocked = by["done"], by["blocked"]
    all_tasks = store.list_tasks()
    research = [t for t in all_tasks if t["source"] == "research"]
    research_shipped = sum(1 for t in research if t["status"] == "done")

    durations = [d for d in (_shift_seconds(s) for s in shifts) if d is not None]
    total_tokens = sum(int(s.get("tokens_used") or 0) for s in shifts)
    shipped = len(done)

    # CEO KPIs — the numbers worth watching.
    kpi = {
        "shipped": shipped,                                   # merges into the target (what's built)
        "shifts": len(shifts),
        "exec_seconds": int(sum(durations)),                  # total execution time
        "avg_shift_seconds": int(sum(durations) / len(durations)) if durations else 0,
        "total_tokens": total_tokens,
        "tokens_per_merge": int(total_tokens / shipped) if shipped else 0,   # efficiency
        "workers_live": len(live),
        "workers_total": shipped + len(blocked),              # developer dispatches that ran to a verdict
        "research_proposed": len(research),                   # is research producing work?
        "research_shipped": research_shipped,
        "backlog_open": len(by["open"]), "in_progress": len(by["in_progress"]),
        "blocked": len(blocked),
    }
    # Mission momentum — the honest "how far": are we still advancing, or converged?
    latest = ms[0]["status"] if ms else None
    verdict = {
        "advancing": "Advancing — actively building toward the mission",
        "steady_state": "Converged — backlog drained, awaiting a new direction",
        "blocked": "Blocked — work is stuck, needs attention",
        "reached": "Mission reached",
    }.get(latest, "Idle — no shifts run yet")

    return {
        "mission": m["statement"] if m else None,
        "target": (m.get("target_repo") if m else None) or None,
        "mode": _mode.read_mode(),          # AUTO (self-driving) | SHIFT (one-and-wait)
        "autopilot": _autopilot_status(),   # is the AUTO runner actually alive? (+ pid)
        "halted": _killswitch.is_halted(),  # STOP engaged → the board shows Resume
        "collab": _collab_state(),          # the agora bus: who's working + who @mentions whom
        "phase": phase,
        "status": latest,
        "running_shift": running["id"] if running else None,
        "summary": {"shifts": len(shifts), "shipped": shipped, "open": len(by["open"]),
                    "in_progress": len(by["in_progress"]), "blocked": len(blocked)},
        "kpi": kpi,
        "momentum": {"verdict": verdict, "status": latest,
                     "merges_series": [sum(1 for t in s["tasks"] if t["status"] == "done")
                                       for s in reversed(shifts)],           # oldest → newest
                     "status_series": [r["status"] for r in reversed(ms)]},
        # THE BACKLOG + PLAN — the active (non-done) tasks: in_progress = claimed/planned
        # THIS shift, open = waiting (incl. research proposals), blocked = stuck. So the
        # operator sees both what the conductor is working and what research proposed.
        "backlog": sorted(
            [{"id": t["id"], "title": t["title"], "source": t["source"],
              "status": t["status"], "ref": t.get("source_ref", ""),
              "result": t.get("result", "")}      # blocked → the WHY (error/discard reason)
             for t in all_tasks if t["status"] in ("open", "in_progress", "blocked")],
            key=lambda t: {"in_progress": 0, "open": 1, "blocked": 2}.get(t["status"], 3)),
        # WHAT'S BEEN BUILT — the ledger of shipped changes (title + sha), newest first.
        "built": [{"title": t["title"], "sha": (t.get("result") or "")[:10],
                   "source": t["source"], "shift": t.get("shift_id")}
                  for t in sorted(done, key=lambda t: t.get("updated_at", ""), reverse=True)][:25],
        "research": {"proposed": len(research), "shipped": research_shipped,
                     "working": len(research) > 0},
        "live": live,
        "shifts": [{"id": s["id"], "status": s["status"], "tokens": int(s.get("tokens_used") or 0),
                    "shipped": sum(1 for t in s["tasks"] if t["status"] == "done"),
                    "seconds": int(_shift_seconds(s) or 0),
                    "report": (s.get("report") or "")[:240]} for s in shifts],
        "mission_status": [{"status": r["status"], "shift": r.get("shift_id"),
                            "rationale": r.get("rationale", "")} for r in ms],
        "digests": [d["summary"][:140] for d in state["digests"]],
    }


def live_workers() -> list[dict]:
    """Snapshot the `claude -p` processes running right now, labelled by role from their
    args. Best-effort — returns [] if pgrep is unavailable."""
    try:
        out = subprocess.run(["pgrep", "-fl", "claude -p"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:  # noqa: BLE001
        return []
    workers = []
    for line in out.splitlines():
        # Real super-workers are `claude -p … --add-dir <wd>`. Requiring --add-dir (and a
        # numeric pid) skips shells/echoes/greps that merely MENTION "claude -p" in text.
        if "claude -p" not in line or "--add-dir" not in line:
            continue
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        pid = parts[0]
        # Classify by the TOOLSET signature (the prompt is on stdin + --add-dir varies, so
        # the role name isn't in the command line): a worker in a clone is a developer; web
        # + NO Bash is the read-only researcher; Bash outside a clone is the conductor.
        is_clone = "/cf-dev-" in line or "/cf-champ-" in line or ".factory-auto" in line
        has_bash = " Bash" in line
        has_web = "WebSearch" in line or "WebFetch" in line
        if is_clone:
            role, where = "developer worker", _add_dir(line)
        elif has_web and not has_bash:
            role, where = "researcher", _add_dir(line)
        elif has_bash:
            role, where = "conductor", "planning the shift"
        else:
            role, where = "worker", _add_dir(line)
        workers.append({"pid": pid, "role": role, "where": where})
    return workers


def _add_dir(line: str) -> str:
    parts = line.split()
    if "--add-dir" in parts:
        i = parts.index("--add-dir")
        if i + 1 < len(parts):
            return parts[i + 1]
    return ""


def generate_fleet_html(store, *, out_path: Optional[str] = None, generated_at: str = "") -> str:
    """Gather + render + write the fleet HTML. Returns the path written."""
    out_path = out_path or os.path.join(paths.LOGS_DIR, "fleet.html")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    html_doc = render_fleet_html(build_fleet_state(store), live=live_workers(),
                                 generated_at=generated_at)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)
    return out_path


# --------------------------------------------------------------------------- #
# rendering (pure: state -> html string)                                      #
# --------------------------------------------------------------------------- #
def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _chip(text: str, color: str) -> str:
    return (f'<span class="chip" style="background:{color}22;color:{color};'
            f'border:1px solid {color}55">{_esc(text)}</span>')


def _task_line(t: dict) -> str:
    c = _TASK_COLOR.get(t["status"], "#64748b")
    sha = (t.get("result") or "")[:10]
    extra = f' <code>{_esc(sha)}</code>' if t["status"] == "done" and sha else ""
    if t["status"] == "blocked" and t.get("result"):
        extra = f' <span class="muted">({_esc(t["result"][:40])})</span>'
    return (f'<div class="task"><span class="dot" style="background:{c}"></span>'
            f'{_chip(t["status"], c)} <span class="src">{_esc(t["source"])}</span> '
            f'<b>{_esc(t["id"])}</b> {_esc(t["title"])}{extra}</div>')


def _shift_card(sh: dict) -> str:
    c = _SHIFT_COLOR.get(sh["status"], "#64748b")
    shipped = sum(1 for t in sh["tasks"] if t["status"] == "done")
    head = (f'<div class="shift-head"><b>shift {sh["id"]}</b> {_chip(sh["status"], c)} '
            f'<span class="muted">{sh.get("tokens_used", 0):,} tok · {shipped} shipped · '
            f'{len(sh["tasks"])} worked</span></div>')
    report = f'<div class="report">{_esc(sh.get("report", "")[:600])}</div>' if sh.get("report") else ""
    resume = (f'<div class="resume">↪ next: {_esc(sh["resume_note"][:200])}</div>'
              if sh.get("resume_note") else "")
    tasks = "".join(_task_line(t) for t in sh["tasks"]) or '<div class="muted">— no tasks worked —</div>'
    return f'<div class="card" style="border-left:3px solid {c}">{head}{report}{tasks}{resume}</div>'


def _board_column(status: str, tasks: list) -> str:
    c = _TASK_COLOR.get(status, "#64748b")
    cards = "".join(
        f'<div class="bcard"><b>{_esc(t["id"])}</b><br>{_esc(t["title"][:70])}'
        f'<br><span class="src">{_esc(t["source"])}'
        f'{("/" + _esc(t["source_ref"])) if t.get("source_ref") else ""}</span></div>'
        for t in tasks) or '<div class="muted">—</div>'
    return (f'<div class="col"><div class="col-head" style="color:{c}">'
            f'{_esc(status)} <span class="muted">({len(tasks)})</span></div>{cards}</div>')


def render_fleet_html(state: dict, *, live: Optional[list] = None, generated_at: str = "") -> str:
    live = live or []
    m = state.get("mission")
    mission_txt = _esc(m["statement"]) if m else "— no mission set —"
    target = _esc(m.get("target_repo", "")) if m else ""

    live_html = ""
    if live:
        items = "".join(
            f'<div class="live-item"><span class="pulse"></span>'
            f'<b>{_esc(w["role"])}</b> <span class="muted">pid {_esc(w["pid"])}'
            f'{(" · " + _esc(w["where"])) if w.get("where") else ""}</span></div>'
            for w in live)
        live_html = f'<section><h2>● Live now — {len(live)} worker(s)</h2>{items}</section>'
    else:
        live_html = '<section><h2>● Live now</h2><div class="muted">no claude -p workers running</div></section>'

    shifts_html = "".join(_shift_card(sh) for sh in state["shifts"]) \
        or '<div class="muted">no shifts yet — run: factory run --mission "…"</div>'

    board_html = "".join(_board_column(s, state["tasks_by_status"][s]) for s in _TASK_COLUMNS)

    ms = state["mission_status"]
    strip = "".join(
        f'<span class="ms" title="{_esc(r["status"])} (shift {r.get("shift_id","?")})" '
        f'style="background:{_MISSION_COLOR.get(r["status"], "#64748b")}"></span>'
        for r in reversed(ms))
    ms_html = (f'<section><h2>Mission progress</h2><div class="strip">{strip}</div>'
               f'<div class="muted">{_esc(ms[0]["status"]) if ms else "—"} '
               f'{_esc(ms[0]["rationale"]) if ms else ""}</div></section>') if ms else ""

    digests = state["digests"]
    dig_html = ""
    if digests:
        items = "".join(f'<div class="dig">📦 {_esc(d["summary"][:160])}</div>' for d in digests)
        dig_html = (f'<section><h2>Research digests <span class="muted">(what shipped → '
                    f'fuels the researchers)</span></h2>{items}</section>')

    return _PAGE.format(
        mission=mission_txt, target=target, generated=_esc(generated_at),
        live=live_html, shifts=shifts_html, board=board_html, ms=ms_html, digests=dig_html,
        task_total=state["task_total"], shift_count=len(state["shifts"]))


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Harness Factory — Fleet</title><style>
:root{{--bg:#0b1020;--card:#141a2e;--ink:#e2e8f0;--muted:#94a3b8;--line:#1e293b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px}}
header{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;
border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:8px}}
h1{{font-size:20px;margin:0}}h2{{font-size:15px;margin:22px 0 10px;color:var(--ink)}}
.mission{{color:var(--muted);max-width:760px}}.mission b{{color:var(--ink)}}
.muted{{color:var(--muted)}}.src{{color:#7dd3fc;font-size:12px}}
code{{background:#0008;padding:1px 5px;border-radius:4px;color:#fbbf24;font-size:12px}}
.chip{{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600}}
.card{{background:var(--card);border-radius:10px;padding:12px 14px;margin:10px 0}}
.shift-head{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.report{{color:var(--muted);font-size:13px;margin:8px 0;white-space:pre-wrap}}
.resume{{color:#7dd3fc;font-size:12px;margin-top:6px}}
.task{{display:flex;align-items:center;gap:6px;padding:3px 0;font-size:13px;flex-wrap:wrap}}
.dot{{width:7px;height:7px;border-radius:50%;display:inline-block}}
.board{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.col{{background:var(--card);border-radius:10px;padding:10px;min-height:60px}}
.col-head{{font-weight:700;text-transform:uppercase;font-size:11px;letter-spacing:.5px;margin-bottom:8px}}
.bcard{{background:#0b1020;border:1px solid var(--line);border-radius:7px;padding:7px 8px;margin:6px 0;font-size:12px}}
.live-item{{display:flex;align-items:center;gap:8px;padding:4px 0}}
.pulse{{width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 0 #22c55e;
animation:p 1.6s infinite}}@keyframes p{{0%{{box-shadow:0 0 0 0 #22c55e88}}70%{{box-shadow:0 0 0 8px #22c55e00}}100%{{box-shadow:0 0 0 0 #22c55e00}}}}
.strip{{display:flex;gap:3px;flex-wrap:wrap}}.ms{{width:14px;height:14px;border-radius:3px;display:inline-block}}
.dig{{background:var(--card);border-radius:8px;padding:8px 10px;margin:6px 0;font-size:13px}}
footer{{color:var(--muted);font-size:12px;margin-top:30px;border-top:1px solid var(--line);padding-top:12px}}
</style></head><body><div class="wrap">
<header><div><h1>🏭 Harness Factory — Fleet</h1>
<div class="mission"><b>Mission:</b> {mission} {target}</div></div>
<div class="muted">{shift_count} shifts · {task_total} tasks<br>generated {generated}</div></header>
{live}
<section><h2>Shifts <span class="muted">(conductor instances → developer-worker dispatches)</span></h2>{shifts}</section>
<section><h2>Backlog</h2><div class="board">{board}</div></section>
{ms}
{digests}
<footer>The factory's workers are its records: a shift is a conductor instance; each task it
claimed→done/blocked is a developer-worker dispatch; research tasks are researcher output.
Read-only snapshot of the blackboard — regenerate with <code>factory viz</code>.</footer>
</div></body></html>"""
