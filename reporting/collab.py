"""Read the agora bus (.groupchat/chat.db) so the dashboard can show the COLLABORATION layer
of a shift: who's working, who @mentions whom, and whether agora is actively used at all.

The factory's workers are full Claude instances on a shared agora bus — conductors hand
shifts off to each other, flag findings, and coordinate. That coordination is invisible on
the board (which only reads the blackboard); this module reads the bus (read-only, degrades
gracefully when absent) and surfaces the messages, the agents, and the who-talks-to-whom
edges."""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

from ..common import paths


def agora_db_path() -> Optional[str]:
    """Locate the agora bus DB the way the plugin does: $AGORA_DIR/$GROUPCHAT_DIR override,
    else the repo-local .agora/.groupchat. None when there's no bus."""
    for env in ("AGORA_DIR", "GROUPCHAT_DIR"):
        d = os.environ.get(env)
        if d and os.path.exists(os.path.join(d, "chat.db")):
            return os.path.join(d, "chat.db")
    for name in (".agora", ".groupchat"):
        p = os.path.join(paths.FACTORY_ROOT, name, "chat.db")
        if os.path.exists(p):
            return p
    return None


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def agora_state(limit: int = 14) -> dict:
    """The collaboration view: {active, total, mentions, senders, agents, messages, edges}.
    `active` answers 'is agora actually used here?'. Read-only + crash-proof."""
    empty = {"active": False, "total": 0, "mentions": 0, "senders": 0,
             "live": 0, "agents": [], "messages": [], "edges": []}
    db = agora_db_path()
    if not db:
        return empty
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        total = con.execute("SELECT count(*) FROM messages WHERE kind='chat'").fetchone()[0]
        mentions = con.execute("SELECT count(*) FROM messages WHERE mentions != '[]'").fetchone()[0]
        senders = con.execute("SELECT count(DISTINCT sender) FROM messages WHERE kind='chat'").fetchone()[0]
        recent = con.execute(
            "SELECT ts, sender, kind, body, mentions FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        edge_rows = con.execute("SELECT sender, mentions FROM messages WHERE mentions != '[]'").fetchall()
        agent_rows = con.execute(
            "SELECT handle, squad, status, focus, pid, model FROM agents ORDER BY last_seen DESC").fetchall()
        con.close()
    except sqlite3.Error:
        return empty

    messages = []
    for r in recent:
        try:
            to = json.loads(r["mentions"] or "[]")
        except (ValueError, TypeError):
            to = []
        messages.append({"ts": r["ts"], "sender": r["sender"], "kind": r["kind"],
                         "to": to, "body": r["body"]})

    edges: dict = {}                                    # who @mentions whom, across the whole bus
    for r in edge_rows:
        try:
            for m in json.loads(r["mentions"] or "[]"):
                if m and m != r["sender"]:
                    edges[(r["sender"], m)] = edges.get((r["sender"], m), 0) + 1
        except (ValueError, TypeError):
            continue
    edge_list = sorted(({"from": a, "to": b, "n": n} for (a, b), n in edges.items()),
                       key=lambda e: -e["n"])[:12]

    agents = [{"handle": a["handle"], "squad": a["squad"] or "", "status": a["status"] or "",
               "focus": a["focus"] or "", "model": a["model"] or "", "live": _alive(a["pid"])}
              for a in agent_rows]
    agents.sort(key=lambda a: not a["live"])        # LIVE agents first; the rest are historical
    live = sum(1 for a in agents if a["live"])

    return {"active": total > 0, "total": total, "mentions": mentions, "senders": senders,
            "live": live, "agents": agents, "messages": messages, "edges": edge_list}
