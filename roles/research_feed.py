"""The research feed (design: docs/plans/2026-06-25-conductor-loop-design.md, step 4).

The generative engine: a web-enabled RESEARCHER super-worker proposes bounded directions
toward the mission — informed by what shipped (the digests) and de-duplicated against the
open backlog — and they land in the backlog as `source='research'` tasks. Consuming the
digests closes the research<->dev feedback loop.

Hermetic in tests (claude_super injected); the live researcher runs only when the factory
runs (no Bash/edits — it investigates and proposes, the developer fleet builds)."""
from __future__ import annotations

import uuid
from typing import Optional

from ..common import config, paths
from . import common


def _bullets(rows, fmt, empty: str) -> str:
    return "\n".join(fmt(r) for r in rows) or empty


def build_research_prompt(store, mission: dict, *, limit: int) -> str:
    digests = _bullets(store.unconsumed_digests(), lambda d: f"- {d['summary']}",
                       "(nothing shipped yet)")
    backlog = _bullets(store.list_tasks(status="open"), lambda t: f"- {t['title']}",
                       "(empty)")
    return (common._load_prompt("research_feed")
            .replace("{MISSION}", mission.get("statement", ""))
            .replace("{TARGET_REPO}", mission.get("target_repo", "") or "(none set)")
            .replace("{DIGESTS}", digests)
            .replace("{BACKLOG}", backlog)
            .replace("{LIMIT}", str(limit)))


def propose_directions(store, *, limit: int = 5, as_user: Optional[str] = None,
                       claude_bin: str = "claude") -> list[dict]:
    """Run the researcher super-worker; add its non-duplicate directions to the backlog as
    research tasks; mark the shipped digests consumed. Returns the tasks added. No active
    mission → no-op (nothing to steer toward)."""
    mission = store.active_mission()
    if not mission:
        return []
    sw = config.load_config().get("super_worker", {}) or {}
    digests = store.unconsumed_digests()
    existing = {t["title"].strip().lower() for t in store.list_tasks(status="open")}

    prompt = build_research_prompt(store, mission, limit=limit)
    reply, _tokens, _cost = common.claude_super(
        prompt, workdir=paths.CLIVE_ROOT,                 # reads the target to find real gaps
        allowed_tools=common.RESEARCHER_TOOLS,            # read + web + fan-out; NO Bash/edits
        as_user=as_user, claude_bin=claude_bin,
        settings=sw.get("settings", "user"),              # web + diary + MCP
        extra_env={"AGORA_SQUAD": sw.get("research_squad", "factory-research")},
        max_turns=int(sw.get("research_max_turns", 40)),
        timeout=int(sw.get("research_timeout_s", 900)))

    obj = common._parse_obj(reply)
    directions = obj.get("directions", []) if isinstance(obj, dict) else []

    added: list[dict] = []
    for d in directions[:limit]:
        title = (d.get("title") or "").strip() if isinstance(d, dict) else ""
        if not title or title.lower() in existing:        # skip blanks + backlog duplicates
            continue
        tid = f"task-{uuid.uuid4().hex[:8]}"
        store.add_task(tid, title, source="research", detail=(d.get("detail", "") or ""))
        existing.add(title.lower())
        added.append({"id": tid, "title": title})

    for dg in digests:                                    # close the research<->dev loop
        store.mark_digest_consumed(dg["id"])
    return added
