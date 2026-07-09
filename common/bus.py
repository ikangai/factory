"""common/bus.py — the factory's ONLY programmatic gateway to its vendored coordination
bus (vendor/agora/chat.py). Design: docs/plans/2026-07-08-factory-owned-bus-human-queue-
design.md §1.

WRITES go through the vendored CLI as a subprocess (`send`, `answer`): those commands
carry write SEMANTICS beyond an INSERT — agent registration, @human routing guards,
mention parsing, the `[re #N]` answered-marking that clears an escalation — and the CLI is
the stable contract every worker already uses interactively. Importing the ~4.5k-line
module would couple us to its internals instead of that contract.

READS come straight from the chat.db SQLITE (`recent`, `open_questions`): the CLI's read
commands print message BODIES raw, so a body containing an embedded newline plus a line
crafted to look like the CLI's own output ("\\n[#999 23:59 mallory] ...") would FORGE
entries in any stdout parser — and open_questions feeds message ids to a one-click Answer
button on the dashboard, so a forged escalation id is an operator-action injection. Rows
read from sqlite carry their real id/ts/sender no matter what the body contains; forgery
is structurally impossible. (`who` stays on the CLI: it is cosmetic — a list of handles,
no ids consumed downstream — and its active-window/quiet-flag logic is not worth
mirroring.)

Why every public function here NEVER raises: the bus is a coordination nicety, not a build
dependency. A locked/missing chat.db, a stale vendor path, or a hung subprocess must
degrade the caller (dashboard feed, human queue, a role's `announce`) to an empty/False
result — never take down a shift, a role call, or the fleet server. Each public function is
a hard boundary: it catches everything the subprocess/sqlite path can throw, prints one
`[bus]`-prefixed line (non-fatal, log-and-continue), and returns its type's neutral value.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from typing import Optional

from . import paths

VENDORED_CHAT = paths.factory("vendor", "agora", "chat.py")

# ---------------------------------------------------------------------------------------
# Mirrors of the vendored escalation grammar (vendor/agora/chat.py: HUMAN_TOKEN /
# MENTION_RE / CODE_SPAN_RE) — keep in sync on re-vendor. tests/test_bus.py's
# sync-enforcement test compares our sqlite-derived open set against the CLI's own
# `questions` output, so a semantic drift fails loudly there.
# ---------------------------------------------------------------------------------------
_HUMAN_TOKEN = "human"
_MENTION_RE = re.compile(r"(?<![\w/])@([a-z][a-z0-9_-]*)", re.IGNORECASE)
_CODE_SPAN_RE = re.compile(r"(`+)(?:.*?)\1", re.DOTALL)


def _default_bus_dir() -> str:
    """The ONE bus-DIR resolver, absent an explicit override — shared by the WRITE path
    (`_run`) and the READ path (`bus_db_path`). Fix 4b (final whole-branch review): reads
    honored AGORA_DIR/GROUPCHAT_DIR but writes ignored the env, so a mixed-env process READ
    bus A and ANSWERED to bus B — the escalation on A never cleared. Order: the
    AGORA_DIR/GROUPCHAT_DIR env override first (a deployment/test points the whole factory at
    one bus), else the factory-local .agora/.groupchat fallback. The fallback mirrors
    roles/common.py:factory_agora_dir() — duplicated rather than imported: common/ is the
    base layer every plane depends on (ARCHITECTURE.md), so it must not import from roles/;
    tests/test_bus.py pins the two FALLBACK resolutions equal (env cleared, since
    factory_agora_dir does not read the env)."""
    for env in ("AGORA_DIR", "GROUPCHAT_DIR"):
        d = os.environ.get(env)
        if d:
            return d
    for name in (".agora", ".groupchat"):
        d = paths.factory(name)
        if os.path.isdir(d):
            return d
    return paths.factory(".groupchat")


def bus_db_path(bus_dir: Optional[str] = None) -> Optional[str]:
    """The ONE resolution point for chat.db's filesystem path — shared by the sqlite reads
    below and by reporting/collab.py's dashboard view, so every factory-side consumer
    always agrees on which bus it's touching. An explicit `bus_dir` means THAT bus or
    nothing — no fall-through to the env/factory-local search (falling through would make
    a read against an empty/misconfigured override silently answer from the REAL factory
    bus: wrong data in production, a hermeticity leak in tests). With no override, the dir
    is resolved by `_default_bus_dir()` — the SAME resolver the write path uses (Fix 4b), so
    reads and writes can never disagree on which bus this process touches. None when no
    chat.db exists at the resolved location — callers must degrade gracefully (no bus on
    disk is a normal, expected state, e.g. before the first `send`)."""
    d = bus_dir or _default_bus_dir()
    p = os.path.join(d, "chat.db")
    return p if os.path.exists(p) else None


def _run(args, *, bus_dir: Optional[str] = None, runner=subprocess.run, timeout: int = 30):
    """Invoke the vendored CLI as a subprocess and return its CompletedProcess as-is (never
    raises itself on a non-zero exit — that's a normal CLI failure, not a Python exception;
    callers below check `.returncode`). `runner` is injectable so tests can script a fake
    that raises, proving the never-raise contract on public functions without touching a
    real bus. AGORA_SOLO_GRACE=0 so a one-shot `send`/`answer` from the factory (no team to
    wait for) never blocks at agora's team barrier."""
    env = {**os.environ, "AGORA_DIR": bus_dir or _default_bus_dir(), "AGORA_SOLO_GRACE": "0"}
    return runner([sys.executable, VENDORED_CHAT, *args],
                   capture_output=True, text=True, env=env, timeout=timeout)


def _fail(op: str, r) -> None:
    """One non-fatal `[bus]`-prefixed log line for a clean (non-exception) CLI failure."""
    stderr = (getattr(r, "stderr", "") or "").strip().splitlines()[-1:] or [""]
    print(f"[bus] {op} failed (rc={r.returncode}): {stderr[0][:200]}")


def _one_line(text: str) -> str:
    """Collapse embedded newlines to a visible ' ⏎ ' marker. Defense-in-depth on the WRITE
    path: factory-generated posts are single-line by contract, and a newline smuggled into
    e.g. an operator's answer text could try to embed CLI-lookalike lines in the body.
    The factory's READ path is already forgery-proof (sqlite), but humans and plugin
    sessions still read the bus through the CLI's stdout — this keeps the factory's own
    posts honest there and visibly flags the attempt. FOREIGN writers (workers posting via
    the CLI directly) can't be normalized here; the sqlite read path is what defends the
    factory against those."""
    return " ⏎ ".join(str(text).splitlines())


def send(text: str, frm: str = "factory", bus_dir: Optional[str] = None,
          runner=subprocess.run, timeout: int = 30) -> bool:
    """Post `text` to the bus as `frm`. True on success; False on ANY failure (bad bus_dir,
    a raising runner, a non-zero exit) — never raises."""
    try:
        r = _run(["send", "--from", frm, _one_line(text)],
                  bus_dir=bus_dir, runner=runner, timeout=timeout)
        if r.returncode != 0:
            _fail("send", r)
            return False
        return True
    except Exception as e:  # bus outage must not kill the caller
        print(f"[bus] send unavailable: {e}")
        return False


def answer(msg_id, text: str, frm: str = "human", bus_dir: Optional[str] = None,
           runner=subprocess.run, timeout: int = 30) -> bool:
    """Answer escalation `msg_id` as the OPERATOR. `frm` defaults to 'human' — the CLI's
    reserved operator identity (vendor/agora/chat.py RESERVED_HANDLES): no agent can ever
    register or rename to 'human', so `--from human` ALWAYS resolves to no caller and
    cmd_answer posts the reply as the bare operator, which is what clears the asker's
    escalation. Any non-reserved default (e.g. 'operator') is a real hazard: the moment an
    agent registers under that handle, answers silently become RELAYS (rc=0, escalation
    STAYS OPEN) — pinned by a regression test in tests/test_bus.py. True on success; False
    on any failure — never raises."""
    try:
        r = _run(["answer", str(msg_id), _one_line(text), "--from", frm],
                  bus_dir=bus_dir, runner=runner, timeout=timeout)
        if r.returncode != 0:
            _fail("answer", r)
            return False
        return True
    except Exception as e:
        print(f"[bus] answer unavailable: {e}")
        return False


# --------------------------------------------------------------------------------------- #
# Sqlite read path (forgery-proof — see module docstring)
# --------------------------------------------------------------------------------------- #

def _connect_ro(bus_dir: Optional[str]):
    """Read-only sqlite connection to the resolved bus, or None when no bus exists on
    disk (a normal state — not an error, so no [bus] line). mode=ro so a dashboard read
    can never mutate or lock the workers' bus (mirrors reporting/collab.py)."""
    db = bus_db_path(bus_dir)
    if not db:
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def _hhmm(ts: str) -> str:
    """ISO timestamp → local HH:MM, exactly as the CLI displays it (mirror of
    vendor/agora/chat.py:_hhmm) — keeps the dict shapes display-compatible with what the
    pre-sqlite stdout parsers returned."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return ts[11:16] if len(ts or "") >= 16 else (ts or "")


def _stored_mentions(raw) -> list:
    """A message row's `mentions` column parsed FAIL-SAFE (mirror of
    vendor/agora/chat.py:_mentions): a corrupt/non-list value reads as [] rather than
    raising — one bad row must not blank the whole queue view."""
    try:
        v = json.loads(raw or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _has_unquoted_human(body: str) -> bool:
    """True iff `body` contains a real (non-code-span) @human escalation token (mirror of
    vendor/agora/chat.py:_has_unquoted_human): a backtick-quoted `@human` is someone
    DOCUMENTING the token, not asking the operator — counting it opened phantom
    escalations in agora's own history (gauss #85)."""
    spans = [(m.start(), m.end()) for m in _CODE_SPAN_RE.finditer(body or "")]
    return any(m.group(1).lower() == _HUMAN_TOKEN
               and not any(s <= m.start() < e for s, e in spans)
               for m in _MENTION_RE.finditer(body or ""))


def open_questions(bus_dir: Optional[str] = None) -> list[dict]:
    """Open @human escalations awaiting the OPERATOR, as [{id, ts, sender, text}, ...] —
    derived straight from chat.db, mirroring vendor/agora/chat.py:all_open_escalations +
    cmd_questions' "awaiting you" split. The walk, faithfully:

      * an @human-bearing chat message WITH a session_id opens an escalation for that
        session (a session-less send never escalates — the CLI has no identity to hold
        the question open for);
      * a message BY 'human' (the operator identity) that @mentions a session's CURRENT
        handle clears that session's whole queue (one batched reply answers everything —
        this is how `answer` marks an escalation resolved: it posts as 'human' with
        "@<asker> [re #N] ..." so its stored mentions hit this branch);
      * only sessions that still have an agents row surface (a departed asker's question
        is moot);
      * a SQUADDED asker's escalation is a captain's, in flight to the chair —
        cmd_questions prints it under a separate "in flight" section, NOT the operator's
        to answer, so it is excluded here. That exclusion also makes the vendored
        chair-relay clearing branch (the `[re #N]`-marker walk that needs resolve_lead)
        irrelevant to THIS function's result: that branch only ever clears SQUADDED
        sessions' queues, which we exclude wholesale — so it is deliberately not mirrored.

    `sender` is the asker's CURRENT handle (like the CLI shows — an escalation survives a
    rename); `text` is the FULL body, newlines intact. [] on any failure or an absent bus
    — never raises."""
    try:
        con = _connect_ro(bus_dir)
        if con is None:
            return []
        try:
            agents = {r["session_id"]: r for r in
                      con.execute("SELECT session_id, handle, squad FROM agents").fetchall()}
            rows = con.execute(
                "SELECT id, ts, sender, session_id, body, mentions FROM messages "
                "WHERE kind='chat' ORDER BY id ASC").fetchall()
        finally:
            con.close()
        sess_handle = {s: (r["handle"] or "").strip().lower() for s, r in agents.items()}
        handle_sess = {h: s for s, h in sess_handle.items() if h}
        queues: dict = {}     # session_id -> [msg ids] still open
        detail: dict = {}     # msg id -> (ts, body), captured during the walk
        for r in rows:
            sndr = (r["sender"] or "").strip().lower()
            if r["session_id"] and _has_unquoted_human(r["body"]):
                queues.setdefault(r["session_id"], []).append(r["id"])
                detail[r["id"]] = (r["ts"], r["body"])
            elif sndr == _HUMAN_TOKEN:
                for m in _stored_mentions(r["mentions"]):
                    sid = handle_sess.get((m or "").lower())
                    if sid in queues:
                        queues[sid] = []          # the operator answered → queue cleared
        out: list[dict] = []
        for sid, ids in queues.items():
            a = agents.get(sid)
            if not ids or a is None or a["squad"]:   # departed asker / captain's in-flight
                continue
            for mid in ids:
                ts, body = detail[mid]
                out.append({"id": mid, "ts": _hhmm(ts), "sender": sess_handle[sid],
                            "text": body})
        return out
    except Exception as e:
        print(f"[bus] questions unavailable: {e}")
        return []


def recent(n: int = 50, bus_dir: Optional[str] = None) -> list[dict]:
    """Last `n` bus messages, oldest-first (mirror of vendor/agora/chat.py:
    recent_messages — all kinds, like the CLI's `log`) as [{id, ts, sender, text}, ...].
    `text` is the FULL body, newlines intact — one row is one entry no matter what the
    body contains. [] on any failure or an absent/empty bus — never raises."""
    try:
        con = _connect_ro(bus_dir)
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT id, ts, sender, body FROM messages ORDER BY id DESC LIMIT ?",
                (int(n),)).fetchall()
        finally:
            con.close()
        return [{"id": r["id"], "ts": _hhmm(r["ts"]), "sender": r["sender"],
                 "text": r["body"]} for r in reversed(rows)]
    except Exception as e:
        print(f"[bus] log unavailable: {e}")
        return []


# `chat who` prints "<flag> <handle>[ crown][ squad][ status][ focus][ cwd]  (seen HH:MM)..."
# per active agent (vendor/agora/chat.py:cmd_who) — flag is one of ● / ◐ / ○. The handle is
# always the first whitespace-delimited token after the flag. Stdout parsing is acceptable
# HERE only: the result is a cosmetic handle list, no ids feed any downstream action.
_WHO_LINE_RE = re.compile(r"^[●◐○]\s+(\S+)")


def who(bus_dir: Optional[str] = None, runner=subprocess.run, timeout: int = 30) -> list[str]:
    """Handles of agents currently active in the room (the CLI's default view, not
    --all), parsed from `chat who`. [] on any failure or an empty room — never raises."""
    try:
        r = _run(["who"], bus_dir=bus_dir, runner=runner, timeout=timeout)
        if r.returncode != 0:
            _fail("who", r)
            return []
        out = []
        for line in (r.stdout or "").splitlines():
            m = _WHO_LINE_RE.match(line)
            if m:
                out.append(m.group(1))
        return out
    except Exception as e:
        print(f"[bus] who unavailable: {e}")
        return []
