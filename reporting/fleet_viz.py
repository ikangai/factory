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
from typing import Optional

from ..common import paths

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
        if "claude -p" not in line or "pgrep" in line:
            continue
        pid = line.split(" ", 1)[0]
        if "/cf-dev-" in line or "/cf-champ-" in line or ".factory-auto" in line:
            role, where = "developer worker", _add_dir(line)
        elif "research" in line.lower():
            role, where = "researcher", _add_dir(line)
        elif "/factory" in line:
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
