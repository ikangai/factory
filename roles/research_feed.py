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
    from ..reporting import factory_memory                  # factory memory: prior lessons → context
    return (common._load_prompt("research_feed")
            .replace("{MISSION}", mission.get("statement", ""))
            .replace("{TARGET_REPO}", target or "(none set)")
            .replace("{ISSUES}", issues or "(none fetched — propose from the code + the web)")
            .replace("{DIGESTS}", digests)
            .replace("{BACKLOG}", backlog)
            .replace("{MEMORY}", factory_memory.memory_card(store, "researcher"))
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
    from ..research.focus import read_material                      # human-dropped pointers (Task 1.3)
    material = read_material(paths.factory("MISSION.md"))
    if material:                                                    # weigh what the human handed us
        prompt += ("\n\n## Material from the human (they dropped these — weigh them):\n" + material)
    target_root = config.get_adapter().entry()[0]         # the REAL target repo (not the parent dir)
    reply, tokens, cost = common.claude_super(
        prompt, workdir=target_root,                      # reads the target to find real gaps
        allowed_tools=common.RESEARCHER_TOOLS,            # read + web + fan-out; NO Bash/edits
        as_user=as_user, claude_bin=claude_bin,
        settings=sw.get("settings", "user"),              # web + diary + MCP
        extra_env=common.worker_bus_env(sw.get("research_squad", "factory-research")),
        max_turns=int(sw.get("research_max_turns", 40)),
        timeout=int(sw.get("research_timeout_s", 900)))
    # Ledger the researcher's spend (Task 0.5) before the sentinel — a failed/junk refill
    # still consumed tokens.
    store.add_budget("researcher", tokens, cost, notes="research refill",
                     shift_id=store.current_shift_id())

    if reply.startswith("[claude -p"):    # transport failed/timed out — DON'T consume the
        return []                          # digests (they'd be lost); leave them for a retry

    obj = common._parse_obj(reply)
    if not isinstance(obj, dict):          # junk reply → no well-formed result; don't consume
        return []
    directions = obj.get("directions", [])

    from ..reporting import factory_memory                  # record the researcher's emitted lessons
    for lesson in factory_memory.coerce_learnings(obj.get("learnings")):  # guard non-list JSON drift
        factory_memory.record_learning(store, "researcher", lesson,
                                       shift_id=store.current_shift_id())

    from ..reporting import scope_check                    # fold the emitted spec into the detail
    added: list[dict] = []
    complete = 0
    for d in directions[:limit]:
        title = (d.get("title") or "").strip() if isinstance(d, dict) else ""
        if not title or title.lower() in existing:        # skip blanks + backlog duplicates
            continue
        spec = {"target_surface": d.get("target_surface", ""),
                "acceptance": d.get("acceptance", ""), "out_of_scope": d.get("out_of_scope", "")}
        detail = (d.get("detail", "") or "") + scope_check.spec_detail_suffix(spec)
        tid = f"task-{uuid.uuid4().hex[:8]}"
        store.add_task(tid, title, source="research", detail=detail, spec=spec)  # typed spec (GSD #2)
        existing.add(title.lower())
        added.append({"id": tid, "title": title})
        if scope_check.is_spec_complete(spec):
            complete += 1
    if added:                                             # spec-lint: surface authorship quality
        print(f"[research] {complete}/{len(added)} new directions are spec-complete "
              f"(target_surface + acceptance)", flush=True)

    for dg in digests:                                    # well-formed result → close the loop
        store.mark_digest_consumed(dg["id"])
    return added
