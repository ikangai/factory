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


def fetch_issues(repo: str, limit: int = 25) -> str:
    """The target's OPEN GitHub issues (via `gh`), as a compact bulleted list for the
    research prompt — so the researcher proposes against real filed problems, not only its
    own ideas. Best-effort: no repo / no gh / no network → '' (the researcher carries on)."""
    if not repo:
        return ""
    import json as _json
    import subprocess
    try:
        out = subprocess.run(["gh", "issue", "list", "-R", repo, "--state", "open",
                              "--limit", str(limit), "--json", "number,title,labels"],
                             capture_output=True, text=True, timeout=20)
        issues = _json.loads(out.stdout or "[]") if out.returncode == 0 else []
    except Exception:  # noqa: BLE001 — gh missing / offline / bad repo → skip silently
        return ""
    lines = []
    for it in issues:
        labels = ",".join(l.get("name", "") for l in it.get("labels", []))
        lines.append(f"- #{it.get('number')}: {it.get('title', '')}"
                     + (f"  [{labels}]" if labels else ""))
    return "\n".join(lines)


def build_research_prompt(store, mission: dict, *, limit: int, issues: str = "") -> str:
    digests = _bullets(store.unconsumed_digests(), lambda d: f"- {d['summary']}",
                       "(nothing shipped yet)")
    backlog = _bullets(store.list_tasks(status="open"), lambda t: f"- {t['title']}",
                       "(empty)")
    target = mission.get("target_repo") or config.target_repo_slug()    # robust fallback if unset
    return (common._load_prompt("research_feed")
            .replace("{MISSION}", mission.get("statement", ""))
            .replace("{TARGET_REPO}", target or "(none set)")
            .replace("{ISSUES}", issues or "(none fetched — propose from the code + the web)")
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

    issues = fetch_issues(mission.get("target_repo") or config.target_repo_slug())  # real filed problems
    prompt = build_research_prompt(store, mission, limit=limit, issues=issues)
    target_root = config.get_adapter().entry()[0]         # the REAL target repo (not the parent dir)
    reply, _tokens, _cost = common.claude_super(
        prompt, workdir=target_root,                      # reads the target to find real gaps
        allowed_tools=common.RESEARCHER_TOOLS,            # read + web + fan-out; NO Bash/edits
        as_user=as_user, claude_bin=claude_bin,
        settings=sw.get("settings", "user"),              # web + diary + MCP
        extra_env=common.worker_bus_env(sw.get("research_squad", "factory-research")),
        max_turns=int(sw.get("research_max_turns", 40)),
        timeout=int(sw.get("research_timeout_s", 900)))

    if reply.startswith("[claude -p"):    # transport failed/timed out — DON'T consume the
        return []                          # digests (they'd be lost); leave them for a retry

    obj = common._parse_obj(reply)
    if not isinstance(obj, dict):          # junk reply → no well-formed result; don't consume
        return []
    directions = obj.get("directions", [])

    added: list[dict] = []
    for d in directions[:limit]:
        title = (d.get("title") or "").strip() if isinstance(d, dict) else ""
        if not title or title.lower() in existing:        # skip blanks + backlog duplicates
            continue
        tid = f"task-{uuid.uuid4().hex[:8]}"
        store.add_task(tid, title, source="research", detail=(d.get("detail", "") or ""))
        existing.add(title.lower())
        added.append({"id": tid, "title": title})

    for dg in digests:                                    # well-formed result → close the loop
        store.mark_digest_consumed(dg["id"])
    return added
