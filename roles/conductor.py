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


def _bullets(rows, fmt, empty: str) -> str:
    return "\n".join(fmt(r) for r in rows) or empty


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
    digests = _bullets(store.unconsumed_digests(), lambda d: f"- {d['summary']}", "(none)")
    issues = fetch_issues(mission.get("target_repo", "")) or "(none fetched)"
    return (common._load_prompt("conductor")
            .replace("{MISSION}", mission.get("statement", ""))
            .replace("{TARGET_REPO}", mission.get("target_repo", "") or "(none set)")
            .replace("{BUDGET}", f"{token_budget:,} tokens")
            .replace("{RESUME}", resume)
            .replace("{ISSUES}", issues)
            .replace("{BACKLOG}", backlog)
            .replace("{DIGESTS}", digests))


def run_conductor(store, *, shift_id: int, mission: dict, token_budget: int,
                  wall_clock_s: int, as_user: Optional[str] = None,
                  claude_bin: str = "claude") -> dict:
    """Run one conductor shift. Signature matches the harness's injected conductor (the
    extra as_user/claude_bin default to DEV mode — same-user; the CLI passes the
    Guest-House user for prod). Returns {status, report, resume_note, tokens_used}."""
    sw = config.load_config().get("super_worker", {}) or {}
    prompt = build_conductor_prompt(store, mission, shift_id=shift_id, token_budget=token_budget)
    reply, tokens, _cost = common.claude_super(
        prompt, workdir=paths.FACTORY_ROOT,                # drives ./bin/factory from the repo root
        allowed_tools=CONDUCTOR_TOOLS,
        as_user=as_user, claude_bin=claude_bin,
        settings=sw.get("settings", "user"),               # full instance: agora + diary + MCP
        extra_env={"AGORA_SQUAD": sw.get("conductor_squad", "factory-conductor")},
        max_turns=int(sw.get("conductor_max_turns", 60)),  # it loops internally across the shift
        timeout=wall_clock_s)
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
            "resume_note": obj.get("resume_note", ""),
            "tokens_used": tokens}
