"""The conductor (design: docs/plans/2026-06-25-conductor-loop-design.md, step 3).

The LLM lead the shift harness runs. `run_conductor` matches the harness's injected-
conductor signature: it assembles the contract + the live shift context (mission, the
prior shift's resume note, the open backlog, unconsumed research digests), spawns a FULL
claude instance (settings=user → agora + diary + MCP) that drives `./bin/factory
develop-once` to dispatch gated developer workers, and parses the final JSON block into
{status, report, resume_note, tokens_used}. The harness owns the ceilings.

Hermetic in tests: claude_super is monkeypatched, so no live agent is spawned. The live
conductor runs only when the operator runs the factory (like develop-once)."""
from __future__ import annotations

import time
from typing import Optional

from ..common import config, paths
from . import common

# The conductor DISPATCHES (Bash → ./bin/factory) and coordinates (agora via Bash,
# diary via Skill, web). It does NOT edit the target's code itself — no Write/Edit.
CONDUCTOR_TOOLS = ("Read", "Bash", "Grep", "Glob", "Task", "Workflow", "Skill") + common.WEB_TOOLS

# The shift-status vocabulary the harness/schema accept. The conductor's free-text JSON
# status is coerced into this set — 'blocked' etc. are MISSION-level (recorded by assess),
# not shift-level, and would violate the shifts.status CHECK constraint.
_VALID_SHIFT_STATUS = {"completed", "halted", "timed_out", "budget_exhausted", "error"}

# Task 1.2: the sectioned resume note's (json_key, label) pairs, in render order.
_RESUME_SECTIONS = (("verified", "VERIFIED"), ("open", "OPEN FAILURES"), ("next", "NEXT"))


def _fold_resume_note(note) -> str:
    """Task 1.2 (P7 structure at the write site): the conductor MAY return resume_note as
    an object with optional verified/open/next keys — fold it into one labeled block
    (VERIFIED/OPEN FAILURES/NEXT lines) for the existing shifts.resume_note column. A bare
    string passes through unchanged (fail-open floor = status quo); the abnormal end paths
    (transport sentinel above, shift timeout/error, crash-reap) never reach this parse.
    A dict with no usable sections (or an explicit null) degrades to '' — never a
    stringified '{}'/'None' in the next shift's {RESUME} seam."""
    if not isinstance(note, dict):
        return note if isinstance(note, str) else ("" if note is None else str(note))
    lines = []
    for key, label in _RESUME_SECTIONS:
        v = note.get(key)
        if isinstance(v, (list, tuple)):                # plural facts → one '; '-joined line
            v = "; ".join(str(x).strip() for x in v if str(x).strip())
        v = str(v).strip() if v is not None else ""
        if v:
            lines.append(f"{label}: {v}")
    return "\n".join(lines)


def _bullets(rows, fmt, empty: str) -> str:
    return "\n".join(fmt(r) for r in rows) or empty


def _plan_bullets(store) -> str:
    """Render the plan for the {PLAN} seam: per-milestone progress, budget, and — the signal
    the conductor revises the plan against (Task 2.4) — the linked tasks' estimated vs ACTUAL
    tokens (`./bin/factory timesheet` has the per-engagement breakdown)."""
    ms = store.list_milestones()
    if not ms:
        return "(no plan yet — draft 2-4 milestones with `./bin/factory plan add …`)"
    lines = []
    for m in ms:
        p = store.milestone_progress(m["id"])
        e = store.milestone_effort(m["id"])
        line = (f"- M{m['id']} [{m['status']}] {m['title']} — {p['done']}/{p['total']} tasks, "
                f"budget {m['budget_tokens']:,} tok, "
                f"est {e['est_tokens']:,} vs actual {e['actual_tokens']:,} tok")
        if m.get("deliverable"):
            line += f"; deliverable: {m['deliverable']}"
        if m.get("acceptance"):
            line += f"; acceptance: {m['acceptance']}"
        lines.append(line)
    return "\n".join(lines)


def _workers_bullets(store) -> str:
    """Render the active bench for the {WORKERS} seam (Task 5.6): each profile's tier + outcome
    stats (engagements, merge rate, tokens, estimate accuracy) so the conductor assigns, generates
    and retires profiles on EVIDENCE (the timesheet), not guesswork."""
    from ..reporting import timesheets
    profs = store.list_profiles(active_only=True)
    if not profs:
        return ("(no bench yet — it seeds at run start; generate specialists with "
                "`./bin/factory worker add <name> --description … --overlay … --model standard`)")
    roll = {r["profile"]: r for r in timesheets.by_profile(store)}
    lines = []
    for p in profs:
        o = roll.get(p["name"], {})
        eng, merged, blocked = (int(o.get("engagements", 0)), int(o.get("merged", 0)),
                                int(o.get("blocked", 0)))
        completed = merged + blocked           # rounds that reached a verdict (excludes STOP-halted)
        rate = f"{100 * merged // completed}%" if completed else "—"
        acc = o.get("est_accuracy")
        acc_s = f"{acc:.1f}x actual/est" if acc is not None else "no est data"
        lines.append(f"- {p['name']} [{p.get('model') or 'frontier'}] — {(p.get('description') or '')[:60]}; "
                     f"{eng} eng, {rate} merged, {int(o.get('tokens', 0)):,} tok, {acc_s}")
    return "\n".join(lines)


def build_conductor_prompt(store, mission: dict, *, shift_id: int, token_budget: int) -> str:
    """Fill the conductor contract with this shift's live context from the store + the
    target's open GitHub issues (so planning is issue-aware, not just backlog-aware)."""
    from .research_feed import fetch_issues
    prior = store.prior_shift(shift_id)
    resume = (prior.get("resume_note") if prior else "") or "(first shift — no prior note)"
    backlog = _bullets(
        store.list_tasks(status="open"),
        lambda t: f"- [{t['source']}{('/' + t['source_ref']) if t['source_ref'] else ''}] "
                  f"{t['id']}: {t['title']}",
        "(empty — mine new work from the target's issues + research)")
    # Task 1.1: the {BLOCKED} seam. The backlog above injects status='open' ONLY, so blocked
    # outcomes never reached the prompt — this guarantees the freshest failures (with the
    # reason each blocked) are prompt input, newest-first.
    blocked = _bullets(
        store.recent_blocked_tasks(limit=8),
        lambda t: f"- {t['id']}: {t['title']} — {(t['result'] or '')[:160]}",
        "(none blocked — nothing to reopen)")
    digests = _bullets(store.unconsumed_digests(), lambda d: f"- {d['summary']}", "(none)")
    target = mission.get("target_repo") or config.target_repo_slug()   # robust fallback if unset
    issues = fetch_issues(target) or "(none fetched)"
    from ..reporting import factory_memory                  # factory memory: prior lessons → context
    return (common._load_prompt("conductor")
            .replace("{MISSION}", mission.get("statement", ""))
            .replace("{TARGET_REPO}", target or "(none set)")
            .replace("{BUDGET}", f"{token_budget:,} tokens")
            .replace("{RESUME}", resume)
            .replace("{MEMORY}", factory_memory.memory_card(store, "conductor"))
            .replace("{ISSUES}", issues)
            .replace("{BACKLOG}", backlog)
            .replace("{BLOCKED}", blocked)
            .replace("{PLAN}", _plan_bullets(store))
            .replace("{WORKERS}", _workers_bullets(store))
            .replace("{DIGESTS}", digests))


def run_conductor(store, *, shift_id: int, mission: dict, token_budget: int,
                  wall_clock_s: int, as_user: Optional[str] = None,
                  claude_bin: str = "claude") -> dict:
    """Run one conductor shift. Signature matches the harness's injected conductor (the
    extra as_user/claude_bin default to DEV mode — same-user; the CLI passes the
    Guest-House user for prod). Returns {status, report, resume_note, tokens_used}."""
    sw = config.load_config().get("super_worker", {}) or {}
    prompt = build_conductor_prompt(store, mission, shift_id=shift_id, token_budget=token_budget)
    t0 = time.monotonic()
    reply, tokens, cost = common.claude_super(
        prompt, workdir=paths.FACTORY_ROOT,                # drives ./bin/factory from the repo root
        allowed_tools=CONDUCTOR_TOOLS,
        as_user=as_user, claude_bin=claude_bin,
        settings=sw.get("settings", "user"),               # full instance: agora + diary + MCP
        extra_env={"AGORA_SQUAD": sw.get("conductor_squad", "factory-conductor")},
        max_turns=int(sw.get("conductor_max_turns", 60)),  # it loops internally across the shift
        timeout=wall_clock_s)
    # Ledger the shift lead's own spend (Task 0.4). Placed BEFORE the sentinel branch so both
    # the failed-spawn and the normal return paths are covered by one call.
    store.add_budget("conductor", tokens, cost, notes="shift lead",
                     shift_id=shift_id, seconds=round(time.monotonic() - t0, 1))
    if reply.startswith("[claude -p"):    # transport sentinel: the spawn failed / timed out /
        # crashed — claude_super never raises, so WITHOUT this the shift would be mislabeled a
        # clean 'completed' with a blank resume note (the wall-clock ceiling would be dead).
        return {"status": "error", "report": reply[:2000],
                "resume_note": f"conductor spawn failed or hit the wall-clock: {reply[:200]}",
                "tokens_used": tokens}

    obj = common._parse_obj(reply)
    if not isinstance(obj, dict):     # prose with no fenced JSON parses to a bare string/None
        obj = {}
    status = obj.get("status", "completed")
    if status not in _VALID_SHIFT_STATUS:   # coerce 'blocked'/anything → a legal shift status
        status = "completed"                 # (blockers live in the report + mission_status)
    return {"status": status,
            "report": obj.get("report") or reply[:2000],
            "resume_note": _fold_resume_note(obj.get("resume_note", "")),
            "tokens_used": tokens}
