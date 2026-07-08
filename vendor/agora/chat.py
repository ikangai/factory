#!/usr/bin/env python3
"""agora (formerly groupchat) — a shared bus for parallel Claude Code instances on one repo.

All instances working on the same repository share a single SQLite database on
disk. Each instance ("agent") gets a short, memorable handle (e.g. ``curie``).
Agents post messages, @mention each other, and track which messages they have
already seen via a per-agent read cursor.

This file is BOTH:
  * an importable module (the hook scripts ``import chat`` and call functions), and
  * a command-line tool (``python3 chat.py <command> ...``).

Env + storage honor the new ``AGORA_*`` / ``.agora`` names with the legacy
``GROUPCHAT_*`` / ``.groupchat`` as a fallback (new spelling wins) — see ``_env`` /
``_room_dirname``. Storage location resolution (first match wins):
  1. ``$AGORA_DIR`` / ``$GROUPCHAT_DIR``               — explicit override
  2. ``<git common dir parent>/{.agora|.groupchat}``   — shared across all worktrees
  3. ``$CLAUDE_PROJECT_DIR/{.agora|.groupchat}``        — project root when run via a hook
  4. ``<cwd>/{.agora|.groupchat}``                      — fallback

Design goals: zero third-party dependencies (Python 3 stdlib only), safe under
concurrent access (WAL + busy timeout), and never crash a Claude session — the
hook wrappers swallow all errors.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# Memorable handles, assigned in order to each new agent. Scientists &
# mathematicians; if the pool is exhausted we fall back to ``agent-N``.
HANDLE_POOL = [
    "ada", "turing", "hopper", "lovelace", "curie", "bohr", "tesla", "newton",
    "euler", "gauss", "noether", "ramanujan", "fermi", "feynman", "dirac",
    "shannon", "babbage", "kepler", "galois", "pascal", "fourier", "laplace",
    "hilbert", "riemann", "cantor", "godel", "church", "knuth", "dijkstra",
    "liskov", "kay", "ritchie", "thompson", "torvalds", "berners", "engelbart",
]

MENTION_RE = re.compile(r"(?<![\w/])@([a-z][a-z0-9_-]*)", re.IGNORECASE)
# Reserved mention: the human operator. Hub-and-spoke routing (the hierarchy
# substrate) funnels worker→human questions through the lead, so @human is special
# — it can never be an agent handle, and only the lead may address it directly.
HUMAN_TOKEN = "human"
# Broadcast tokens: ``@team`` / ``@all`` expand to every active teammate, so a
# broadcast actually blocks everyone's Stop (a plain message doesn't). Reserved like
# @human so no agent can be named ``team``/``all`` and shadow the broadcast.
BROADCAST_TOKENS = frozenset({"team", "all"})
RESERVED_HANDLES = frozenset({HUMAN_TOKEN}) | BROADCAST_TOKENS
# Markdown code spans (matched backtick runs, inline or fenced). The @human guard
# leaves a *quoted* token alone so writing `@human` in docs/help/chat is not an
# escalation — backreference \1 requires the closing run to match the opening run.
CODE_SPAN_RE = re.compile(r"(`+)(?:.*?)\1", re.DOTALL)
# Rule citations (governance): case-SENSITIVE `R` + a non-zero-leading number as a
# whole token, with MENTION_RE's boundary guard. parse_rules() also rejects the
# R-squared family (no R0, no leading zeros, no `R2-squared`).
RULE_RE = re.compile(r"(?<![\w/])R([1-9]\d*)\b")
ACTIVE_WINDOW_SECONDS = 15 * 60  # an agent is "active" if seen within 15 min
QUIET_SECONDS = 10 * 60          # an active, not-done agent that hasn't chatted in this
                                 # long is flagged "quiet" (◐) — a soft stuck/heads-down
                                 # signal. Override with GROUPCHAT_QUIET_SECS.

# --- Team barrier (parallel /goal coordination) -------------------------------
# A finished agent does not exit on its own; it waits at a barrier until the
# whole team is done. These tune that wait (see docs/plans/*-team-barrier-*).
DONE_STATUS = "done"
STARTUP_GRACE_SECONDS = 90        # how long to wait for a staggered launch when
                                  # the team size is unknown (no GROUPCHAT_TEAM_SIZE
                                  # / `expect`), before the barrier may complete
SOLO_GRACE_SECONDS = 10           # a lone, undeclared agent only settles this long
                                  # (catch a co-launched teammate a beat behind
                                  # registering) before the barrier may complete — so
                                  # "working solo" never means waiting 90s for nobody.
                                  # Override with GROUPCHAT_SOLO_GRACE (0 = no wait).
MAX_PARK_SECONDS = 2 * 60 * 60    # ceiling: release a parked agent after this much
                                  # continuous waiting regardless of the barrier,
                                  # so a mis-set team size can't hang everyone forever.
                                  # Override per-run with GROUPCHAT_MAX_PARK (seconds;
                                  # 0 = release immediately). Raised from 30m so long
                                  # goals don't drop teammates mid-run.


# --------------------------------------------------------------------------- #
# Storage location & connection
# --------------------------------------------------------------------------- #
def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var by its SUFFIX, honoring the new ``AGORA_*`` names with the legacy
    ``GROUPCHAT_*`` as a fallback (new spelling wins). Accepts either full spelling — the
    prefix is stripped — so ``_env('SQUAD')`` / ``_env('GROUPCHAT_SQUAD')`` both check
    ``AGORA_SQUAD`` then ``GROUPCHAT_SQUAD``. This is the whole backward-compat seam for
    the groupchat→agora rename: every env read goes through here."""
    suffix = name.split("_", 1)[1] if name.startswith(("AGORA_", "GROUPCHAT_")) else name
    v = os.environ.get("AGORA_" + suffix)
    if v is None:
        v = os.environ.get("GROUPCHAT_" + suffix)
    return default if v is None else v


def _room_dirname(anchor: str) -> str:
    """The runtime room dir under ``anchor``: the new ``.agora`` by default, but an
    EXISTING legacy ``.groupchat`` room (its chat.db present) keeps being used so the
    rename never strands an old room. An existing ``.agora`` wins over a legacy one."""
    if os.path.isfile(os.path.join(anchor, ".agora", "chat.db")):
        return ".agora"
    if os.path.isfile(os.path.join(anchor, ".groupchat", "chat.db")):
        return ".groupchat"
    return ".agora"


def store_dir() -> str:
    """Return the directory holding the shared chat database for this repo."""
    env = _env("DIR")  # AGORA_DIR, else legacy GROUPCHAT_DIR
    if env:
        return os.path.abspath(env)

    # All worktrees of one repo share a single git "common dir"; anchoring the
    # room there means agents in different worktrees still see each other.
    try:
        # --path-format=absolute (git >= 2.31) avoids a relative ".git" that would
        # resolve against the wrong cwd; abspath() is a fallback for older git.
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=3,
        )
        out = common.stdout.strip()
        if common.returncode != 0 or not out:
            common = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, timeout=3,
            )
            out = common.stdout.strip()
        if common.returncode == 0 and out:
            git_common = os.path.abspath(out)
            # parent of the .git dir == main worktree root
            root = os.path.dirname(git_common) or os.getcwd()
            return os.path.join(root, _room_dirname(root))
    except Exception:
        pass

    cpd = os.environ.get("CLAUDE_PROJECT_DIR")
    if cpd and os.path.isdir(cpd):
        root = os.path.abspath(cpd)
        return os.path.join(root, _room_dirname(root))

    return os.path.join(os.getcwd(), _room_dirname(os.getcwd()))


def db_path() -> str:
    return os.path.join(store_dir(), "chat.db")


def repo_root() -> str:
    """Repo root that holds CONSTITUTION.md — the parent of the room dir, so the
    durable law sits beside ``.groupchat`` and resolves at the SAME git anchor as
    the bus. Mirrors ``store_dir()``'s resolution chain minus the trailing
    ``.groupchat`` join (NOT ``git rev-parse --show-toplevel``, which is per-worktree)."""
    return os.path.dirname(store_dir())


def connect() -> sqlite3.Connection:
    d = store_dir()
    os.makedirs(d, exist_ok=True)
    # In a plugin install the repo's .groupchat/ holds only the runtime db, so
    # drop a gitignore on first creation. Guarded by exists() — a committed
    # .gitignore (e.g. this dev repo's) is left untouched.
    gi = os.path.join(d, ".gitignore")
    if not os.path.exists(gi):
        try:
            with open(gi, "w") as fh:
                fh.write("# group chat runtime — do not commit\n*\n")
        except Exception:
            pass
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _add_column_if_missing(conn, table: str, col: str, decl: str) -> None:
    have = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            sender      TEXT    NOT NULL,
            session_id  TEXT,
            kind        TEXT    NOT NULL DEFAULT 'chat',
            body        TEXT    NOT NULL,
            mentions    TEXT    NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS agents (
            session_id   TEXT PRIMARY KEY,
            handle       TEXT UNIQUE NOT NULL,
            cwd          TEXT,
            pid          INTEGER,
            status       TEXT,
            first_seen   TEXT,
            last_seen    TEXT,
            last_read_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_id ON messages(id);
        -- Speeds the kind-filtered chronological scans (the escalation gate, cite
        -- harvest, last-chat lookup) so they're an index range, not a full-table scan —
        -- the first thing that bites at dozens of agents on one bus (the Stop park loop
        -- re-walks kind='chat' every ~2s). Additive; no behaviour change.
        CREATE INDEX IF NOT EXISTS idx_messages_kind_id ON messages(kind, id);
        CREATE TABLE IF NOT EXISTS rule_cites (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            rule_id    TEXT    NOT NULL,
            sender     TEXT    NOT NULL,
            message_id INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rule_cites_rule ON rule_cites(rule_id);
        CREATE TABLE IF NOT EXISTS motions (
            id          INTEGER PRIMARY KEY,
            ts          TEXT    NOT NULL,
            proposer    TEXT    NOT NULL,
            target      TEXT    NOT NULL,
            op          TEXT    NOT NULL,
            change      TEXT,
            because     TEXT    NOT NULL,
            base_text   TEXT,
            new_id      TEXT,
            status      TEXT    NOT NULL DEFAULT 'open'
        );
        CREATE TABLE IF NOT EXISTS votes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT NOT NULL,
            motion_id     INTEGER NOT NULL,
            voter_session TEXT NOT NULL,
            voter_handle  TEXT NOT NULL,
            vote          TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT    NOT NULL,
            title   TEXT    NOT NULL,
            owner   TEXT,
            status  TEXT    NOT NULL DEFAULT 'open',
            paths   TEXT,
            creator TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE TABLE IF NOT EXISTS claims (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,
            session_id TEXT    NOT NULL,
            handle     TEXT    NOT NULL,
            glob       TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_claims_session ON claims(session_id);
        CREATE TABLE IF NOT EXISTS dismissed (
            session_id TEXT PRIMARY KEY,
            ts         TEXT
        );
        """
    )
    # Token-usage columns (added post-v1; guarded so old dbs upgrade in place).
    for _col in ("in_tokens", "out_tokens", "cache_read_tokens", "cache_create_tokens"):
        _add_column_if_missing(conn, "agents", _col, "INTEGER NOT NULL DEFAULT 0")
    # Optional short heading for an add-motion's Article (else the (new rule) placeholder).
    _add_column_if_missing(conn, "motions", "title", "TEXT")
    # Which parliamentary session a motion/decision-item belongs to (governance framing).
    _add_column_if_missing(conn, "motions", "session_id", "TEXT")
    # The authoring session of a message (the escalation gate keys on it). Part of the
    # CREATE TABLE, but a pre-session_id legacy room (.groupchat) needs the guarded ALTER
    # too — without it every send() raises "no such column: session_id" (the messages
    # table's only post-v1 column; old dbs are exactly the ones _room_dirname prefers).
    _add_column_if_missing(conn, "messages", "session_id", "TEXT")
    # Spawn lineage (Phase 3): how deep this agent was spawned and by whom — the
    # autonomous-spawn audit trail. Guarded so old dbs upgrade in place.
    _add_column_if_missing(conn, "agents", "spawn_depth", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "agents", "spawned_by", "TEXT")
    # Per-agent "what I'm working on now" (Phase 4) — distinct from the barrier status.
    _add_column_if_missing(conn, "agents", "focus", "TEXT")
    # Barrier capability (Phase 5): 1 if this agent has a Stop hook that marks done /
    # parks (Claude, Codex), 0 for a non-hook host (opencode/generic) that never marks
    # done — so a hook-less agent can't hold a hook team at the barrier. Default 1.
    _add_column_if_missing(conn, "agents", "parks", "INTEGER NOT NULL DEFAULT 1")
    # Sub-team sharding: which SQUAD an agent belongs to (NULL = the single global room).
    # The team BARRIER scopes per squad so a finished squad tears down independently; the
    # lead / @human funnel stays global. Set via $GROUPCHAT_SQUAD / `squad` / bootstrap.
    _add_column_if_missing(conn, "agents", "squad", "TEXT")
    # The agent's MODEL (NULL = unknown). Used ONLY to annotate the advisory vote tally
    # with model DIVERSITY — a homogeneous-fleet sweep is flagged low-independence so the
    # human ratifier can see the capture risk. It never gates or binds. Set via
    # $AGORA_MODEL / the `model` verb / `bootstrap --model` (a bridge adapter MAY set it
    # for Codex/opencode; none does yet, so a bridged voter is unknown-model).
    _add_column_if_missing(conn, "agents", "model", "TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hhmm(ts: str) -> str:
    """Render an ISO timestamp as local HH:MM for compact display."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return ts[11:16] if len(ts) >= 16 else ts


def parse_mentions(body: str) -> list[str]:
    """Handles @mentioned in ``body``, EXCLUDING any inside a markdown code span:
    quoting `` `@ada` `` in docs/help/chat is *discussing* the handle, not pinging it.
    This is the single home for that rule — routing, the inbox, the Stop @mention
    block, and escalation detection all key off the stored mentions, so a quoted
    handle never spuriously pings, wakes, blocks, or escalates anyone (the dogfooding
    sharp-edge from chat #85/#90). (``_code_span_ranges``/``_in_spans`` are defined
    with the hierarchy helpers below; resolved at call time.)"""
    spans = _code_span_ranges(body)
    return sorted({m.group(1).lower() for m in MENTION_RE.finditer(body)
                   if not _in_spans(m.start(), spans)})


def parse_rules(body: str) -> list[str]:
    """Harvest rule-id citations (``R<n>``) from a message body. A tolerant signal,
    not a ledger: case-sensitive, boundary-guarded, no R0/leading-zero, and skips
    the R-squared family (trailing ``=``/``^``/``²`` or ``-squared``/`` squared``)
    so chatter like ``R2=0.99`` or ``R2-squared`` is not a cite."""
    out = set()
    for m in RULE_RE.finditer(body):
        tail = body[m.end():m.end() + 9]
        if tail[:1] in ("=", "^", "²") or re.match(r"[-\s]squared\b", tail):
            continue
        out.add("R" + m.group(1))
    return sorted(out)


def _now_epoch() -> float:
    return time.time()


def iso_age_seconds(ts: str | None) -> float:
    """Seconds elapsed since an ISO timestamp; +inf if unparseable/empty."""
    if not ts:
        return float("inf")
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return _now_epoch() - dt.timestamp()
    except Exception:
        return float("inf")


def _is_active(last_seen: str | None) -> bool:
    return iso_age_seconds(last_seen) <= ACTIVE_WINDOW_SECONDS


def _env_int(name: str, default: int | None = None) -> int | None:
    """Parse an int from env ``name``; return ``default`` when unset/empty/invalid.

    ONE definition serves both call styles — the barrier callers
    (``expected_team_size`` / ``max_park_seconds``) omit ``default`` and treat the
    resulting ``None`` as "unset", while the constitution callers pass an explicit
    fallback. A second, two-arg redefinition of this function once shadowed the
    original and made every no-default caller raise ``TypeError`` — which
    ``stop.py`` swallowed (fail-open), silently killing the team barrier. Keep it
    single. (See .dev-diary/2026-06-07-test-harness-and-the-dead-barrier.md.)
    """
    v = _env(name)  # AGORA_* with GROUPCHAT_* legacy fallback
    if v in (None, ""):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Token accounting (best-effort; from the local Claude Code transcript)
# --------------------------------------------------------------------------- #
TOKEN_FIELDS = ("in_tokens", "out_tokens", "cache_read_tokens", "cache_create_tokens")
_USAGE_MAP = {
    "in_tokens": "input_tokens",
    "out_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_create_tokens": "cache_creation_input_tokens",
}


def sum_transcript_tokens(transcript_path: str | None) -> dict:
    """Sum per-turn ``usage`` across assistant messages in a Claude Code
    transcript (JSONL). Returns the four cumulative counts; zeros on any error.

    Approximate by design (see docs/plans/2026-06-02-groupchat-plugin-design.md):
    the transcript's input/output counts can undercount; cache counts are
    reliable. Good enough for *relative* per-agent burn and idle verification.
    """
    totals = {k: 0 for k in TOKEN_FIELDS}
    if not transcript_path or not os.path.isfile(transcript_path):
        return totals
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                usage = (rec.get("message") or {}).get("usage") or {}
                if not usage:
                    continue
                for col, src in _USAGE_MAP.items():
                    totals[col] += int(usage.get(src) or 0)
    except Exception:
        pass
    return totals


# --------------------------------------------------------------------------- #
# Meta key/value store (small bits of room-wide state)
# --------------------------------------------------------------------------- #
def get_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def del_meta(conn, key: str) -> None:
    conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    conn.commit()


# --------------------------------------------------------------------------- #
# Agent registry & identity
# --------------------------------------------------------------------------- #
def _assign_handle(conn: sqlite3.Connection, preferred: str | None = None) -> str:
    # Only *active* handles are taken — a closed/idle session's name is free to
    # recycle, so the pool doesn't march forward (ada→turing→…→agent-N) and the
    # agents table doesn't grow unbounded. An active session never loses its handle.
    # Reserved names (e.g. "human") stay taken so the @human escalation token can
    # never collide with a real agent handle.
    taken = {a["handle"] for a in active_agents(conn)} | set(RESERVED_HANDLES)
    if preferred:
        cand = re.sub(r"[^a-z0-9_-]", "", preferred.lower()) or "agent"
        if cand not in taken:
            return cand
        i = 2
        while f"{cand}-{i}" in taken:
            i += 1
        return f"{cand}-{i}"
    for h in HANDLE_POOL:
        if h not in taken:
            return h
    i = 1
    while f"agent-{i}" in taken:
        i += 1
    return f"agent-{i}"


def register(conn: sqlite3.Connection, session_id: str, cwd: str | None = None,
             pid: int | None = None, handle: str | None = None,
             status: str | None = None, parks: bool = True) -> str:
    """Idempotently ensure an agent row for ``session_id``; return its handle.

    Re-running (e.g. on every prompt) refreshes ``last_seen`` without changing
    the handle, so an agent keeps a stable identity for its whole session.

    ``parks`` records barrier capability at first registration: a non-hook host
    (opencode/generic, ``--no-barrier``) sets ``parks=False`` so it never holds a hook
    team at the barrier (see ``team_done``).
    """
    row = conn.execute(
        "SELECT handle FROM agents WHERE session_id = ?", (session_id,)
    ).fetchone()
    ts = now_iso()
    if row:
        sets = ["last_seen = ?"]
        params: list = [ts]
        if cwd is not None:
            sets.append("cwd = ?"); params.append(cwd)
        if pid is not None:
            sets.append("pid = ?"); params.append(pid)
        if status is not None:
            sets.append("status = ?"); params.append(status)
        if not parks:
            # One-way downgrade: a re-register declaring --no-barrier marks a non-hook
            # host even if its first registration predated the flag. Never the reverse —
            # a default (parks=True) refresh must not re-upgrade a parks=0 agent (a hook
            # agent's every-turn refresh defaults True but its row is already 1).
            sets.append("parks = 0")
        params.append(session_id)
        conn.execute(f"UPDATE agents SET {', '.join(sets)} WHERE session_id = ?", params)
        conn.commit()
        return row["handle"]

    # New agent: assign a handle, retrying on the rare race where two sessions
    # grab the same one concurrently.
    for _ in range(len(HANDLE_POOL) + 50):
        h = _assign_handle(conn, handle)
        # Reclaim a recycled name: if this handle is held by an INACTIVE agent
        # (a closed/idle session), drop the dead row so the UNIQUE INSERT below
        # succeeds — that's how a restarted shell keeps its GROUPCHAT_HANDLE and how
        # pool names get reused. _assign_handle never returns an actively-held handle,
        # so we only ever delete a dead identity; the `_is_active` guard means a race
        # that revived the holder is left alone and the INSERT just retries.
        stale = conn.execute(
            "SELECT session_id, last_seen FROM agents WHERE handle = ?", (h,)
        ).fetchone()
        if stale and not _is_active(stale["last_seen"]):
            # Re-assert staleness IN the DELETE (not just the check above): if the
            # holder revived in between — a TOCTOU where its own re-register /
            # set_status / mark_read refreshed last_seen — the guarded DELETE matches
            # 0 rows, the INSERT below collides on the UNIQUE handle, and the retry
            # loop falls through to a different name. So an active session can never
            # lose its handle (or its read cursor) to a newcomer reusing the name.
            cutoff = datetime.fromtimestamp(
                _now_epoch() - ACTIVE_WINDOW_SECONDS, timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            cur = conn.execute(
                "DELETE FROM agents WHERE session_id = ? AND (last_seen IS NULL OR last_seen < ?)",
                (stale["session_id"], cutoff),
            )
            # Only if a row was actually retired, and it held a lead pointer, drop the
            # stale pointer so a name-reuser doesn't inherit leadership — resolve_lead's
            # floor re-elects instead. Covers BOTH the global chair ('lead') and any squad
            # captaincy ('lead:<squad>') the reclaimed handle held (council layer).
            if cur.rowcount:
                for (k,) in conn.execute(
                        "SELECT key FROM meta WHERE key='lead' OR key LIKE 'lead:%'").fetchall():
                    if (get_meta(conn, k) or "").strip().lower() == h:
                        del_meta(conn, k)
        try:
            conn.execute(
                "INSERT INTO agents(session_id, handle, cwd, pid, status, "
                "first_seen, last_seen, last_read_id, spawn_depth, spawned_by, parks, "
                "squad, model) "
                "VALUES (?,?,?,?,?,?,?, (SELECT COALESCE(MAX(id),0) FROM messages), ?, ?, ?, ?, ?)",
                (session_id, h, cwd, pid, status, ts, ts,
                 current_spawn_depth(), _env("SPAWNED_BY") or None,
                 1 if parks else 0, _norm_squad(_env("SQUAD")), _norm_model(_env("MODEL"))),
            )
            conn.commit()
            _clear_stale_team_size(conn, session_id)
            _clear_stale_standdown(conn, session_id)
            return h
        except sqlite3.IntegrityError:
            handle = None  # collided; let the pool pick the next free one
            continue
    raise RuntimeError("could not assign a unique handle")


def rename_agent(conn: sqlite3.Connection, session_id: str,
                 new_handle: str) -> tuple[str, str]:
    """Change ``session_id``'s handle to ``new_handle`` in place; return (old, new).

    Mirrors register()'s identity rules so a rename can't break the handle
    invariants: sanitized, reserved-rejecting, and collision-safe — an *active*
    holder blocks the rename (the caller picks another name), while an *inactive*
    holder is reclaimed with the same TOCTOU-guarded delete register() uses. The
    agents table is keyed by session_id and every hook reads/advances the cursor by
    session_id, so the read cursor, token counters, and message delivery all survive
    the handle change untouched. Raises ValueError on an empty/reserved/taken target.
    """
    agent = agent_by_session(conn, session_id)
    if not agent:
        raise ValueError("no such session — register before renaming")
    old = agent["handle"]
    new = re.sub(r"[^a-z0-9_-]", "", (new_handle or "").lower())
    if not new:
        raise ValueError("handle must contain a-z, 0-9, '-' or '_'")
    if new == old:
        return (old, old)  # idempotent: renaming to your own name is a no-op
    if new in RESERVED_HANDLES:
        raise ValueError(f"'{new}' is reserved and cannot be a handle")

    holder = agent_by_handle(conn, new)
    if holder and holder["session_id"] != session_id:
        if _is_active(holder["last_seen"]):
            raise ValueError(f"handle '{new}' is in use by an active agent")
        # Inactive holder: reclaim the dead row, re-asserting staleness IN the
        # DELETE so a holder that revived mid-reclaim keeps its name — the UPDATE
        # below then collides on the UNIQUE handle and we surface a clean error.
        cutoff = datetime.fromtimestamp(
            _now_epoch() - ACTIVE_WINDOW_SECONDS, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "DELETE FROM agents WHERE session_id = ? AND (last_seen IS NULL OR last_seen < ?)",
            (holder["session_id"], cutoff),
        )

    try:
        conn.execute(
            "UPDATE agents SET handle = ?, last_seen = ? WHERE session_id = ?",
            (new, now_iso(), session_id),
        )
    except sqlite3.IntegrityError:
        raise ValueError(f"handle '{new}' was just taken — try another")
    # Leadership follows the rename: a lead pointer stores a handle, so without this a
    # renamed lead would orphan the pointer until the floor re-elected. Repoint only the
    # scopes THIS agent can legitimately hold — the global chair ('lead') and its own
    # squad's captaincy ('lead:<its squad>') — so a rename never refreshes some other
    # squad's stale pointer that happens to match the old handle.
    scopes = ["lead"] + ([_lead_key(agent["squad"])] if agent["squad"] else [])
    for k in scopes:
        if (get_meta(conn, k) or "").strip().lower() == old.lower():
            set_meta(conn, k, new)
    conn.commit()
    # Let teammates learn the rename through the normal cursor. kind='system'
    # carries no @mentions, so it neither blocks a Stop nor gates the team barrier;
    # best-effort — the rename already succeeded if the notice fails.
    try:
        send(conn, new, f"renamed: {old} → {new}", session_id=session_id,
             kind="system")
    except Exception:
        pass
    return (old, new)


def agent_by_session(conn, session_id: str):
    return conn.execute(
        "SELECT * FROM agents WHERE session_id = ?", (session_id,)
    ).fetchone()


def agent_by_handle(conn, handle: str):
    return conn.execute(
        "SELECT * FROM agents WHERE handle = ?", (handle.lower(),)
    ).fetchone()


def resolve_agent(conn, session_id: str | None, handle: str | None):
    if session_id:
        a = agent_by_session(conn, session_id)
        if a:
            return a
    if handle:
        return agent_by_handle(conn, handle)
    return None


def active_agents(conn) -> list[sqlite3.Row]:
    rows = conn.execute("SELECT * FROM agents ORDER BY handle").fetchall()
    return [r for r in rows if _is_active(r["last_seen"])]


def _norm_squad(name: str | None) -> str | None:
    """Sanitize a squad name to ``[a-z0-9_-]`` (like a handle); empty → None (the default
    global room). So a NULL squad and an unset/blank ``$GROUPCHAT_SQUAD`` are the same."""
    s = re.sub(r"[^a-z0-9_-]", "", (name or "").strip().lower())
    return s or None


def _norm_model(name: str | None) -> str | None:
    """Sanitize a model id/family for the diversity annotation: keep ``[a-z0-9._-]``
    (lowercased), collapse to a bounded token; empty → None (unknown). Free-form enough
    for any host's model id (``claude-opus-4-8``, ``gpt-5-codex``, ``glm-4.6``)."""
    s = re.sub(r"[^a-z0-9._-]", "", (name or "").strip().lower())
    return s[:64] or None


def active_in_squad(conn, squad: str | None) -> list[sqlite3.Row]:
    """Active agents in ``squad`` (``None`` = the default global room). In an UNSHARDED
    room every agent has ``squad IS NULL``, so ``active_in_squad(conn, None)`` is exactly
    ``active_agents(conn)`` — which is what keeps the barrier byte-identical when unused."""
    sq = _norm_squad(squad) if isinstance(squad, str) else squad
    return [a for a in active_agents(conn) if (a["squad"] or None) == sq]


# --------------------------------------------------------------------------- #
# Team bootstrap — spawn other agent sessions, mapped to free handles
# --------------------------------------------------------------------------- #
# Open the rest of the team in one command: pick free team-member handles and
# launch a Claude (or any host) session per handle, each born with its name via
# GROUPCHAT_HANDLE. Dormant until used — nothing here runs unless `bootstrap` is
# called, so a repo that never bootstraps behaves exactly as before.
BOOTSTRAP_MAX = 8  # soft cap so a fat-fingered count can't open a swarm of windows
# Safe-autonomous-spawn backstops. A spawned agent inherits the full CLI + the skill
# that advertises bootstrap, so recursive fan-out would otherwise be unbounded. Each
# child is launched with GROUPCHAT_SPAWN_DEPTH+1; bootstrap refuses past the max depth
# (runaway recursion) or the live-fleet ceiling. Both are env-tunable; --force overrides.
MAX_SPAWN_DEPTH = 2   # how deep autonomous spawning may nest (the orchestrator is 0)
MAX_FLEET = 16        # ceiling on simultaneously-active agents in one room


def max_spawn_depth() -> int:
    v = _env_int("GROUPCHAT_MAX_SPAWN_DEPTH")
    return MAX_SPAWN_DEPTH if v is None else v


def max_fleet() -> int:
    v = _env_int("GROUPCHAT_MAX_FLEET")
    return MAX_FLEET if v is None else v


def current_spawn_depth() -> int:
    """This process's spawn depth (0 for a human-launched/root agent), from the env a
    parent bootstrap threaded in. Floored at 0 so a negative env can't defeat the
    ``depth >= max`` backstop or self-propagate a negative depth down the lineage."""
    return max(0, _env_int("GROUPCHAT_SPAWN_DEPTH", 0) or 0)


def _pick_free_handles(conn, n: int, explicit: list[str] | None = None) -> list[str]:
    """Choose handles for ``n`` agents-to-be, none colliding with an active
    teammate, a reserved name, or each other. With ``explicit`` names, sanitize and
    collision-suffix each (so `ada` becomes `ada-2` when `ada` is active); otherwise
    walk HANDLE_POOL then fall back to agent-N. The names are not registered here —
    each spawned session claims its own via GROUPCHAT_HANDLE when it starts."""
    taken = {a["handle"] for a in active_agents(conn)} | set(RESERVED_HANDLES)
    picked: list[str] = []
    if explicit:
        for raw in explicit:
            cand = re.sub(r"[^a-z0-9_-]", "", (raw or "").lower()) or "agent"
            if cand in taken:
                i = 2
                while f"{cand}-{i}" in taken:
                    i += 1
                cand = f"{cand}-{i}"
            taken.add(cand)
            picked.append(cand)
        return picked
    for h in HANDLE_POOL:
        if len(picked) >= n:
            break
        if h not in taken:
            taken.add(h)
            picked.append(h)
    i = 1
    while len(picked) < n:
        cand = f"agent-{i}"
        if cand not in taken:
            taken.add(cand)
            picked.append(cand)
        i += 1
    return picked


def _default_spawn_method() -> str:
    """Terminal.app on macOS; elsewhere fall back to printing the commands."""
    return "terminal" if sys.platform == "darwin" else "print"


def _spawn_command(name: str, cwd: str, prompt: str | None,
                   depth: int = 0, spawned_by: str | None = None,
                   squad: str | None = None, model: str | None = None) -> str:
    """The shell command a spawned session runs: cd into the repo and launch claude
    with its handle pre-set, so its SessionStart hook registers it under ``name``.
    ``AGORA_SPAWN_DEPTH``/``AGORA_SPAWNED_BY`` carry the spawn lineage; ``squad``
    (``AGORA_SQUAD``) puts the child in a sub-team with its own barrier; ``model``
    (``AGORA_MODEL``) lets a same-host bootstrapped fleet self-declare its model so the
    vote tally's diversity signal isn't inert (a same-host spawn IS the capture case). (The
    child reads the new ``AGORA_*`` names; legacy ``GROUPCHAT_*`` is still honored.)"""
    claude = shutil.which("claude") or "claude"
    env = f"AGORA_HANDLE={name} AGORA_SPAWN_DEPTH={int(depth)}"
    if spawned_by:
        env += f" AGORA_SPAWNED_BY={shlex.quote(spawned_by)}"
    if squad:
        env += f" AGORA_SQUAD={shlex.quote(squad)}"
    if model:
        env += f" AGORA_MODEL={shlex.quote(model)}"
    cmd = f"cd {shlex.quote(cwd)} && {env} {shlex.quote(claude)}"
    if prompt:
        cmd += " " + shlex.quote(prompt)
    return cmd


def _applescript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _worktree_path(cwd: str, name: str) -> str:
    """Where a spawned agent's isolated worktree lives: a sibling of the repo dir,
    ``<repo>-worktrees/<name>`` — outside the working tree so it doesn't clutter
    git status, and parallel to it so it's easy to find."""
    base = os.path.abspath(cwd)
    return os.path.join(os.path.dirname(base),
                        os.path.basename(base) + "-worktrees", name)


def _worktree_add_argv(repo_cwd: str, path: str, name: str) -> list[str]:
    return ["git", "-C", repo_cwd, "worktree", "add", path, "-b", f"groupchat/{name}"]


def _create_worktree(repo_cwd: str, path: str, name: str) -> str | None:
    """Create a git worktree at ``path`` on a fresh branch ``groupchat/<name>`` (from
    the current HEAD) in the repo containing ``repo_cwd``. Returns None on success,
    else an error string. ``git worktree add`` makes the parent dirs itself, so a
    failure leaves nothing behind. There is deliberately NO fallback to checking out
    an *existing* ``groupchat/<name>`` branch: a left-over branch from a prior run
    would start the agent on a STALE base. On a name collision we report the error and
    let the caller skip — the human clears the old worktree/branch and retries
    (`git worktree remove …` / `git branch -D groupchat/<name>`)."""
    r = subprocess.run(_worktree_add_argv(repo_cwd, path, name),
                       capture_output=True, text=True)
    if r.returncode == 0:
        return None
    return (r.stderr or "").strip() or "git worktree add failed"


# --- Worktree reconciliation (read-only, diff-only) ---------------------------
# `bootstrap --worktree` lands each agent on its own `groupchat/<name>` branch; this
# is the collect side. It is strictly READ-ONLY — it computes ahead/behind, changed
# files, and cross-branch overlaps so an operator can decide a merge order, but it
# NEVER merges (the human runs the merges from the report).
def _git_out(args: list[str], cwd: str) -> str | None:
    """Run a git command, returning stripped stdout (None on any error)."""
    try:
        r = subprocess.run(["git", "-C", cwd, *args],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def groupchat_branches(repo_cwd: str) -> list[str]:
    """The ``groupchat/<name>`` branches in the repo (the bootstrap --worktree branches)."""
    out = _git_out(["for-each-ref", "--format=%(refname:short)", "refs/heads/groupchat/"],
                   repo_cwd)
    return [b for b in (out or "").splitlines() if b.strip()]


def _default_base(repo_cwd: str) -> str:
    """The base to diff the worktree branches against: the MAIN worktree's branch — NOT
    the cwd's current branch. Running ``worktrees`` from *inside* a ``groupchat/<name>``
    worktree would otherwise default the base to that branch and compare it against
    itself (silently zeroing its own work). ``git worktree list --porcelain`` lists the
    main worktree first; we take its branch, falling back to the cwd's HEAD."""
    out = _git_out(["worktree", "list", "--porcelain"], repo_cwd) or ""
    block = out.split("\n\n", 1)[0]  # first block = the main worktree
    for line in block.splitlines():
        if line.startswith("branch "):
            ref = line[len("branch "):].strip()
            return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return _git_out(["rev-parse", "--abbrev-ref", "HEAD"], repo_cwd) or "HEAD"


def worktree_report(repo_cwd: str, base: str | None = None) -> dict:
    """A read-only reconciliation of the ``groupchat/*`` branches against ``base``
    (default: the repo's current branch). Returns:
      ``{base, branches:[{branch,name,ahead,behind,files,insertions,deletions}],
         overlaps:[{file, branches:[…]}], order:[branch,…]}``
    ``order`` is an advisory merge order (smallest blast radius first); ``overlaps``
    flags files touched by more than one branch — the merge-carefully signal. Computes
    nothing destructive: pure ``git rev-list``/``diff`` reads."""
    base = base or _default_base(repo_cwd)
    branches = []
    files_by_branch: dict[str, set] = {}
    for br in groupchat_branches(repo_cwd):
        name = br.split("/", 1)[1] if "/" in br else br
        counts = _git_out(["rev-list", "--left-right", "--count", f"{base}...{br}"], repo_cwd)
        behind = ahead = 0
        if counts and "\t" in counts:
            left, _, right = counts.partition("\t")
            behind, ahead = int(left or 0), int(right.strip() or 0)
        files = [f for f in (_git_out(["diff", "--name-only", f"{base}...{br}"],
                                      repo_cwd) or "").splitlines() if f.strip()]
        ins = dele = 0
        for line in (_git_out(["diff", "--numstat", f"{base}...{br}"], repo_cwd) or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                ins += int(parts[0]) if parts[0].isdigit() else 0
                dele += int(parts[1]) if parts[1].isdigit() else 0
        files_by_branch[br] = set(files)
        branches.append({"branch": br, "name": name, "ahead": ahead, "behind": behind,
                         "files": files, "insertions": ins, "deletions": dele})
    # Cross-branch file overlaps — the same path edited on two branches.
    overlaps = []
    all_files = sorted(set().union(*files_by_branch.values())) if files_by_branch else []
    for f in all_files:
        touched = sorted(br for br, fs in files_by_branch.items() if f in fs)
        if len(touched) > 1:
            overlaps.append({"file": f, "branches": touched})
    # Advisory order: fewest files, then fewest commits ahead, then name.
    order = [b["branch"] for b in sorted(
        branches, key=lambda b: (len(b["files"]), b["ahead"], b["branch"]))]
    return {"base": base, "branches": branches, "overlaps": overlaps, "order": order}


def _parse_spec(token: str) -> tuple[str, str | None]:
    """Split a bootstrap spec ``name`` or ``name:prompt``. Handles are [a-z0-9_-], so
    the FIRST colon always separates the handle from its per-agent prompt — letting an
    orchestrator deal out DISTINCT work (``ada:'do X' turing:'do Y'``) instead of one
    identical prompt to everyone."""
    if ":" in token:
        name, prompt = token.split(":", 1)
        return name, (prompt.strip() or None)
    return token, None


def spawn_agents(names, cwd: str, method: str = "terminal",
                 prompt: str | None = None, dry_run: bool = False,
                 worktree: bool = False, prompts: dict | None = None,
                 depth: int = 0, spawned_by: str | None = None,
                 squad: str | None = None, model: str | None = None) -> list[dict]:
    """Open one agent session per handle in ``names``. Returns a per-name result
    list of dicts {name, command, ok, error}. ``dry_run`` (or method='print')
    spawns nothing and just reports the runnable commands. ``prompts`` maps a handle
    to its own initial prompt (per-agent work division); a handle absent from the map
    falls back to the uniform ``prompt``. ``depth``/``spawned_by`` are threaded to each
    child as spawn lineage. With ``worktree=True`` each agent gets its own git worktree
    (branch ``groupchat/<name>``) so their file edits can't collide; the shared chat.db
    (anchored at the git common dir) keeps them in one room."""
    results: list[dict] = []
    tmux_started = False
    for name in names:
        this_prompt = (prompts.get(name) if prompts else None) or prompt
        launch_dir = _worktree_path(cwd, name) if worktree else cwd
        launch_cmd = _spawn_command(name, launch_dir, this_prompt,
                                    depth=depth, spawned_by=spawned_by, squad=squad,
                                    model=model)
        # The reproducible one-liner shown to a human (print/dry-run) must also
        # create the worktree; the real-launch path makes it via subprocess first,
        # then runs only launch_cmd (re-running `git worktree add` would fail the
        # &&-chain and stop `claude` from starting).
        if worktree:
            add = " ".join(shlex.quote(x)
                           for x in _worktree_add_argv(cwd, launch_dir, name))
            display_cmd = add + " && " + launch_cmd
        else:
            display_cmd = launch_cmd
        rec = {"name": name, "command": display_cmd, "ok": False, "error": None}
        if dry_run or method == "print":
            rec["ok"] = True  # nothing to launch; the command line is the deliverable
            results.append(rec)
            continue
        try:
            if worktree:
                err = _create_worktree(cwd, launch_dir, name)
                if err:
                    # Don't silently fall back to the shared cwd — isolation was the
                    # whole point; report and skip this one.
                    raise RuntimeError(f"git worktree add failed: {err}")
            if method == "terminal":
                if sys.platform != "darwin":
                    raise RuntimeError("terminal method needs macOS; use --method print")
                script = ('tell application "Terminal" to do script '
                          f'"{_applescript_escape(launch_cmd)}"')
                subprocess.run(["osascript", "-e", script], check=True,
                               capture_output=True, text=True)
                rec["ok"] = True
            elif method == "tmux":
                if not shutil.which("tmux"):
                    raise RuntimeError("tmux not found; use --method terminal or print")
                tmux_args = (["new-session", "-d", "-s", "groupchat", "-n", name, launch_cmd]
                             if not tmux_started
                             else ["new-window", "-t", "groupchat", "-n", name, launch_cmd])
                subprocess.run(["tmux", *tmux_args], check=True,
                               capture_output=True, text=True)
                tmux_started = True
                rec["ok"] = True
            else:
                raise RuntimeError(f"unknown spawn method '{method}'")
        except Exception as e:
            detail = getattr(e, "stderr", None) or str(e)
            rec["error"] = (detail.strip() if isinstance(detail, str) and detail
                            else str(e))
        results.append(rec)
    # Bring Terminal forward once if anything opened (best-effort cosmetic).
    if method == "terminal" and not dry_run and any(r["ok"] for r in results):
        try:
            subprocess.run(["osascript", "-e",
                            'tell application "Terminal" to activate'],
                           check=False, capture_output=True, text=True)
        except Exception:
            pass
    return results


def poll_joined(conn, names, timeout: float = 5.0, tick: float = 0.5) -> dict:
    """Best-effort: wait up to ``timeout`` seconds for each handle in ``names`` to
    appear as an *active* agent, so bootstrap can report who actually registered.

    A spawned window where ``claude`` wasn't on PATH (or that is just slow to start)
    simply reports not-yet — never an error. Returns ``{name: bool}``; exits early
    once every name has joined."""
    tick = max(0.05, tick)  # never let a bad tick reach time.sleep() as a negative
    pending = set(names)
    joined = {n: False for n in names}
    deadline = time.monotonic() + max(0.0, timeout)
    while pending and time.monotonic() < deadline:
        active = {a["handle"] for a in active_agents(conn)}
        for n in list(pending):
            if n in active:
                joined[n] = True
                pending.discard(n)
        if pending:
            time.sleep(min(tick, max(0.0, deadline - time.monotonic())))
    return joined


def set_status(conn, session_id: str, status: str) -> None:
    conn.execute(
        "UPDATE agents SET status = ?, last_seen = ? WHERE session_id = ?",
        (status, now_iso(), session_id),
    )
    conn.commit()


def record_tokens(conn, session_id: str, totals: dict) -> None:
    """Overwrite an agent's cumulative token counts (idempotent — totals are
    recomputed from the full transcript each call, so re-parks can't double-count)."""
    conn.execute(
        "UPDATE agents SET in_tokens=?, out_tokens=?, cache_read_tokens=?, "
        "cache_create_tokens=? WHERE session_id=?",
        (int(totals.get("in_tokens", 0)), int(totals.get("out_tokens", 0)),
         int(totals.get("cache_read_tokens", 0)), int(totals.get("cache_create_tokens", 0)),
         session_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Team barrier — when may a finished agent actually exit?
# --------------------------------------------------------------------------- #
def _size_keys(squad: str | None) -> tuple[str, str]:
    """The (size, stamped-at) meta keys for a squad. The default room (``squad=None``)
    keeps the original ``team_size`` keys, so an unsharded room is byte-identical; a
    named squad gets ``team_size:<squad>``."""
    sq = _norm_squad(squad) if isinstance(squad, str) else squad
    return (("team_size", "team_size_at") if not sq
            else (f"team_size:{sq}", f"team_size_at:{sq}"))


def expected_team_size(conn, squad: str | None = None) -> int | None:
    """Declared size for a squad (``None`` = the default room): ``$GROUPCHAT_TEAM_SIZE``
    wins for the default room, else the ``expect``-stamped meta key for that squad."""
    if not (_norm_squad(squad) if isinstance(squad, str) else squad):
        n = _env_int("GROUPCHAT_TEAM_SIZE")
        if n is not None:
            return n
    mv = get_meta(conn, _size_keys(squad)[0])
    return int(mv) if mv and mv.isdigit() else None


def set_team_size(conn, n: int, squad: str | None = None) -> None:
    """Declare the expected size for a squad (``expect`` / ``bootstrap``). Stamps *when*
    it was set so a leftover from a long-departed cohort can be told apart from a fresh
    declaration (see ``_clear_stale_team_size``). ``n <= 0`` clears it."""
    ksize, kat = _size_keys(squad)
    if n <= 0:
        del_meta(conn, ksize)
        del_meta(conn, kat)
        return
    set_meta(conn, ksize, str(n))
    set_meta(conn, kat, now_iso())


def _clear_stale_team_size(conn, session_id: str) -> None:
    """Drop a leftover size declared by a now-departed cohort so a quick solo session in
    a REUSED room isn't routed into the 90s wait. Scoped to the just-registered agent's
    SQUAD (its own size key + its own active set): fires only when the agent is the SOLE
    active member of its squad AND that squad's size was declared more than one
    active-window ago. A teammate in a DIFFERENT squad never anchors (or defeats) the
    reclaim, and a per-squad ``team_size:<squad>`` is reaped like the global one. A
    missing stamp (old db) reads as +inf age, i.e. stale. ``$GROUPCHAT_TEAM_SIZE`` (env)
    wins in ``expected_team_size`` and is untouched here."""
    a = agent_by_session(conn, session_id)
    squad = a["squad"] if a else None
    ksize, kat = _size_keys(squad)
    if get_meta(conn, ksize) is None:
        return
    if any(x["session_id"] != session_id for x in active_in_squad(conn, squad)):
        return  # a squadmate anchors the cohort — the size is live
    if iso_age_seconds(get_meta(conn, kat)) >= ACTIVE_WINDOW_SECONDS:
        del_meta(conn, ksize)
        del_meta(conn, kat)


def max_park_seconds() -> int:
    v = _env_int("GROUPCHAT_MAX_PARK")  # 0 is a valid (release-now) override
    return MAX_PARK_SECONDS if v is None else v


def solo_grace_seconds() -> int:
    v = _env_int("GROUPCHAT_SOLO_GRACE")  # 0 is a valid (no-wait) override
    return SOLO_GRACE_SECONDS if v is None else v


def cohort_age_seconds(conn, squad: str | None = None) -> float:
    """Age of a squad's current cohort — seconds since its earliest-joined active agent
    first registered (``None`` = the whole default room, byte-identical when unsharded)."""
    ages = [iso_age_seconds(a["first_seen"]) for a in active_in_squad(conn, squad)]
    finite = [a for a in ages if a != float("inf")]
    return max(finite) if finite else 0.0


def startup_guard_satisfied(conn, squad: str | None = None) -> bool:
    """Has the team finished assembling enough to trust the barrier?

    Closes the ragged-startup race where a fast agent stops before slower
    teammates have even registered (trivially satisfying an empty barrier), while
    keeping a *solo* agent from waiting for teammates who will never come.

    Counts only **active** agents (not all-time rows): a stale row from a prior
    run must never satisfy the size guard — that would defeat the barrier on any
    reused room (premature exit). Mirrors ``cohort_age_seconds`` / ``team_done``.
    """
    n_active = len(active_in_squad(conn, squad))
    size = expected_team_size(conn, squad)
    if size:
        if n_active >= size:
            return True
        # A declared team that never fully assembles (a failed bootstrap window, a
        # too-high size) must NOT hang everyone until the 2h ceiling: fall back to
        # the startup grace so it releases at ~90s instead. A no-show is bounded.
        return cohort_age_seconds(conn, squad) >= STARTUP_GRACE_SECONDS
    if n_active <= 1:
        # Solo (or alone-so-far) with no declared size: only a brief settle window,
        # not the full grace — a lone agent doesn't wait for absent teammates. A
        # co-launched teammate that registers within the window flips us into the
        # multi-agent branch below and gets the full ragged-startup grace.
        return cohort_age_seconds(conn, squad) >= solo_grace_seconds()
    return cohort_age_seconds(conn, squad) >= STARTUP_GRACE_SECONDS


def team_done(conn, squad: str | None = None) -> bool:
    """True when every active barrier-capable agent in ``squad`` has finished — the
    barrier. ``squad=None`` is the default global room (byte-identical when unsharded:
    every agent has ``squad IS NULL`` so the scope is the whole active set). A finished
    squad tears down independently of the rest of the fleet.

    Crashed/silent teammates age out of the active window and stop counting, so
    a dead agent can't wedge the team forever.
    """
    if not startup_guard_satisfied(conn, squad):
        return False
    active = active_in_squad(conn, squad)
    if not active:
        return False
    # Only barrier-capable agents (those with a Stop hook that marks done) gate the
    # barrier — a non-hook host (opencode/generic, parks=0) never marks done and must
    # not hold a hook team. They still count toward assembly (startup guard); they just
    # don't block the all-done check.
    barrier = [a for a in active if a["parks"]]
    if not barrier:
        return False
    return all((a["status"] or "") == DONE_STATUS for a in barrier)


# --------------------------------------------------------------------------- #
# Control plane — release a fleet without waiting on the barrier
# --------------------------------------------------------------------------- #
# Steering used to be cooperative-only. These let an operator/lead release parked
# agents: a team-wide ``standdown`` (everyone may stop now) or a per-agent ``dismiss``
# (drop one worker so a still-active orchestrator doesn't pin it to the 2h ceiling).
# The Stop-hook park loop reads ``released_from_barrier`` each tick. Dormant until used.
def set_standdown(conn, reason: str | None = None) -> None:
    """Declare a team-wide standdown (timestamped). Every parked agent is released
    within one poll tick. Auto-expires after the active window so a stale flag can't
    haunt a later cohort in a reused room; ``clear_standdown`` lifts it explicitly."""
    set_meta(conn, "standdown", now_iso())
    if reason:
        set_meta(conn, "standdown_reason", reason.strip())
    else:
        del_meta(conn, "standdown_reason")


def clear_standdown(conn) -> None:
    del_meta(conn, "standdown")
    del_meta(conn, "standdown_reason")


def standdown_active(conn) -> bool:
    ts = get_meta(conn, "standdown")
    # A flag older than the active window is from a departed cohort — treat as lifted.
    return bool(ts) and iso_age_seconds(ts) < ACTIVE_WINDOW_SECONDS


def _clear_stale_standdown(conn, session_id: str) -> None:
    """Drop a standdown left by a DEPARTED cohort so a fresh cohort assembling in a reused
    room WITHIN the 15-min active window isn't released from its barrier on arrival (the
    auto-expiry only covers a flag older than the window). Fires only when the
    just-registered agent is the SOLE active agent — i.e. the declaring cohort is gone; a
    still-active cohort (a teammate present) keeps its standdown. Mirrors
    ``_clear_stale_team_size``."""
    if get_meta(conn, "standdown") is None:
        return
    if any(a["session_id"] != session_id for a in active_agents(conn)):
        return  # a teammate anchors the cohort — the standdown is live
    del_meta(conn, "standdown")
    del_meta(conn, "standdown_reason")


def _dismissed_set(conn) -> set:
    """The dismissed-session set. A dedicated ROW-PER-SESSION table (not a JSON blob), so
    add/remove are atomic single statements — no read-modify-write lost-update under
    concurrent dismiss/dismiss or dismiss/clear. Reads are inherently fail-safe (no JSON to
    corrupt), which matters because ``is_dismissed`` runs inside the Stop hook."""
    return {r["session_id"] for r in conn.execute("SELECT session_id FROM dismissed")}


def dismiss_agent(conn, handle: str) -> str | None:
    """Release ONE active agent from the barrier (lead/operator action). Returns its
    session id, or None if no such active agent. Marks it ``done`` (so it no longer holds
    OTHER agents) and records its session in the ``dismissed`` table — keyed by session id
    (immune to handle reuse). The insert is atomic (``INSERT OR IGNORE``), so concurrent
    dismissals can't clobber each other. Its own park loop sees the dismissal and exits."""
    a = agent_by_handle(conn, (handle or "").strip().lower())
    if not a or not _is_active(a["last_seen"]):
        return None
    sid = a["session_id"]
    set_status(conn, sid, DONE_STATUS)
    conn.execute("INSERT OR IGNORE INTO dismissed(session_id, ts) VALUES(?, ?)",
                 (sid, now_iso()))
    # Opportunistically prune rows for departed sessions so the table stays bounded.
    live = {x["session_id"] for x in active_agents(conn)}
    for s in list(_dismissed_set(conn)):
        if s not in live and s != sid:
            conn.execute("DELETE FROM dismissed WHERE session_id=?", (s,))
    conn.commit()
    return sid


def is_dismissed(conn, session_id: str) -> bool:
    return conn.execute("SELECT 1 FROM dismissed WHERE session_id=?",
                        (session_id,)).fetchone() is not None


def clear_dismissed(conn, session_id: str) -> None:
    """Consume a ONE-SHOT dismissal: drop ``session_id`` (atomic DELETE). Called when a
    dismissed session is released (leaving) or REVIVES to answer a teammate — so a revived
    agent rejoins the barrier instead of being stuck 'released' for the rest of its life."""
    conn.execute("DELETE FROM dismissed WHERE session_id=?", (session_id,))
    conn.commit()


def released_from_barrier(conn, session_id: str) -> bool:
    """Should this agent be let out of the barrier regardless of team-done? True under
    a team-wide standdown or an individual dismissal. The Stop hook checks this each
    park tick; a flat room never sets either, so it's always False there."""
    return standdown_active(conn) or is_dismissed(conn, session_id)


# --------------------------------------------------------------------------- #
# Collision-safety & observability — focus, shared-cwd, file claims, quiet-detection
# --------------------------------------------------------------------------- #
# The roster showed liveness but never WHAT each agent is doing or where collisions
# lurk. These add a per-agent focus, a shared-working-tree warning, a structured
# file-claim ledger, and a soft "gone quiet" signal. All dormant until used.
def set_focus(conn, session_id: str, text: str | None) -> None:
    """Set (or clear, with empty text) this agent's current-work focus — distinct from
    the barrier ``status`` column, so it never affects done-detection. Interior
    whitespace/newlines are collapsed so a focus can't spoof a roster line or inject
    raw newlines into the briefing context."""
    clean = " ".join((text or "").split()) or None
    conn.execute("UPDATE agents SET focus = ? WHERE session_id = ?", (clean, session_id))
    conn.commit()


def quiet_seconds() -> int:
    v = _env_int("GROUPCHAT_QUIET_SECS")
    return QUIET_SECONDS if v is None else v


def last_chat_age(conn, handle: str) -> float:
    """Seconds since ``handle`` last posted a chat message; +inf if it never has."""
    row = conn.execute(
        "SELECT ts FROM messages WHERE kind='chat' AND sender=? ORDER BY id DESC LIMIT 1",
        ((handle or "").strip().lower(),)).fetchone()
    return iso_age_seconds(row["ts"]) if row else float("inf")


def last_chat_ages(conn) -> dict:
    """``{handle: seconds-since-last-chat}`` for every sender, in ONE grouped query — so
    ``who`` computes quiet-detection without an N+1 per-agent message scan."""
    rows = conn.execute(
        "SELECT sender, MAX(ts) AS ts FROM messages WHERE kind='chat' GROUP BY sender"
    ).fetchall()
    return {r["sender"]: iso_age_seconds(r["ts"]) for r in rows}


def is_quiet(conn, agent, chat_age: float | None = None) -> bool:
    """A soft stuck/heads-down signal: the agent is active and NOT done, has been around
    longer than the quiet window (so a fresh joiner isn't flagged), yet hasn't chatted
    within it. Advisory only. A done (parked) agent and an agent with an explicit
    ``focus`` (direct evidence it's mid-task) are never flagged. ``chat_age`` may be
    passed pre-computed (so ``who`` avoids an N+1 per-agent message scan)."""
    if (agent["status"] or "") == DONE_STATUS or not _is_active(agent["last_seen"]):
        return False
    if agent["focus"]:
        return False  # an explicit focus is liveness — never contradict it with ◐
    q = quiet_seconds()
    age = iso_age_seconds(agent["first_seen"])
    if not (age == age) or age == float("inf") or age < q:
        return False  # unknown/NaN/too-fresh age -> not flagged (fail to not-quiet)
    if chat_age is None:
        chat_age = last_chat_age(conn, agent["handle"])
    return chat_age >= q


def shared_cwd_peers(conn, session_id: str) -> list[str]:
    """Handles of OTHER active agents sharing this agent's working tree (same cwd) — the
    high-collision config (parallel edits, no worktree isolation). Empty when the agent
    has its own tree (e.g. a ``bootstrap --worktree`` agent) or is alone."""
    me = agent_by_session(conn, session_id)
    if not me or not me["cwd"]:
        return []
    mine = os.path.abspath(me["cwd"])
    return sorted(a["handle"] for a in active_agents(conn)
                  if a["session_id"] != session_id and a["cwd"]
                  and os.path.abspath(a["cwd"]) == mine)


def add_claim(conn, session_id: str, handle: str, glob: str) -> int:
    """Record an intent-to-edit claim on a path glob. Idempotent per (session, glob):
    re-claiming the same glob refreshes rather than duplicates."""
    glob = " ".join((glob or "").split())  # collapse whitespace (no newline injection)
    if not glob:
        raise ValueError("a claim needs a path or glob")
    conn.execute("DELETE FROM claims WHERE session_id=? AND glob=?", (session_id, glob))
    cur = conn.execute(
        "INSERT INTO claims(ts, session_id, handle, glob) VALUES (?,?,?,?)",
        (now_iso(), session_id, (handle or "").strip().lower(), glob))
    conn.commit()
    return cur.lastrowid


def release_claim(conn, session_id: str, glob: str) -> int:
    cur = conn.execute("DELETE FROM claims WHERE session_id=? AND glob=?",
                       (session_id, (glob or "").strip()))
    conn.commit()
    return cur.rowcount


def active_claims(conn) -> list[sqlite3.Row]:
    """Claims held by currently-active agents (a crashed agent's claims age out with
    it, so the ledger self-cleans)."""
    live = {a["session_id"] for a in active_agents(conn)}
    rows = conn.execute("SELECT * FROM claims ORDER BY id").fetchall()
    return [r for r in rows if r["session_id"] in live]


def _glob_matches(glob: str, path: str) -> bool:
    """Soft match of a claim ``glob`` against a file ``path``. Handles the cases that
    matter: a wildcard glob against the ABSOLUTE path an Edit tool presents
    (``src/auth/*.py`` ↔ ``/repo/src/auth/handler.py``), a basename glob (``*.py``), and
    a directory-prefix claim (``src/auth`` ↔ ``…/src/auth/handler.py``). The directory
    match is anchored to path COMPONENT boundaries, so a bare ``src`` does NOT match
    ``…/mysrc/…`` and a one-letter claim doesn't grab the repo. Advisory only."""
    import fnmatch
    g = (glob or "").strip().replace("\\", "/")
    p = (path or "").strip().replace("\\", "/")
    if not g or not p:
        return False
    base = p.rsplit("/", 1)[-1]
    # Direct fnmatch on the full path / basename, and the relative-glob-vs-absolute-path
    # case (a leading ``*/`` lets a repo-relative glob match an absolute path tail).
    if (fnmatch.fnmatch(p, g) or fnmatch.fnmatch(base, g)
            or fnmatch.fnmatch(p, "*/" + g)):
        return True
    # Directory / prefix claim: the literal prefix before the first wildcard, matched at
    # a component boundary (start, or bracketed by '/'), never a bare substring.
    prefix = re.split(r"[*?\[]", g, maxsplit=1)[0].rstrip("/")
    if not prefix:
        return False
    return (p == prefix or p.startswith(prefix + "/")
            or ("/" + prefix + "/") in p or p.endswith("/" + prefix))


def path_claimed_by(conn, path: str, exclude_session: str | None = None) -> list[tuple]:
    """``(handle, glob)`` for every active claim (excluding ``exclude_session``) whose
    glob matches ``path`` — the lookup an edit-time warning (or ``claims --path``) uses."""
    out = []
    for r in active_claims(conn):
        if r["session_id"] == exclude_session:
            continue
        if _glob_matches(r["glob"], path):
            out.append((r["handle"], r["glob"]))
    return out


# --------------------------------------------------------------------------- #
# Work division — a durable task ledger + a shared goal
# --------------------------------------------------------------------------- #
# The chat is a room; these make it a coordinator. A ``tasks`` row is one slice of
# work (open / claimed / done) so an agent can learn its task from the bus instead of
# a human typing it into each window, and two agents can't both grab the same slice
# (the claim is an ATOMIC, status-guarded UPDATE). A ``goal`` meta key holds the
# one-line shared objective. Dormant until used: a room that never adds a task or sets
# a goal renders byte-identically to before (the surfaces below check for emptiness).
def add_task(conn, title: str, paths: str | None = None,
             creator: str | None = None, owner: str | None = None,
             status: str = "open") -> int:
    """Append a task; return its id. ``owner``/``status`` let ``assign`` create a task
    already claimed by a teammate in a single step."""
    title = (title or "").strip()
    if not title:
        raise ValueError("a task needs a title")
    cur = conn.execute(
        "INSERT INTO tasks(ts, title, owner, status, paths, creator) VALUES (?,?,?,?,?,?)",
        (now_iso(), title, (owner or None), status, (paths or None), (creator or None)),
    )
    conn.commit()
    return cur.lastrowid


def task_by_id(conn, task_id: int):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def claim_task(conn, task_id: int, handle: str) -> tuple[str, "sqlite3.Row | None"]:
    """Atomically claim an OPEN task for ``handle``. Returns ``(result, row)``:
      * ``('claimed', row)`` — newly claimed, or idempotently re-claimed by its owner;
      * ``('taken', row)``   — already held by someone else;
      * ``('done', row)``    — already completed;
      * ``('missing', None)``— no such task.

    The status-guarded UPDATE is the race fix: two agents racing to claim the same id
    both run ``... WHERE id=? AND status='open'``; SQLite serializes the writes, so
    exactly one row changes and the loser falls through to ``'taken'`` rather than
    co-owning the slice (the "two agents grab the same task" gap)."""
    h = (handle or "").strip().lower()
    cur = conn.execute(
        "UPDATE tasks SET owner=?, status='claimed' WHERE id=? AND status='open'",
        (h, task_id),
    )
    conn.commit()
    row = task_by_id(conn, task_id)
    if row is None:
        return ("missing", None)
    if cur.rowcount:
        return ("claimed", row)             # we won the race
    if row["status"] == "done":
        return ("done", row)
    if (row["owner"] or "") == h:
        return ("claimed", row)             # idempotent: already ours
    return ("taken", row)


def complete_task(conn, task_id: int,
                  handle: str | None = None) -> tuple[str, "sqlite3.Row | None"]:
    """Mark a task done (idempotent). Records ``handle`` as owner only when the task is
    still unclaimed, so "who did it" is captured for a grab-and-finish.

    A SINGLE atomic ``COALESCE`` UPDATE — NOT a read-then-write. A read-then-write
    (read owner, compute ``owner or h``, then UPDATE) loses to a concurrent
    ``claim_task``: if the claim commits between this op's read and write, the stale
    read overwrites the fresh owner, and two agents end up each believing they own the
    slice — the exact integrity failure the atomic claim was built to prevent.
    ``COALESCE(owner, ?)`` keeps any already-committed owner and only stamps the
    completer when the column was NULL, so the claimer's ownership always survives.

    Returns ``(status, row)``: ``'missing'`` (no such task), ``'already'`` (was already
    done — the UPDATE changed nothing), or ``'done'`` (this call closed it). Callers
    that report a closure (``result --task``) need to tell a real close from a no-op."""
    h = (handle or "").strip().lower() or None
    cur = conn.execute(
        "UPDATE tasks SET status='done', owner=COALESCE(owner, ?) WHERE id=? AND status!='done'",
        (h, task_id),
    )
    conn.commit()
    row = task_by_id(conn, task_id)
    if row is None:
        return ("missing", None)
    return ("done" if cur.rowcount else "already", row)


def list_tasks(conn, include_done: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM tasks"
    if not include_done:
        q += " WHERE status != 'done'"
    q += " ORDER BY id"
    return conn.execute(q).fetchall()


def agent_open_tasks(conn, handle: str) -> list[sqlite3.Row]:
    """A handle's still-to-do tasks (owned, not yet done) — what the briefing surfaces
    as 'Your task(s)' so an agent learns its slice without a human typing it in."""
    h = (handle or "").strip().lower()
    if not h:
        return []
    return conn.execute(
        "SELECT * FROM tasks WHERE status != 'done' AND owner = ? ORDER BY id", (h,)
    ).fetchall()


def task_counts(conn) -> dict:
    """``{open, claimed, done, total}`` — drives the dormant-until-used summaries."""
    rows = conn.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
    by = {r["status"]: r["n"] for r in rows}
    return {"open": by.get("open", 0), "claimed": by.get("claimed", 0),
            "done": by.get("done", 0), "total": sum(by.values())}


def _quote_span(text: str) -> str:
    """Wrap ``text`` in a markdown code span for safe inlining into a chat body.
    parse_mentions / _apply_human_guard / open_escalations all IGNORE code spans, so a
    quoted ``@human`` / ``@someone`` inside it never pings, redirects, escalates, or
    blocks anyone. Backticks in the text are flattened so they can't close the span
    early and leak the tail back into routing."""
    return "`" + (text or "").replace("`", "'") + "`"


def assign_task(conn, handle: str, title: str, paths: str | None = None,
                creator: str | None = None) -> int:
    """Create a task already owned by ``handle`` AND @mention them, so an assignment is
    both DURABLE (a ledger row that survives the 15-line chat scroll) and DELIVERED
    (the mention rides the assignee's cursor / blocks their Stop). Works before the
    assignee has even joined — the row waits in the ledger and the briefing surfaces
    it on their first turn.

    The assignee ``@h`` is the ONLY live mention; the free-text title/paths are quoted
    into a code span so a title like ``ask @human about X`` can't open a phantom
    escalation (which would wedge the lead-done gate), redirect to the lead, harvest a
    spurious rule cite, or block an uninvolved third agent it happens to @mention. The
    full, unquoted title still lives verbatim in the ledger row (and the briefing)."""
    h = (handle or "").strip().lower()
    if not h:
        raise ValueError("assign needs a teammate handle")
    if h in RESERVED_HANDLES:
        raise ValueError(f"'{h}' is reserved and cannot be assigned a task")
    tid = add_task(conn, title, paths=paths, creator=creator, owner=h, status="claimed")
    send(conn, (creator or "system"),
         f"@{h} [assignment] #{tid}: {_quote_span(title.strip())}"
         + (f"  (files: {_quote_span(paths)})" if paths else ""),
         kind="chat")
    return tid


def get_goal(conn) -> str | None:
    """The one-line shared objective, or None when unset."""
    g = get_meta(conn, "goal")
    return g if (g or "").strip() else None


def set_goal(conn, text: str) -> None:
    """Set (or, with empty text, clear) the shared goal."""
    text = (text or "").strip()
    if not text:
        del_meta(conn, "goal")
        return
    set_meta(conn, "goal", text)


def _format_task(r) -> str:
    owner = f"→@{r['owner']}" if r["owner"] else ""
    paths = f"  ({r['paths']})" if r["paths"] else ""
    return f"#{r['id']} [{r['status']}{owner}] {r['title']}{paths}"


# --------------------------------------------------------------------------- #
# Fan-in — structured results back to the orchestrator
# --------------------------------------------------------------------------- #
# The other half of work division: when an agent finishes its slice it posts a
# RESULT, so the orchestrator collects structured outcomes via ``results`` instead of
# prose-grepping the chat log. A result is a ``kind='result'`` message — it rides the
# same bus, but (like every non-chat kind) carries NO @mention, so it never blocks a
# teammate's Stop or gates the barrier. Dormant until used.
def post_result(conn, sender: str, body: str, session_id: str | None = None,
                task_id: int | None = None) -> tuple[int, str | None]:
    """Record a result; return ``(msg_id, task_status)``. With ``task_id`` it also
    closes that task (the natural "finished my slice — here's the outcome") and tags
    the body with the task ref so ``results`` lines outcomes up against the ledger.

    A ``task_id`` that doesn't exist raises ``ValueError`` BEFORE anything is stored —
    so a typo'd/stale id can't poison the fan-in view with a result that references a
    phantom ledger row (and falsely reports it closed). ``task_status`` is
    ``None``/``'done'``/``'already'`` so the caller reports the close honestly."""
    body = (body or "").strip()
    if not body:
        raise ValueError("a result needs a body")
    task_status = None
    if task_id is not None:
        task_status, _ = complete_task(conn, task_id, sender)
        if task_status == "missing":
            raise ValueError(f"no task #{task_id}")
        body = f"[task #{task_id}] {body}"
    return send(conn, sender, body, session_id=session_id, kind="result"), task_status


def list_results(conn, sender: str | None = None,
                 limit: int | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM messages WHERE kind='result'"
    args: list = []
    if sender:
        q += " AND sender = ?"; args.append((sender or "").strip().lower())
    q += " ORDER BY id ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, args).fetchall()


# --------------------------------------------------------------------------- #
# Hierarchy substrate — lead resolution & @human routing (model-agnostic)
# --------------------------------------------------------------------------- #
# Human contact is hub-and-spoke: only the lead may address @human; a worker's
# @human is redirected to @<lead>, so clarifications funnel to one node that batches
# and escalates. This file owns the READ side only — resolve_lead() (who is the lead
# right now) and the send-guard that routes on it. The WRITE side (claiming /
# handing off / releasing the lead role, which set meta['lead']) is a separate,
# decoupled track; the two never co-edit a function. Resolution order is agreed with
# that track (chat #20): the shared pointer wins, then an operator env override, then
# a deterministic FLOOR so a lead ALWAYS exists and fails over the instant one ages
# out — that floor is what kills the single-point-of-failure the research flagged.
def resolve_lead(conn, squad: str | None = None) -> str | None:
    """The handle of the agent who currently owns human contact for ``squad``, or None
    only when no agent is active there. ``squad=None`` resolves the **chair** (the global
    lead — the sole operator contact); a named squad resolves that squad's **captain**
    (within ``active_in_squad``). Order:

    1. the pointer — ``meta['lead']`` for the chair, ``meta['lead:<squad>']`` for a
       captain — **if its holder is currently active in that scope**;
    2. ``$GROUPCHAT_LEAD`` — an operator env override (chair only), if its holder is active;
    3. **floor** — the earliest-joined active agent in the scope (tie broken by handle): a
       deterministic, zero-config default that guarantees a live lead and instant
       failover. The pointer is honoured only while alive, so a parked/dead lead silently
       hands off to the floor — no SPOF, no stale routing.

    Pure/read-only. ``resolve_lead(conn)`` (no squad) is byte-identical to before: the
    chair scope is ``active_agents`` and the key is ``meta['lead']``."""
    acts = active_in_squad(conn, squad) if squad else active_agents(conn)
    if not acts:
        return None
    active_handles = {a["handle"] for a in acts}
    key = "lead" if not squad else f"lead:{_norm_squad(squad)}"
    pointer = (get_meta(conn, key) or "").strip().lower()
    if pointer and pointer in active_handles:
        return pointer
    if not squad:  # the chair honours the operator's env override; squads don't
        env = (_env("LEAD") or "").strip().lower()
        if env and env in active_handles:
            return env
    # Floor: prefer a HOOK-CAPABLE (parks=1) agent — a non-hook host (opencode/generic,
    # parks=0) can't be reliably woken/parked, so it shouldn't silently become the
    # single point of human contact. Fall back to a non-hook agent only if it's the only
    # kind active (an explicit pointer/env above can still designate one deliberately).
    hook_acts = [a for a in acts if a["parks"]]
    floor_pool = hook_acts or acts
    return min(floor_pool, key=lambda a: (a["first_seen"] or "", a["handle"]))["handle"]


def _code_span_ranges(body: str) -> list[tuple[int, int]]:
    """Character ranges covered by markdown inline-code / fenced spans (matched
    backtick runs). Used to leave a *quoted* escalation token untouched — writing
    ``@human`` in docs/help/chat is documentation, not a request to the operator."""
    return [(m.start(), m.end()) for m in CODE_SPAN_RE.finditer(body)]


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def _redirect_mention(body: str, frm: str, to: str) -> str:
    """Rewrite the full mention ``@frm`` → ``@to`` wherever it appears OUTSIDE a code
    span, using the exact mention grammar (so ``@human`` is rewritten but
    ``@humanity`` / ``@human-x`` — distinct handles — are left alone, and a quoted
    `` `@human` `` stays literal). Other mentions pass through untouched."""
    frm = frm.lower()
    spans = _code_span_ranges(body)
    return MENTION_RE.sub(
        lambda m: "@" + to if (m.group(1).lower() == frm
                               and not _in_spans(m.start(), spans)) else m.group(0),
        body)


def _has_unquoted_human(body: str) -> bool:
    """True iff the body contains a real (non-code-span) ``@human`` escalation."""
    spans = _code_span_ranges(body)
    return any(m.group(1).lower() == HUMAN_TOKEN and not _in_spans(m.start(), spans)
               for m in MENTION_RE.finditer(body))


_RE_REF = re.compile(r"\[re #(\d+)\]")


def _re_refs(body: str) -> set:
    """Message ids a reply explicitly answers, parsed from the ``[re #N]`` marker that
    ``answer`` stamps. The captain relay-clear keys on this (a SPECIFIC answered
    escalation) rather than a bare @mention, so ordinary chair→captain chatter
    ('@cap please rebase') can't spuriously clear an open escalation."""
    return {int(m.group(1)) for m in _RE_REF.finditer(body or "")}


def _squad_of_handle(conn, handle: str) -> str | None:
    """The squad of the active agent with ``handle`` (None if unsquadded / not active)."""
    h = (handle or "").strip().lower()
    return next((x["squad"] for x in active_agents(conn) if x["handle"] == h), None)


def human_redirect_target(conn, sender: str, body: str) -> str | None:
    """The handle a sender's @human should be routed to one hop UP the council, or None
    when it passes through to the operator (the sender is the chair) or no redirect
    applies (no active lead, or no *unquoted* @human — a `` `@human` `` in docs never
    escalates). The chain: a worker → its squad lead; a squad lead → the chair; the chair
    → the operator. Unsharded (no squad) → straight to the chair, exactly as before.
    Pure/read-only — the send guard and the CLI feedback note both call this."""
    if not _has_unquoted_human(body):
        return None
    chair = resolve_lead(conn, None)
    if not chair:
        return None  # flat mode — unchanged
    s = (sender or "").strip().lower()
    sq = _squad_of_handle(conn, s)
    if sq:
        captain = resolve_lead(conn, sq)
        if s != captain:
            return captain      # a worker → its squad's captain
        # the captain itself escalates one hop up, to the chair
        return chair if s != chair else None  # captain → chair (or pass through if it IS chair)
    # default-room (no squad): straight to the chair, as today
    return chair if s != chair else None


def _is_captain_escalation(conn, sender: str, target: str) -> bool:
    """True when ``sender`` is a SQUAD CAPTAIN escalating to the chair (target == chair).
    Such an escalation KEEPS its @human so the existing per-session gate parks the captain
    until the chair answers — a worker's redirect strips @human (it delegates, not gated)."""
    s = (sender or "").strip().lower()
    sq = _squad_of_handle(conn, s)
    return bool(sq and s == resolve_lead(conn, sq) and target == resolve_lead(conn, None)
                and s != target)


def _apply_human_guard(conn, sender: str, body: str) -> str:
    """Hub-and-spoke send guard. A worker's @human is rewritten → @<next hop> (stripped,
    delegated). A squad CAPTAIN escalating to the chair KEEPS its @human (so it's gated)
    and the chair is @mentioned for delivery. No-op unless a redirect applies."""
    target = human_redirect_target(conn, sender, body)
    if not target:
        return body
    if _is_captain_escalation(conn, sender, target):
        # Keep @human (gates the captain via session_open_escalations) AND @mention the
        # chair so it receives the escalation to relay. Idempotent: don't double-prepend if
        # the chair is already mentioned (a re-sent body).
        return body if target in parse_mentions(body) else f"@{target} {body}"
    return _redirect_mention(body, HUMAN_TOKEN, target)


def _mentions(row) -> list:
    """A message row's stored ``mentions`` list, parsed FAIL-SAFE: a corrupt / non-list
    value reads as ``[]`` rather than raising. Load-bearing — the escalation gate runs
    inside the Stop hook, and an exception there would escape the gate branch and let an
    agent stop with the operator's answer still owed (mirrors ``_dismissed_set``)."""
    try:
        v = json.loads(row["mentions"] or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def open_escalations(conn, lead: str) -> list[int]:
    """The handle-keyed predecessor of ``session_open_escalations`` — SUPERSEDED in
    production (the Stop hook gates by session, immune to rename/handoff) and retained
    only for its dedicated tests (barrier_escalation_e2e / phase2_lead_escalation), which
    pin the handle-keyed semantics. No production caller; do not add one.

    Message-ids of the lead's @human escalations the operator still owes a reply
    on — the read side of the P2 lead-done gate. Walking chat chronologically: each
    *unquoted* ``@human`` message *by the lead* opens an escalation; an operator
    message (``sender == HUMAN_TOKEN``) that @mentions the lead afterwards clears the
    whole queue (one batched reply answers every pending question, per chat #39).

    The "unquoted" check is load-bearing and mirrors the send-guard: a `` `@human` ``
    inside a code span is the lead *documenting* the token, not asking the operator.
    Counting quoted tokens here was a real barrier-wedge — a lead discussing the
    feature would gate itself on phantom escalations and the team could never reach
    done (gauss #85). parse_mentions/the stored ``mentions`` column ignore code
    spans, so we re-derive from the body via _has_unquoted_human.

    The Stop hook parks a lead while this is non-empty and wakes it via the existing
    @mention path when the operator replies — so the team never tears down with a
    question to the human still unanswered, yet no new state or second cursor is
    introduced. A worker can't appear here: its @human is rewritten to @<lead>
    before storage, so only the lead ever authors an @human escalation."""
    lead = (lead or "").strip().lower()
    if not lead:
        return []
    rows = conn.execute(
        "SELECT id, sender, body, mentions FROM messages WHERE kind='chat' ORDER BY id ASC"
    ).fetchall()
    open_ids: list[int] = []
    for r in rows:
        if r["sender"] == lead and _has_unquoted_human(r["body"]):
            open_ids.append(r["id"])              # a real (unquoted) escalation by the lead
        elif r["sender"] == HUMAN_TOKEN and lead in [m.lower() for m in _mentions(r)]:
            open_ids = []                         # operator answered → queue cleared
    return open_ids


def session_open_escalations(conn, session_id: str) -> list[int]:
    """Open @human escalation ids authored by THIS session — the lead-done gate, keyed
    by SESSION rather than handle so it survives a rename AND a lead handoff. The asker
    stays gated until answered even after it renames or hands off the lead role (only a
    lead ever authors an unquoted @human — a worker's is redirected). Cleared by an
    operator reply (`sender == 'human'`) @mentioning the session's CURRENT handle.

    Matching by session_id is what fixes the orphan: an escalation's stored ``sender``
    is frozen at author time, so a rename/handoff made the old handle-keyed lookup miss
    it (the team could tear down with the operator's answer still owed). Authorship is
    matched STRICTLY on session_id (every real escalation carries one — a worker's/
    unregistered @human is redirected) so a recycled handle can't be mis-gated."""
    a = agent_by_session(conn, session_id)
    if not a:
        return []
    handle = (a["handle"] or "").strip().lower()
    # A CAPTAIN's escalation (the asker is a squad member — only a captain keeps @human; a
    # worker's is stripped) is answered by the CHAIR relaying down, not only by the
    # operator. The clear must be TIME-INVARIANT: we replay immutable history, so keying on
    # the LIVE chair would re-open an already-answered escalation the moment the chair
    # changes (rename / hand-off / floor failover). Instead clear on a reply from a FROZEN
    # addressee (a handle this captain actually escalated to — its escalation @mentions the
    # chair-at-that-time) OR the current chair (a new chair relaying). Scoped to a
    # squad-having asker, so in a FLAT room (no squads) the clause is a strict no-op and
    # only the operator clears — byte-identical to before.
    asker_has_squad = bool(a["squad"])
    chair = resolve_lead(conn, None)
    rows = conn.execute(
        "SELECT id, sender, session_id, body, mentions FROM messages "
        "WHERE kind='chat' ORDER BY id ASC").fetchall()
    open_ids: list[int] = []
    addressees: set[str] = set()
    for r in rows:
        sndr = (r["sender"] or "").strip().lower()
        ments = [m.lower() for m in _mentions(r)]
        if r["session_id"] == session_id and _has_unquoted_human(r["body"]):
            open_ids.append(r["id"])
            if asker_has_squad:
                addressees |= set(ments)          # the chair handle(s) escalated to (frozen)
        elif sndr == HUMAN_TOKEN and handle in ments:
            open_ids = []                         # the operator answered
        elif (asker_has_squad and handle in ments and sndr != handle
              and not _has_unquoted_human(r["body"])
              and (sndr in addressees or (chair and sndr == chair))
              and _re_refs(r["body"]) & set(open_ids)):
            open_ids = []                         # the chair RELAYED an answer down (marked)
    return open_ids


def all_open_escalations(conn) -> dict:
    """``{session_id: [msg_ids]}`` for every unanswered @human escalation room-wide — so
    the operator's ``questions`` view shows what they owe across renames AND handoffs
    (not just the *current* lead's). A queue clears on an operator reply @mentioning that
    session's CURRENT handle — or, for a captain's in-flight escalation, on the CHAIR
    relaying down to it (dormant when unsharded: no captains, the chair never clears its
    own)."""
    agents = {r["session_id"]: r for r in
              conn.execute("SELECT session_id, handle, squad FROM agents").fetchall()}
    sess_handle = {s: (r["handle"] or "").strip().lower() for s, r in agents.items()}
    sess_squad = {s: r["squad"] for s, r in agents.items()}
    handle_sess = {h: s for s, h in sess_handle.items() if h}
    chair = resolve_lead(conn, None)
    rows = conn.execute(
        "SELECT id, sender, session_id, body, mentions FROM messages "
        "WHERE kind='chat' ORDER BY id ASC").fetchall()
    queues: dict = {}
    addressees: dict = {}   # session -> frozen handles it escalated to (captains only)
    for r in rows:
        sndr = (r["sender"] or "").strip().lower()
        ments = [m.lower() for m in _mentions(r)]
        if r["session_id"] and _has_unquoted_human(r["body"]):
            queues.setdefault(r["session_id"], []).append(r["id"])
            if sess_squad.get(r["session_id"]):
                addressees.setdefault(r["session_id"], set()).update(ments)
        elif sndr == HUMAN_TOKEN:
            for m in ments:
                sid = handle_sess.get(m)
                if sid in queues:
                    queues[sid] = []              # the operator answered
        elif not _has_unquoted_human(r["body"]):
            # A captain's queue clears when the chair RELAYS down — time-invariant (the
            # relayer is a FROZEN addressee of that captain's escalation OR the current
            # chair) AND it must carry the explicit [re #id] relay marker, so ordinary
            # chair→captain chatter can't spuriously clear it. Captain (squad) askers only.
            refs = _re_refs(r["body"])
            for m in ments:
                sid = handle_sess.get(m)
                if (sid in queues and sess_squad.get(sid) and sndr != m
                        and (sndr in addressees.get(sid, set()) or (chair and sndr == chair))
                        and refs & set(queues[sid])):
                    queues[sid] = []
    # Only surface queues whose author still has an agent row — a departed/recycled
    # session's question is moot (the asker is gone) and would be unanswerable.
    return {s: ids for s, ids in queues.items() if ids and s in sess_handle}


def pending_relays_for(conn, handle: str) -> list:
    """Open CAPTAIN escalation ids addressed to ``handle`` (a chair) still awaiting its
    relay down. The Stop hook gates the chair on this so it parks until it relays — not
    just while the captain's @mention is still UNREAD (a read-but-unrelayed escalation
    would otherwise let the chair tear down owing a captain an answer). One pass over the
    open escalations; empty (dormant) in a flat / captain-less room."""
    h = (handle or "").strip().lower()
    if not h:
        return []
    sess_squad = {r["session_id"]: r["squad"]
                  for r in conn.execute("SELECT session_id, squad FROM agents").fetchall()}
    out: list = []
    for sid, ids in all_open_escalations(conn).items():
        if not sess_squad.get(sid):          # captains only (squad members with kept @human)
            continue
        for mid in ids:
            r = conn.execute("SELECT mentions FROM messages WHERE id=?", (mid,)).fetchone()
            if r and h in [m.lower() for m in _mentions(r)]:
                out.append(mid)
    return out


# --------------------------------------------------------------------------- #
# Hierarchy substrate — WRITE side (lead claim / hand-off / release)
# --------------------------------------------------------------------------- #
# The decoupled twin of resolve_lead(): the read side honours meta['lead'] only
# while its holder is active, so the write side never has to unset a crashed lead
# — a dead pointer simply fails over to the floor on the next read. Setting the
# pointer is the ONLY write; there is no role column to keep in sync.
def _lead_key(squad: str | None) -> str:
    """The meta pointer key for a lead scope: ``lead`` for the chair (squad=None), else
    ``lead:<squad>`` for a squad's captain."""
    return "lead" if not squad else f"lead:{_norm_squad(squad)}"


def set_lead(conn, handle: str, squad: str | None = None) -> str:
    """Point the lead pointer for ``squad`` at ``handle`` (claim / designate / hand-off);
    ``squad=None`` is the chair. Returns the normalized handle. Honoured by resolve_lead()
    only while that handle is active in the scope."""
    h = (handle or "").strip().lower()
    if not h:
        raise ValueError("lead handle must be non-empty")
    if h in RESERVED_HANDLES:
        raise ValueError(f"'{h}' is reserved and cannot be the lead")
    set_meta(conn, _lead_key(squad), h)
    conn.commit()
    return h


def clear_lead(conn, squad: str | None = None) -> None:
    """Release the designated lead for ``squad`` (None = the chair) → resolve_lead() falls
    back to the floor (the earliest-joined active agent in the scope)."""
    del_meta(conn, _lead_key(squad))
    conn.commit()


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #
def _expand_broadcast(conn, sender: str, mentions: list[str]) -> list[str]:
    """Expand a ``@team`` / ``@all`` broadcast token into active teammates (minus the
    sender), so a broadcast actually @mentions — and thus blocks the Stop of — them. The
    literal token is dropped (reserved). A message without a broadcast token is unchanged.

    Squad-aware: ``@team`` is the sender's OWN SQUAD (a captain's rally doesn't wake other
    squads — squad isolation); ``@all`` is the whole fleet. For an unsquadded sender both
    are the whole fleet (byte-identical to before in a flat room)."""
    toks = BROADCAST_TOKENS & set(mentions)
    if not toks:
        return mentions
    s = (sender or "").strip().lower()
    sq = _squad_of_handle(conn, s)
    if "all" in toks or not sq:
        pool = active_agents(conn)              # fleet-wide (@all, or an unsquadded sender)
    else:
        pool = active_in_squad(conn, sq)        # @team → the sender's squad only
    others = {a["handle"] for a in pool} - {s} - set(RESERVED_HANDLES)
    real = [m for m in mentions if m not in BROADCAST_TOKENS]
    return sorted(set(real) | others)


def send(conn, sender: str, body: str, session_id: str | None = None,
         kind: str = "chat") -> int:
    # Hub-and-spoke routing: a worker's @human is funnelled to the lead before the
    # message is stored, so the mention that blocks/surfaces is @<lead>, not @human.
    # Flat mode (no lead) and non-chat kinds are untouched.
    if kind == "chat":
        body = _apply_human_guard(conn, sender, body)
    # Only chat messages carry @mentions: motions/votes/system must not block a
    # teammate's Stop or gate the barrier (they ride the bus without nagging).
    mentions = parse_mentions(body) if kind == "chat" else []
    if kind == "chat":
        mentions = _expand_broadcast(conn, sender, mentions)
    cur = conn.execute(
        "INSERT INTO messages(ts, sender, session_id, kind, body, mentions) "
        "VALUES (?,?,?,?,?,?)",
        (now_iso(), sender, session_id, kind, body, json.dumps(mentions)),
    )
    msg_id = cur.lastrowid
    # Harvest rule citations — only from real chat messages, and never from one
    # that quotes the constitution itself (self-inflation). Motions, votes, and
    # system announcements naming a rule must NOT count as cites (a motion would
    # otherwise shield the very rule it aims to change; a ratify announcement would
    # self-cite). Cites are the advisory behavioral signal the repeal review runs on.
    if kind == "chat" and "<!-- CONSTITUTION:" not in body:
        for rid in parse_rules(body):
            conn.execute(
                "INSERT INTO rule_cites(ts, rule_id, sender, message_id) "
                "VALUES (?,?,?,?)",
                (now_iso(), rid, sender, msg_id),
            )
    conn.commit()
    return msg_id


def messages_since(conn, after_id: int, limit: int | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM messages WHERE id > ? ORDER BY id ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, (after_id,)).fetchall()


def recent_messages(conn, limit: int = 20) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return list(reversed(rows))


def mark_read(conn, session_id: str, up_to_id: int) -> None:
    conn.execute(
        "UPDATE agents SET last_read_id = MAX(last_read_id, ?) WHERE session_id = ?",
        (up_to_id, session_id),
    )
    conn.commit()


def unread_for(conn, agent_row, include_own: bool = False) -> list[sqlite3.Row]:
    msgs = messages_since(conn, agent_row["last_read_id"])
    if not include_own:
        msgs = [m for m in msgs if m["sender"] != agent_row["handle"]]
    return msgs


def format_message(m: sqlite3.Row, highlight: str | None = None) -> str:
    mentions = _mentions(m)  # fail-safe parse — one corrupt row renders without crashing
    arrow = ""
    if mentions:
        arrow = " → " + " ".join("@" + x for x in mentions)
    tag = "" if m["kind"] == "chat" else f" ({m['kind']})"
    star = ""
    if highlight and highlight.lower() in [x.lower() for x in mentions]:
        star = "★ "
    return f"{star}[#{m['id']} {_hhmm(m['ts'])} {m['sender']}{arrow}]{tag} {m['body']}"


def format_messages(msgs, highlight: str | None = None) -> str:
    return "\n".join(format_message(m, highlight) for m in msgs)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _resolve_for_cli(conn, args):
    """Resolve the acting agent from --session / --from, registering if needed."""
    session_id = getattr(args, "session", None)
    handle = getattr(args, "from_handle", None)
    a = resolve_agent(conn, session_id, handle)
    if a:
        return a
    if session_id:
        register(conn, session_id, cwd=os.getcwd(), pid=os.getpid(), handle=handle)
        return agent_by_session(conn, session_id)
    return None


def cmd_init(args):
    conn = connect()
    print(f"groupchat initialized at {db_path()}")
    conn.close()


def cmd_register(args):
    conn = connect()
    h = register(conn, args.session, cwd=args.cwd or os.getcwd(),
                 pid=args.pid, handle=args.from_handle, status=args.status,
                 parks=not getattr(args, "no_barrier", False))
    print(h)
    conn.close()


def cmd_whoami(args):
    conn = connect()
    a = resolve_agent(conn, getattr(args, "session", None), getattr(args, "from_handle", None))
    print(a["handle"] if a else "(unregistered)")
    conn.close()


def cmd_send(args):
    conn = connect()
    a = _resolve_for_cli(conn, args)
    # Lowercase the unregistered-sender fallback so storage agrees with the lower-cased
    # read side (mentions, last-chat lookups); registered handles are already lower.
    sender = a["handle"] if a else (args.from_handle or "anon").strip().lower()
    body = args.message if isinstance(args.message, str) else " ".join(args.message)
    body = body.strip()
    if not body:
        print("nothing to send (empty message)", file=sys.stderr)
        conn.close()
        return 1
    # Tell a worker when its @human was funnelled to the lead (the guard rewrites
    # the body inside send(); we recompute the target here purely for the notice).
    redirect = human_redirect_target(conn, sender, body)
    mid = send(conn, sender, body, session_id=(a["session_id"] if a else None))
    print(f"sent #{mid} as {sender}")
    if redirect:
        print(f"note: @human redirected to @{redirect} "
              f"(you are not the lead — questions funnel through the lead)")
    conn.close()
    return 0


def cmd_questions(args):
    """Operator view: every open @human escalation awaiting your answer, room-wide —
    so a question whose author renamed or handed off the lead is still visible (not just
    the *current* lead's). The human sees what the fleet needs and how to reply."""
    conn = connect()
    queues = all_open_escalations(conn)
    if not queues:
        print("(no open escalations — the fleet owes you nothing)")
        conn.close()
        return 0
    arows = {r["session_id"]: r for r in
             conn.execute("SELECT session_id, handle, squad FROM agents").fetchall()}
    sess_handle = {s: r["handle"] for s, r in arows.items()}
    chair = resolve_lead(conn, None)
    # A CAPTAIN's escalation (asker is a squad member) is in flight to the chair, who
    # relays; everything else (the chair's own, or an orphaned/handed-off operator
    # escalation) is operator-level — awaiting YOU. Keying on the asker's squad (not on
    # "author == current chair") keeps a flat room's in_flight empty = byte-identical.
    in_flight = {s: ids for s, ids in queues.items()
                 if arows.get(s) and arows[s]["squad"]}
    awaiting = {s: ids for s, ids in queues.items() if s not in in_flight}

    def _show(sid, ids):
        h = sess_handle.get(sid, "?")
        for mid in ids:
            row = conn.execute("SELECT id, ts, body FROM messages WHERE id=?", (mid,)).fetchone()
            if row:
                print(f"  #{row['id']} {_hhmm(row['ts'])} @{h}: {row['body']}")

    if awaiting:
        print('open escalation(s) awaiting you  —  answer with: answer <id> "..."')
        for sid, ids in awaiting.items():
            _show(sid, ids)
    else:
        print("(no escalations awaiting you — the chair owes you nothing)")
    if in_flight:
        n = sum(len(v) for v in in_flight.values())
        print(f"\n{n} escalation(s) in flight to the chair @{chair} (captains awaiting "
              "the chair's relay — not yours to answer directly):")
        for sid, ids in in_flight.items():
            _show(sid, ids)
    conn.close()
    return 0


def cmd_answer(args):
    """Answer / relay an @human escalation. The OPERATOR (bare invocation or
    ``--from human``) posts as 'human' to answer the chair. A relaying CHAIR/lead
    (``--from <handle>``) posts as itself to relay an answer DOWN to a captain — both
    stamp the ``[re #id]`` marker that clears the asker's escalation (a bare @mention
    no longer clears a captain, so ordinary chatter can't). Targets the escalation's
    author and refuses non-escalations, keeping the hub-and-spoke discipline intact."""
    conn = connect()
    mid = args.msg_id
    row = conn.execute(
        "SELECT sender, session_id, mentions FROM messages WHERE id=?", (mid,)).fetchone()
    if not row:
        print(f"no message #{mid}", file=sys.stderr)
        conn.close()
        return 1
    ms = json.loads(row["mentions"] or "[]")
    if HUMAN_TOKEN not in ms:
        print(f"#{mid} is not an @human escalation (mentions: {ms or 'none'}). "
              f'Reply directly with: send --from human "@<handle> ..."',
              file=sys.stderr)
        conn.close()
        return 1
    # Reach the asker by its CURRENT handle (resolved via the frozen author session),
    # so an answer still lands after the lead renamed. Falls back to the frozen sender.
    target = row["sender"]
    author = agent_by_session(conn, row["session_id"]) if row["session_id"] else None
    if author:
        target = author["handle"]
    # The OPERATOR posts as 'human'; a relaying chair/lead (--from <handle>) posts as itself.
    caller = _resolve_for_cli(conn, args)
    # When the OPERATOR answers a captain directly (no --from), it bypasses the chair funnel
    # — inform, don't block. A chair/lead relaying (--from) IS the funnel, so no note.
    if (not caller and author and author["squad"]
            and (target or "").lower() != resolve_lead(conn, None)):
        print(f"note: @{target} is a captain — its escalation usually reaches you via the "
              f"chair @{resolve_lead(conn, None)}. Answering directly bypasses the funnel.",
              file=sys.stderr)
    text = args.message if isinstance(args.message, str) else " ".join(args.message)
    text = text.strip()
    if not text:
        print("nothing to answer (empty message)", file=sys.stderr)
        conn.close()
        return 1
    # The OPERATOR posts as 'human'; a relaying chair/lead (--from <handle>) posts as
    # itself, so a captain's escalation clears via the chair-relay path (marker-gated).
    speaker = caller["handle"] if caller else HUMAN_TOKEN
    new_id = send(conn, speaker, f"@{target} [re #{mid}] {text}",
                  session_id=(caller["session_id"] if caller else None), kind="chat")
    print(f"{'relayed' if caller else 'answered'} #{mid} → @{target} as {speaker} (sent #{new_id})")
    conn.close()
    return 0


def cmd_read(args):
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --session or --from)", file=sys.stderr)
        conn.close()
        return 1
    msgs = unread_for(conn, a, include_own=args.include_own)
    if not msgs:
        print("(no new messages)")
    else:
        print(format_messages(msgs, highlight=a["handle"]))
        if not args.peek:
            mark_read(conn, a["session_id"], msgs[-1]["id"])
    conn.close()
    return 0


def cmd_inbox(args):
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --session or --from)", file=sys.stderr)
        conn.close()
        return 1
    msgs = [m for m in unread_for(conn, a)
            if a["handle"].lower() in [x.lower() for x in _mentions(m)]]
    if not msgs:
        print("(no unread mentions)")
    else:
        print(format_messages(msgs, highlight=a["handle"]))
    # `inbox` is a FILTERED view and is ALWAYS peek-only: the single monotonic cursor
    # can't represent "read this @mention but not an earlier non-mention broadcast", so
    # advancing to the last mention's id would silently skip lower-id chatter. Full
    # delivery (which advances) is `read` / the hooks. (--peek kept as a no-op alias.)
    conn.close()
    return 0


def cmd_log(args):
    conn = connect()
    msgs = recent_messages(conn, args.limit)
    print(format_messages(msgs) if msgs else "(no messages yet)")
    conn.close()


def _fmt_count(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def cmd_tokens(args):
    conn = connect()
    rows = active_agents(conn) if not args.all else conn.execute(
        "SELECT * FROM agents ORDER BY handle").fetchall()
    if not rows:
        print("(no agents)")
        conn.close()
        return 0
    print("~approx (from local transcript)")
    tot = {k: 0 for k in TOKEN_FIELDS}
    for r in rows:
        for k in TOKEN_FIELDS:
            tot[k] += int(r[k] or 0)
        print(f"{r['handle']:<10} out {_fmt_count(r['out_tokens'])}  "
              f"in {_fmt_count(r['in_tokens'])}  "
              f"cache-read {_fmt_count(r['cache_read_tokens'])}  "
              f"cache-create {_fmt_count(r['cache_create_tokens'])}")
    print(f"{'TEAM':<10} out {_fmt_count(tot['out_tokens'])}  "
          f"in {_fmt_count(tot['in_tokens'])}  "
          f"cache-read {_fmt_count(tot['cache_read_tokens'])}  "
          f"cache-create {_fmt_count(tot['cache_create_tokens'])}")
    conn.close()
    return 0


def cmd_who(args):
    conn = connect()
    actives = active_agents(conn)  # computed once, reused for render + summary + cwd
    rows = actives if not args.all else conn.execute(
        "SELECT * FROM agents ORDER BY handle").fetchall()
    if not rows:
        print("(no agents)")
    # Mark a DELIBERATE lead (explicit claim/designation/env) in the roster — not the
    # implicit floor, so flat / floor-only rooms stay uncluttered (no surprise crown).
    lead = resolve_lead(conn)
    _ptr = (get_meta(conn, "lead") or "").strip().lower()
    _envlead = (_env("LEAD") or "").strip().lower()
    explicit_lead = lead if (lead and lead in (_ptr, _envlead)) else None
    # Per-squad captains who are DESIGNATED (an explicit lead:<squad> pointer, not the
    # implicit floor) get a ★captain crown — so a sharded roster shows the council.
    squad_captain = {}
    for sq in {r["squad"] for r in rows if r["squad"]}:
        ptr = (get_meta(conn, _lead_key(sq)) or "").strip().lower()
        if ptr:
            squad_captain[sq] = ptr
    chat_ages = last_chat_ages(conn)        # one grouped query, not N
    solo = len(actives) <= 1                # the quiet ◐ has no consumer when alone
    for r in rows:
        # ● active · ◐ active-but-quiet (soft stuck/heads-down signal) · ○ idle.
        if not _is_active(r["last_seen"]):
            flag = "○"
        elif not solo and is_quiet(conn, r, chat_age=chat_ages.get(r["handle"], float("inf"))):
            flag = "◐"
        else:
            flag = "●"
        if explicit_lead and r["handle"] == explicit_lead:
            crown = " ★lead"
        elif r["squad"] and squad_captain.get(r["squad"]) == r["handle"]:
            crown = " ★captain"
        else:
            crown = ""
        sq = f" ·squad:{r['squad']}" if r["squad"] else ""
        status = f" — {r['status']}" if r["status"] else ""
        foc = f" ▸ {r['focus']}" if r["focus"] else ""
        cwd = f"  [{r['cwd']}]" if r["cwd"] else ""
        toks = f" · {_fmt_count(r['out_tokens'])} out" if r["out_tokens"] else ""
        print(f"{flag} {r['handle']}{crown}{sq}{status}{foc}{cwd}  "
              f"(seen {_hhmm(r['last_seen'] or '')}){toks}")

    # Team summary — how many instances are working, and against what target. Lets a
    # human (and an agent reading `who`) see the count at a glance.
    n = len(actives)
    if n:
        done = sum(1 for a in actives if (a["status"] or "") == DONE_STATUS)
        size = expected_team_size(conn)
        if size:
            line = f"team: {n} active · {done} done · expecting {size}"
            if size > n:
                line += f" · {size - n} not yet joined"
        elif n == 1:
            line = "working solo — no team to wait for"
        else:
            line = f"team: {n} active · {done} done"
        print(line)
        # Per-squad breakdown when the fleet is sharded (each squad has its own barrier).
        squads = sorted({a["squad"] for a in actives if a["squad"]})
        for sq in squads:
            members = active_in_squad(conn, sq)
            sdone = sum(1 for a in members if (a["status"] or "") == DONE_STATUS)
            ssize = expected_team_size(conn, sq)
            sline = f"  squad {sq}: {len(members)} active · {sdone} done"
            if ssize:
                sline += f" · expecting {ssize}"
            print(sline)

    # Coordination state — shown only when there is *live* work, so the roster goes
    # quiet once everything is done (matching the briefing's dormancy; a lingering
    # all-done tally would otherwise stick forever). `task list --all` still has it.
    goal = get_goal(conn)
    if goal:
        print(f"goal: {goal}")
    tc = task_counts(conn)
    if tc["open"] or tc["claimed"]:
        print(f"tasks: {tc['open']} open · {tc['claimed']} claimed · {tc['done']} done")
    if standdown_active(conn):
        print("⚠ standdown active — agents are released from the barrier "
              "(`standdown --clear` to lift)")

    # Shared working tree — two+ active agents in one cwd (no worktree isolation) is the
    # high-collision config. Dormant when each has its own tree (worktrees) or is solo.
    bycwd: dict = {}
    for a in actives:
        if a["cwd"]:
            bycwd.setdefault(os.path.abspath(a["cwd"]), []).append(a["handle"])
    for hs in bycwd.values():
        if len(hs) > 1:
            print(f"⚠ shared working tree: {', '.join('@' + h for h in hs)} — flag files "
                  "before editing (or use `bootstrap --worktree`)")
    # Active file claims (who's editing what).
    cl = active_claims(conn)
    if cl:
        print("claims: " + "; ".join(f"@{c['handle']} {c['glob']}" for c in cl))
    # Open parliamentary session, if any (governance framing; dormant otherwise).
    ps = parl_session(conn)
    if ps:
        print(f"session: {ps['title']} ({len(agenda_items(conn, ps['id']))} open agenda item(s))")
    conn.close()


def cmd_lead(args):
    """Show / claim / hand off / release the lead — the WRITE side of hub-and-spoke
    @human routing. Forms:
        lead                       show who's lead and why (claim / env / floor)
        lead <handle>              designate / hand off to <handle>
        lead --claim --from <me>   claim the lead for yourself (emergent self-claim)
        lead --release             step down → the deterministic floor takes over
    A human can also designate out-of-band via env GROUPCHAT_LEAD, or by ratifying an
    election. resolve_lead() (read side) honours the pointer only while its holder is
    active, so a parked/crashed lead auto-fails-over to the floor — no manual cleanup."""
    conn = connect()
    # Scope: the caller's SQUAD captaincy by default (a squad agent steers its own squad);
    # the global CHAIR when --chair is passed or the caller is in the default room. So
    # `lead --claim` makes you your squad's captain; `lead --chair --claim` makes you chair.
    caller = _resolve_for_cli(conn, args)
    scope = None if getattr(args, "chair", False) else (caller["squad"] if caller else None)
    # The global lead is "the lead" in a flat room (byte-identical wording) and "the chair"
    # once squads exist (council framing); a squad's is always "captain".
    _sharded = any(a["squad"] for a in active_agents(conn))
    role = (("chair" if _sharded else "lead") if not scope
            else f"captain of squad '{scope}'")
    if getattr(args, "release", False):
        prev = get_meta(conn, _lead_key(scope))
        clear_lead(conn, scope)
        now = resolve_lead(conn, scope)
        send(conn, "system",
             f"{role.capitalize()} released{f' (was @{prev})' if prev else ''} — "
             + (f"the floor is now @{now} (earliest-joined active)."
                if now else "no agents active in scope."),
             kind="system")
        print(f"released the {role} → floor is now @{now}" if now
              else f"released the {role} (no active agents)")
        conn.close()
        return 0
    target = None
    if getattr(args, "claim", False):
        if not caller:
            print("lead --claim needs your identity: pass --from <your handle> "
                  "(or --session <id>)", file=sys.stderr)
            conn.close()
            return 1
        target = caller["handle"]
    elif getattr(args, "handle", None):
        target = args.handle
    if target:
        # The lead must be an *active* agent that can actually receive routed
        # @mentions. resolve_lead honors the pointer only while its holder is active,
        # so designating an inactive/unknown handle would silently fall through to the
        # floor — yet still broadcast "route to @<h>", and a worker addressing @<h>
        # directly would be lost (audit #70). Refuse it (reserved/empty still get
        # set_lead's specific message).
        _t = (target or "").strip().lower()
        scope_actives = {x["handle"] for x in (active_in_squad(conn, scope) if scope
                                               else active_agents(conn))}
        if _t and _t not in RESERVED_HANDLES and _t not in scope_actives:
            where = f"in squad '{scope}'" if scope else "active"
            print(f"@{_t} is not {where} — can't be the {role}. A lead must be active in "
                  f"its scope so a worker's @human reaches it; an inactive pointer would "
                  f"silently fall through to the floor (see `lead` / `who` / `council`).",
                  file=sys.stderr)
            conn.close()
            return 1
        try:
            h = set_lead(conn, target, scope)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            conn.close()
            return 1
        dest = "the operator" if not scope else "the chair"
        send(conn, "system",
             f"@{h} is now the {role}. {('Workers' if not scope else 'Squad ' + scope)}: "
             f"your @human routes to @{h}, who batches questions for {dest}.",
             kind="system")
        print(f"{role} is now @{h}")
        conn.close()
        return 0
    # show
    lead = resolve_lead(conn, scope)
    if not lead:
        print(f"no active agents in scope — flat (no {role})")
        conn.close()
        return 0
    pointer = (get_meta(conn, _lead_key(scope)) or "").strip().lower()
    env = (_env("LEAD") or "").strip().lower()
    actives = {x["handle"] for x in (active_in_squad(conn, scope) if scope
                                     else active_agents(conn))}
    if pointer and pointer in actives:
        why = "claimed / designated"
    elif not scope and env and env in actives:
        why = "operator env GROUPCHAT_LEAD"
    else:
        why = "floor — earliest-joined active (emergent default)"
    print(f"{role}: @{lead}  [{why}]")
    if pointer and pointer not in actives:
        print(f"  note: designated @{pointer} is inactive — failed over to the floor")
    conn.close()
    return 0


def cmd_council(args):
    """Show the council: the chair (sole operator contact) + each squad's captain. The
    @human funnel climbs worker → squad captain → chair → operator. Dormant-friendly: in
    an unsharded room it just shows the single lead."""
    conn = connect()
    chair = resolve_lead(conn, None)
    if not chair:
        print("no active agents — flat (no council)")
        conn.close(); return 0
    squads = sorted({a["squad"] for a in active_agents(conn) if a["squad"]})
    print(f"chair: @{chair}  (the single operator contact)")
    if not squads:
        print("  (no squads — flat room; @human → the chair → operator)")
    for sq in squads:
        cap = resolve_lead(conn, sq)
        members = [a["handle"] for a in active_in_squad(conn, sq)]
        print(f"  squad {sq}: captain @{cap}  ({len(members)} member(s): "
              f"{', '.join(members)})")
    conn.close(); return 0


def cmd_heartbeat(args):
    conn = connect()
    register(conn, args.session, cwd=args.cwd, pid=args.pid, status=args.status)
    conn.close()


def cmd_done(args):
    """Mark this agent's slice complete. The Stop hook does this automatically;
    this is the explicit version for clarity."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --session or --from)", file=sys.stderr)
        conn.close()
        return 1
    set_status(conn, a["session_id"], DONE_STATUS)
    done = team_done(conn, a["squad"])  # this agent's squad barrier
    where = f" (squad {a['squad']})" if a["squad"] else ""
    unit = "Squad" if a["squad"] else "Team"
    print(f"{a['handle']} marked done{where}."
          + (f" {unit} is all done." if done else " Waiting for teammates."))
    conn.close()
    return 0


def cmd_expect(args):
    """Declare how many agents this run should have (closes the startup race exactly).
    ``--squad <name>`` declares a sub-team's size; with no number, print the current
    expectation. Each squad has its own barrier."""
    conn = connect()
    squad = _norm_squad(getattr(args, "squad", None))
    label = f"squad '{squad}'" if squad else "team"
    if args.n is None:
        size = expected_team_size(conn, squad)
        print(f"expected {label} size: {size}" if size
              else f"expected {label} size: (unset — using startup grace)")
    elif args.n <= 0:
        set_team_size(conn, args.n, squad)
        print(f"expected {label} size cleared (using startup grace)")
    else:
        set_team_size(conn, args.n, squad)
        print(f"expected {label} size set to {args.n}")
    conn.close()
    return 0


def cmd_squad(args):
    """Show / join a squad — a sub-team with its OWN barrier (a finished squad tears down
    independently; the lead / @human funnel stays global). Empty = the default room."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --from <your handle> or --session)", file=sys.stderr)
        conn.close(); return 1
    if args.name:
        raw = " ".join(args.name).strip()
        sq = _norm_squad(raw)
        if raw and not sq:
            print(f"'{raw}' isn't a usable squad name (a-z, 0-9, '-', '_') — "
                  "staying in the default room.", file=sys.stderr)
            conn.close(); return 1
        cur = agent_by_session(conn, a["session_id"])["squad"]
        if sq != cur:
            # Re-stamp first_seen so the NEW squad's cohort age (startup grace / solo
            # settle) is honest about this agent just arriving — not its original join.
            conn.execute("UPDATE agents SET squad=?, first_seen=? WHERE session_id=?",
                         (sq, now_iso(), a["session_id"]))
            conn.commit()
        print(f"joined squad '{sq}'" if sq else "left your squad (back to the default room)")
        conn.close(); return 0
    cur = agent_by_session(conn, a["session_id"])["squad"]
    if not cur:
        print("you're in the default room (no squad)")
    else:
        mates = [x["handle"] for x in active_in_squad(conn, cur) if x["handle"] != a["handle"]]
        print(f"squad '{cur}'" + (f" with {', '.join(mates)}" if mates else " (just you so far)"))
    conn.close(); return 0


def cmd_model(args):
    """Show / set your MODEL — used only to annotate the advisory vote tally with model
    DIVERSITY (a homogeneous-fleet sweep is flagged low-independence). It never binds.
    Usually set at launch via $AGORA_MODEL; this is the runtime form."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --from <your handle> or --session)", file=sys.stderr)
        conn.close(); return 1
    if args.name:
        raw = " ".join(args.name).strip()
        mdl = _norm_model(raw)
        if raw and not mdl:
            print(f"'{raw}' isn't a usable model id (a-z, 0-9, '.', '-', '_') — "
                  "model unchanged.", file=sys.stderr)
            conn.close(); return 1
        conn.execute("UPDATE agents SET model=? WHERE session_id=?", (mdl, a["session_id"]))
        conn.commit()
        print(f"model set to '{mdl}'" if mdl else "model cleared (unknown)")
        conn.close(); return 0
    cur = agent_by_session(conn, a["session_id"])["model"]
    print(f"your model: {cur}" if cur else "your model: (unknown — set via $AGORA_MODEL or `model <id>`)")
    conn.close(); return 0


def cmd_task(args):
    """Work-division ledger: add / list / claim / done. The durable substrate that
    lets an agent learn its slice from the bus instead of a human typing it in, with
    an atomic claim so two agents can't both grab the same task."""
    conn = connect()
    action = args.action
    if action == "add":
        title = " ".join(args.rest).strip() if args.rest else ""
        if not title:
            print('a task needs a title: task add "<what to do>"', file=sys.stderr)
            conn.close(); return 1
        tid = add_task(conn, title, paths=getattr(args, "paths", None),
                       creator=getattr(args, "from_handle", None))
        print(f"added task #{tid}: {title}"
              + (f"  (files: {args.paths})" if getattr(args, "paths", None) else ""))
        conn.close(); return 0
    if action == "list":
        rows = list_tasks(conn, include_done=args.all)
        if not rows:
            print("(no tasks)")
        else:
            for r in rows:
                print(_format_task(r))
            cnt = task_counts(conn)
            print(f"— {cnt['open']} open · {cnt['claimed']} claimed · {cnt['done']} done")
        conn.close(); return 0
    # claim / done need an identity and a task id. Treat an out-of-SQLite-range id as
    # simply missing (binding it would raise OverflowError → an ugly traceback).
    if not (0 < args.id <= 2**63 - 1):
        print(f"no task #{args.id}", file=sys.stderr); conn.close(); return 1
    a = _resolve_for_cli(conn, args)
    who = a["handle"] if a else getattr(args, "from_handle", None)
    if action == "claim":
        if not who:
            print("who is claiming? pass --from <your handle>", file=sys.stderr)
            conn.close(); return 1
        result, row = claim_task(conn, args.id, who)
        if result == "missing":
            print(f"no task #{args.id}", file=sys.stderr); conn.close(); return 1
        if result == "taken":
            print(f"#{args.id} is already claimed by @{row['owner']} — coordinate in "
                  f"chat before taking it.", file=sys.stderr)
            conn.close(); return 1
        if result == "done":
            print(f"#{args.id} is already done.", file=sys.stderr)
            conn.close(); return 1
        print(f"claimed #{args.id} as @{who}: {row['title']}")
        conn.close(); return 0
    if action == "done":
        result, _row = complete_task(conn, args.id, who)
        if result == "missing":
            print(f"no task #{args.id}", file=sys.stderr); conn.close(); return 1
        print(f"marked task #{args.id} done")
        conn.close(); return 0
    print(f"unknown task action '{action}'", file=sys.stderr)
    conn.close(); return 1


def cmd_assign(args):
    """Hand a specific teammate a task: create it already owned by them and @mention
    them. Durable (a ledger row) + delivered (rides their cursor / blocks their Stop).
    Sugar over ``task add`` + a notify, but in one race-free step."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    creator = a["handle"] if a else getattr(args, "from_handle", None)
    title = " ".join(args.title).strip() if args.title else ""
    if not title:
        print('assign needs a title: assign <handle> "<what to do>"', file=sys.stderr)
        conn.close(); return 1
    try:
        tid = assign_task(conn, args.handle, title, paths=args.paths, creator=creator)
    except ValueError as e:
        print(str(e), file=sys.stderr); conn.close(); return 1
    print(f"assigned #{tid} to @{args.handle.strip().lower()}: {title}")
    conn.close(); return 0


def cmd_goal(args):
    """Show / set / clear the one-line shared objective every agent sees in its
    briefing and in ``who``."""
    conn = connect()
    if getattr(args, "clear", False):
        set_goal(conn, "")
        print("goal cleared")
        conn.close(); return 0
    text = " ".join(args.text).strip() if args.text else ""
    if not text:
        g = get_goal(conn)
        print(f"goal: {g}" if g else 'no goal set (`goal "<objective>"` to set one)')
        conn.close(); return 0
    set_goal(conn, text)
    print(f"goal set: {text}")
    conn.close(); return 0


def cmd_result(args):
    """Report a structured result back to the orchestrator (`kind='result'`). With
    `--task N` it also closes that task."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    # Lowercase the unregistered-sender fallback so storage agrees with the
    # case-insensitive `results --from` filter (registered handles are already lower).
    sender = a["handle"] if a else (args.from_handle or "anon").strip().lower()
    body = " ".join(args.message).strip() if args.message else ""
    if not body:
        print("nothing to report (empty result)", file=sys.stderr)
        conn.close(); return 1
    if args.task is not None and not (0 < args.task <= 2**63 - 1):
        print(f"no task #{args.task}", file=sys.stderr); conn.close(); return 1
    try:
        mid, tstatus = post_result(conn, sender, body,
                                   session_id=(a["session_id"] if a else None),
                                   task_id=args.task)
    except ValueError as e:
        print(str(e), file=sys.stderr); conn.close(); return 1
    note = ""
    if tstatus == "done":
        note = f" (closed task #{args.task})"
    elif tstatus == "already":
        note = f" (task #{args.task} was already done)"
    print(f"result #{mid} recorded as {sender}{note}")
    conn.close(); return 0


def cmd_results(args):
    """Collect the results agents have reported — the orchestrator's fan-in view."""
    conn = connect()
    rows = list_results(conn, sender=getattr(args, "from_handle", None))
    if not rows:
        print("(no results reported yet)")
        conn.close(); return 0
    for r in rows:
        print(f"#{r['id']} {_hhmm(r['ts'])} {r['sender']}: {r['body']}")
    print(f"— {len(rows)} result(s)")
    conn.close(); return 0


def cmd_summary(args):
    """A read-only one-shot digest of the room: goal, roster, tasks, results — so a
    human or an orchestrator gets the whole picture without four separate calls."""
    conn = connect()
    goal = get_goal(conn)
    print(f"Goal: {goal}" if goal else "Goal: (none set)")

    actives = active_agents(conn)
    done = sum(1 for a in actives if (a["status"] or "") == DONE_STATUS)
    roster = ", ".join(
        f"{a['handle']}{' ✓' if (a['status'] or '') == DONE_STATUS else ''}" for a in actives)
    print(f"Agents: {len(actives)} active · {done} done"
          + (f" — {roster}" if roster else ""))

    tc = task_counts(conn)
    print(f"Tasks: {tc['open']} open · {tc['claimed']} claimed · {tc['done']} done")
    for r in list_tasks(conn, include_done=False):
        print(f"  {_format_task(r)}")

    results = list_results(conn)
    print(f"Results: {len(results)}")
    for r in results:
        print(f"  [{r['sender']}] {r['body']}")
    conn.close(); return 0


def cmd_worktrees(args):
    """Read-only, DIFF-ONLY reconciliation of the `bootstrap --worktree` branches:
    each `groupchat/<name>` branch's ahead/behind + changed files, cross-branch file
    overlaps, and an advisory merge order. Never merges anything — the operator runs
    the merges from this report."""
    cwd = args.cwd or os.getcwd()
    if _git_out(["rev-parse", "--git-dir"], cwd) is None:
        print("not a git repository (worktree reconciliation needs git)", file=sys.stderr)
        return 1
    # Resolve and VALIDATE the base up front: an unresolvable ref would otherwise make
    # every branch read +0/-0 — a false "all clean / nothing to merge" report.
    base = args.base or _default_base(cwd)
    if _git_out(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], cwd) is None:
        print(f"base ref '{base}' does not resolve to a commit "
              "(pass a valid --base).", file=sys.stderr)
        return 1
    rep = worktree_report(cwd, base=base)
    if not rep["branches"]:
        print("no groupchat/* worktree branches found "
              "(nothing from `bootstrap --worktree` to reconcile).")
        return 0
    print(f"worktree branches vs {rep['base']} (read-only — diff, never merge):")
    for b in rep["branches"]:
        print(f"  {b['branch']}: +{b['ahead']}/-{b['behind']} commits, "
              f"{len(b['files'])} file(s) (+{b['insertions']}/-{b['deletions']})")
        for f in b["files"]:
            print(f"      {f}")
    if rep["overlaps"]:
        print("\n⚠ file overlaps (the same path edited on >1 branch — merge carefully):")
        for o in rep["overlaps"]:
            print(f"  {o['file']}: {', '.join(o['branches'])}")
    else:
        print("\nno file overlaps — branches touch disjoint files.")
    print("\nsuggested merge order (smallest blast radius first):")
    print("  " + " → ".join(rep["order"]))
    # shlex.quote the suggestion so a copy-pasted branch name with shell metachars is
    # safe in the operator's shell (the tool itself never runs this).
    first = shlex.quote(rep["order"][0]) if rep["order"] else "<branch>"
    print(f"\nMerge them yourself, e.g.:  git merge {first}")
    return 0


def cmd_direct(args):
    """Imperatively redirect a teammate: an @mention (blocks their Stop, picked up on
    their next turn) after checking the target is active."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    sender = a["handle"] if a else (args.from_handle or "anon").strip().lower()
    target = (args.handle or "").strip().lower()
    if target not in {x["handle"] for x in active_agents(conn)}:
        print(f"@{target} is not an active agent — can't direct it (`who` for the "
              f"roster).", file=sys.stderr)
        conn.close(); return 1
    msg = " ".join(args.message).strip() if args.message else ""
    if not msg:
        print("nothing to direct (empty message)", file=sys.stderr)
        conn.close(); return 1
    mid = send(conn, sender, f"@{target} {msg}",
               session_id=(a["session_id"] if a else None))
    print(f"directed @{target} (#{mid})")
    conn.close(); return 0


def _control_caller_ok(conn, args):
    """May the caller run a control action (standdown/dismiss)? Returns
    ``(ok, caller, lead)``. The lead, the operator (sender 'human'), and a BARE CLI
    invocation (no identity = the operator at the terminal) pass; a known worker (a
    resolved/named non-lead agent) is rejected. ``--from`` is unauthenticated like
    everywhere else — this is a guardrail against a worker *agent* disbanding the
    fleet, not a security boundary. In particular ``--from human`` is forgeable BY
    DESIGN (a bare invocation already IS the operator), so the gate stops an *honest*
    worker, not a determined one — consistent with the soft-guard model throughout."""
    a = _resolve_for_cli(conn, args)
    caller = (a["handle"] if a
              else (getattr(args, "from_handle", None) or "").strip().lower() or None)
    lead = resolve_lead(conn)
    ok = caller is None or caller == lead or caller == HUMAN_TOKEN
    return ok, caller, lead


def cmd_standdown(args):
    """Declare (or lift) a team-wide standdown — every parked agent is released from
    the barrier within a poll tick. The teardown switch for a whole fleet. Lead/operator
    only (a worker can't disband the fleet)."""
    conn = connect()
    ok, caller, lead = _control_caller_ok(conn, args)
    if not ok:
        print(f"only the lead (@{lead}) or the operator can call a standdown "
              f"(you are @{caller}).", file=sys.stderr)
        conn.close(); return 1
    if getattr(args, "clear", False):
        clear_standdown(conn)
        send(conn, "system", "Standdown lifted — the team barrier is back to normal.",
             kind="system")
        print("standdown lifted")
        conn.close(); return 0
    reason = " ".join(args.reason).strip() if args.reason else ""
    set_standdown(conn, reason or None)
    send(conn, "system",
         "Standdown — all agents may stop now and leave the barrier."
         + (f" ({reason})" if reason else ""), kind="system")
    print("standdown declared — parked agents are released within a poll tick "
          "(`standdown --clear` to lift).")
    conn.close(); return 0


def cmd_dismiss(args):
    """Release ONE agent from the barrier (lead/operator action) — so a still-active
    orchestrator doesn't pin its finished workers to the park ceiling."""
    conn = connect()
    ok, caller, lead = _control_caller_ok(conn, args)
    if not ok:
        print(f"only the lead (@{lead}) or the operator can dismiss agents "
              f"(you are @{caller}).", file=sys.stderr)
        conn.close(); return 1
    target = (args.handle or "").strip().lower()
    sid = dismiss_agent(conn, target)
    if not sid:
        print(f"@{target} is not an active agent to dismiss.", file=sys.stderr)
        conn.close(); return 1
    send(conn, "system",
         f"@{target} dismissed from the barrier by @{caller or 'the operator'} — "
         "it may stop now.", kind="system")
    print(f"dismissed @{target}")
    conn.close(); return 0


def cmd_focus(args):
    """Set / clear / show your current-work focus — what `who` and the briefing show
    teammates you're on right now (distinct from the barrier status)."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --from <your handle> or --session)", file=sys.stderr)
        conn.close(); return 1
    if getattr(args, "clear", False):
        set_focus(conn, a["session_id"], "")
        print("focus cleared")
        conn.close(); return 0
    text = " ".join(args.text).strip() if args.text else ""
    if not text:
        cur = agent_by_session(conn, a["session_id"])["focus"]
        print(f"focus: {cur}" if cur else "(no focus set)")
        conn.close(); return 0
    set_focus(conn, a["session_id"], text)
    print(f"focus set: {text}")
    conn.close(); return 0


def cmd_claim(args):
    """Announce intent to edit files matching a glob — a structured 'I'm on these files'
    teammates see in `claims` and their briefing (soft, advisory collision-safety)."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --from <your handle>)", file=sys.stderr)
        conn.close(); return 1
    glob = " ".join(args.glob).strip() if args.glob else ""
    try:
        add_claim(conn, a["session_id"], a["handle"], glob)
    except ValueError as e:
        print(str(e), file=sys.stderr); conn.close(); return 1
    others = path_claimed_by(conn, glob, exclude_session=a["session_id"])
    print(f"claimed {glob} as @{a['handle']}")
    if others:
        print("  ⚠ overlaps an existing claim: "
              + ", ".join(f"@{h} ({g})" for h, g in others))
    conn.close(); return 0


def cmd_unclaim(args):
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --from <your handle>)", file=sys.stderr)
        conn.close(); return 1
    glob = " ".join(args.glob).strip() if args.glob else ""
    n = release_claim(conn, a["session_id"], glob)
    print(f"released {glob}" if n else f"(no claim {glob} to release)")
    conn.close(); return 0


def cmd_claims(args):
    """List active file claims, or (`--path P`) who has claimed a given path."""
    conn = connect()
    if getattr(args, "path", None):
        who = path_claimed_by(conn, args.path)
        if not who:
            print(f"(no active claim on {args.path})")
        else:
            for h, g in who:
                print(f"@{h} claims {g}")
        conn.close(); return 0
    rows = active_claims(conn)
    if not rows:
        print("(no active claims)")
    else:
        for r in rows:
            print(f"@{r['handle']}: {r['glob']}")
        print(f"— {len(rows)} active claim(s)")
    conn.close(); return 0


def cmd_rename(args):
    """Change your handle in place — keeps your session, history, and read cursor."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    if not a:
        print("(no agent identity; pass --from <current-handle> or --session)",
              file=sys.stderr)
        conn.close()
        return 1
    try:
        old, new = rename_agent(conn, a["session_id"], args.new)
    except ValueError as e:
        print(f"rename failed: {e}", file=sys.stderr)
        conn.close()
        return 1
    if old == new:
        print(f"already named {new} (no change)")
    else:
        print(f"renamed: {old} → {new}\n"
              f"Post as `--from {new}` from now on (your session keeps its history).")
    conn.close()
    return 0


def cmd_bootstrap(args):
    """Spawn other agent sessions and map them to free team-member handles. The
    'ask the human how many' UX lives in the /groupchat:team command (Claude-driven);
    this CLI just resolves names and launches."""
    conn = connect()
    specs = args.spec or []
    method = args.method or _default_spawn_method()
    cwd = args.cwd or os.getcwd()
    prompt_map: dict = {}
    if len(specs) == 1 and specs[0].isdigit():
        count = int(specs[0])
        if count < 1:
            print("count must be ≥ 1", file=sys.stderr)
            conn.close()
            return 1
        names = _pick_free_handles(conn, count)
    elif specs:
        # Each spec is a name or ``name:prompt`` — split off any per-agent prompt
        # BEFORE handle resolution (the colon would otherwise be sanitized into the
        # handle), then map the resolved handle (order-preserving) back to its prompt.
        parsed = [_parse_spec(s) for s in specs]
        names = _pick_free_handles(conn, len(parsed),
                                   explicit=[p[0] for p in parsed])
        prompt_map = {names[i]: parsed[i][1]
                      for i in range(len(names)) if parsed[i][1]}
    else:
        print("specify how many teammates (e.g. `bootstrap 3`) or their names "
              "(e.g. `bootstrap frontend backend`).", file=sys.stderr)
        conn.close()
        return 1
    if len(names) > BOOTSTRAP_MAX and not args.force:
        print(f"refusing to spawn {len(names)} agents (cap {BOOTSTRAP_MAX}); "
              f"pass --force to override.", file=sys.stderr)
        conn.close()
        return 1
    # How many agents are already here (incl. the bootstrapper) — so the declared
    # size below counts the whole team, not just the newcomers. With --squad, count the
    # SQUAD's existing members (the bootstrapper usually isn't in the spawned squad).
    boot_squad = _norm_squad(getattr(args, "squad", None))
    n_active_before = len(active_agents(conn))
    n_squad_before = len(active_in_squad(conn, boot_squad)) if boot_squad else n_active_before
    conn.close()

    # Safe-autonomous-spawn backstops (the runaway-recursion / fleet-blowup guards).
    # A spawned agent that itself runs bootstrap inherits a deeper GROUPCHAT_SPAWN_DEPTH;
    # refuse past the max so recursive fan-out can't be unbounded, and past the live
    # fleet ceiling. --force (a human's explicit override) bypasses both.
    depth = current_spawn_depth()
    if depth >= max_spawn_depth() and not args.force:
        print(f"refusing to spawn: spawn depth {depth} is at the limit "
              f"({max_spawn_depth()}) — this looks like runaway recursion. A human can "
              f"pass --force; an agent should NOT spawn deeper.", file=sys.stderr)
        return 1
    if n_active_before + len(names) > max_fleet() and not args.force:
        print(f"refusing to spawn: would put {n_active_before + len(names)} agents in "
              f"the room, over the fleet ceiling ({max_fleet()}). Pass --force to "
              f"override.", file=sys.stderr)
        return 1

    # Thread spawn lineage to the children: their depth is ours + 1, and we record
    # ourselves as the spawner (the agent running bootstrap, if it has a handle).
    spawner = _env("HANDLE") or "bootstrap"
    # A same-host bootstrap IS the homogeneous-fleet capture case: let the orchestrator
    # stamp the spawned agents' model (--model, else inherit the bootstrapper's own
    # $AGORA_MODEL) so the vote-tally diversity signal isn't inert. None = unknown.
    boot_model = _norm_model(getattr(args, "model", None) or _env("MODEL"))
    results = spawn_agents(names, cwd, method=method, prompt=args.prompt,
                           dry_run=args.dry_run, worktree=args.worktree,
                           prompts=(prompt_map or None),
                           depth=depth + 1, spawned_by=spawner, squad=boot_squad,
                           model=boot_model)
    only_printing = args.dry_run or method == "print"
    verb = "would spawn" if only_printing else "spawned"
    ok = sum(1 for r in results if r["ok"])
    print(f"{verb} {ok}/{len(results)} teammate(s) via {method} in {cwd}:")
    for r in results:
        print(f"  {'✓' if r['ok'] else '✗'} {r['name']}")
        if r["error"]:
            print(f"      error: {r['error']}")

    # Declare the team size the moment it's known — bootstrap *is* team formation —
    # so the barrier is precise from t=0 and everyone (and `who`) knows the target.
    # Skipped only for --dry-run (a pure preview). The size time-fallback means an
    # optimistic count can never wedge the team: a no-show just delays to the grace.
    if not only_printing and ok > 0:
        # Only a real launch declares a size. A preview (--dry-run / --method print)
        # spawns nothing, so committing a barrier of N nonexistent agents would force
        # ~90s waits on whoever's here — the human declares via `expect N` once the
        # windows are actually open.
        size = (n_squad_before if boot_squad else n_active_before) + ok
        conn = connect()
        set_team_size(conn, size, boot_squad)
        if getattr(args, "goal", None):
            set_goal(conn, args.goal)  # bootstrap IS team formation — record the goal
        conn.close()
        print(f"\nDeclared {('squad ' + boot_squad) if boot_squad else 'team'} size: {size} "
              f"(its barrier waits for the {'squad' if boot_squad else 'team'}; "
              f"`expect{(' --squad ' + boot_squad) if boot_squad else ''} N` to adjust).")
        if getattr(args, "goal", None):
            print(f"Shared goal: {args.goal} "
                  "(every agent sees it in their briefing and `who`).")
        # $AGORA_TEAM_SIZE only overrides the DEFAULT-room size (expected_team_size reads
        # the env for squad=None) — a per-squad size lives in its own meta key, so don't
        # print the misleading override note for a --squad bootstrap.
        env_sz = (_env("TEAM_SIZE") or "").strip()
        if env_sz and not boot_squad:
            print(f"  note: $AGORA_TEAM_SIZE={env_sz} in your environment "
                  f"overrides this — the barrier will use {env_sz}.")

    if only_printing:
        print("\nRun each in its own terminal:")
        for r in results:
            print(f"  {r['command']}")
    elif method == "tmux" and ok:
        print("\nAttach with:  tmux attach -t groupchat")
    elif method == "terminal" and ok:
        print("\nEach opened in a new Terminal window — they'll join the chat. "
              "`who` to confirm; tell each what to do, or `/rename` to relabel.")
    if args.worktree and ok and not args.dry_run:
        print("Each runs in its own git worktree (branch groupchat/<name>) so file "
              "edits can't collide; one shared chat.db keeps them in the same room. "
              "Clean up later with `git worktree remove`.")

    # For a real launch, give a quick joined-vs-not-yet readout — this catches a
    # phantom ✓ (osascript returned 0 but `claude` wasn't on the child's PATH) and
    # tells the human how many instances actually came up. Best-effort; never blocks.
    if method in ("terminal", "tmux") and not args.dry_run and ok > 0:
        conn = connect()
        joined = poll_joined(conn, [r["name"] for r in results if r["ok"]])
        conn.close()
        n_join = sum(1 for v in joined.values() if v)
        print(f"\n{n_join}/{ok} joined the chat so far.")
        not_yet = [n for n, j in joined.items() if not j]
        if not_yet:
            print(f"  not yet: {', '.join(not_yet)} "
                  "(usually appear within seconds — `who` to recheck; if one never "
                  "joins, check that `claude` is on its PATH).")
    return 0 if all(r["ok"] for r in results) else 1


# Hook wiring appended to a target repo's .claude/settings.json by `install`.
HOOK_ENTRIES = {
    "SessionStart": 'python3 "$CLAUDE_PROJECT_DIR/.groupchat/hooks/session_start.py"',
    "UserPromptSubmit": 'python3 "$CLAUDE_PROJECT_DIR/.groupchat/hooks/user_prompt_submit.py"',
    "Stop": 'python3 "$CLAUDE_PROJECT_DIR/.groupchat/hooks/stop.py"',
}

# Extra per-hook options merged alongside the command. The Stop hook parks a
# finished agent at the team barrier, so it needs a long timeout (it returns on
# its own before this) and a status line while it blocks.
HOOK_OPTIONS = {
    "UserPromptSubmit": {"timeout": 15},
    "Stop": {"timeout": 600,
             "statusMessage": "⏳ parked at the agora team barrier — not stopped: "
                              "wakes on a teammate's @mention, exits when the whole "
                              "team is done"},
}


def _merge_settings(settings: dict) -> tuple[dict, int]:
    """Idempotently add our hook commands to a settings dict. Returns (dict, added)."""
    import copy
    settings = copy.deepcopy(settings) if settings else {}
    hooks = settings.setdefault("hooks", {})
    added = 0
    for event, command in HOOK_ENTRIES.items():
        groups = hooks.setdefault(event, [])
        already = any(
            h.get("command") == command
            for g in groups for h in g.get("hooks", [])
        )
        if not already:
            entry = {"type": "command", "command": command}
            entry.update(HOOK_OPTIONS.get(event, {}))
            groups.append({"hooks": [entry]})
            added += 1
    return settings, added


def cmd_install(args):
    import shutil
    src = os.path.dirname(os.path.abspath(__file__))          # this .groupchat dir
    target_root = os.path.abspath(args.target)
    dst = os.path.join(target_root, ".groupchat")

    if os.path.abspath(dst) != src:
        os.makedirs(dst, exist_ok=True)
        shutil.copy2(os.path.join(src, "chat.py"), os.path.join(dst, "chat.py"))
        gi = os.path.join(src, ".gitignore")
        if os.path.exists(gi):
            shutil.copy2(gi, os.path.join(dst, ".gitignore"))
        hooks_dst = os.path.join(dst, "hooks")
        os.makedirs(hooks_dst, exist_ok=True)
        for f in os.listdir(os.path.join(src, "hooks")):
            if f.endswith(".py"):
                shutil.copy2(os.path.join(src, "hooks", f), os.path.join(hooks_dst, f))
        print(f"copied agora files -> {dst}")
    else:
        print(f"using existing files at {dst}")

    settings_path = os.path.join(target_root, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    existing = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path) as fh:
                existing = json.load(fh)
        except Exception:
            print(f"warning: {settings_path} is not valid JSON; refusing to overwrite",
                  file=sys.stderr)
            return 1
    merged, added = _merge_settings(existing)
    with open(settings_path, "w") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    print(f"{'added' if added else 'no new'} hook(s) in {settings_path}"
          + (f" (+{added})" if added else ""))
    print("Done. Open Claude Code in this repo (restart the session) and agora "
          "is live for every instance.")
    return 0


# --------------------------------------------------------------------------- #
# Constitution (governance layer) — Phase 1: the document
# --------------------------------------------------------------------------- #
CONST_FILENAME = "CONSTITUTION.md"
_CORE_BEGIN = "<!-- CONSTITUTION:CORE:BEGIN -->"
_CORE_END = "<!-- CONSTITUTION:CORE:END -->"
_ART_BEGIN = "<!-- CONSTITUTION:ARTICLES:BEGIN -->"
_ART_END = "<!-- CONSTITUTION:ARTICLES:END -->"
_CONST_ZONES = {"core": (_CORE_BEGIN, _CORE_END), "articles": (_ART_BEGIN, _ART_END)}
_CORE_ID_RE = re.compile(r"^###\s+(C\d+)\b[ \t]*[—:-]?[ \t]*(.*)$", re.MULTILINE)
_ART_ID_RE = re.compile(r"^###\s+(R\d+)\b[ \t]*[—:-]?[ \t]*(.*)$", re.MULTILINE)
_PROV_RE = re.compile(r"<!--\s*meta:\s*(.*?)\s*-->")


def constitution_path() -> str:
    return os.path.join(repo_root(), CONST_FILENAME)


def _starter_constitution(today: str) -> str:
    return (
        "# Repo Constitution\n\n"
        f"{_CORE_BEGIN}\n"
        "## Core (entrenched — amendable only by a human, never by the parliament)\n\n"
        "### C1 — The human is the final authority\n"
        "No automated process may modify this Core section or apply an amendment to\n"
        "the Articles without a human committing it.\n\n"
        "### C2 — Hooks fail open\n"
        "A coordination hook must never crash or block a session on error.\n\n"
        "### C3 — Writes are single-threaded\n"
        "Agents add intelligence, not concurrent edits. One writer per change.\n\n"
        "### C4 — The amendment procedure\n"
        "Articles change only by: a motion citing evidence -> an advisory vote -> a\n"
        "human ratifying the proposed diff after reading the cited evidence. Core\n"
        "changes are out of scope for this procedure.\n"
        f"{_CORE_END}\n\n"
        f"{_ART_BEGIN}\n"
        "## Articles (amendable by the parliament, ratified by a human)\n\n"
        "### R1 — Announce before you touch a file\n"
        "Post \"starting on <path>\" before editing, so two agents don't collide.\n"
        f"<!-- meta: id=R1 added={today} by=human ratified={today} amended= source= -->\n\n"
        "### R2 — Converge, don't fork\n"
        "If two agents propose overlapping designs, one retracts. Do not merge into\n"
        "an average; pick one and make it the contract.\n"
        f"<!-- meta: id=R2 added={today} by=human ratified={today} amended= source= -->\n"
        f"{_ART_END}\n"
    )


def _zone_span(text: str, which: str):
    """Return ``(content_start, content_end)`` offsets for a zone's content (between
    the marker LINES, exclusive). Markers must be their own (stripped) line, so a
    marker string quoted inside body text can't be mistaken for the boundary."""
    begin, end = _CONST_ZONES[which]
    starts, pos = [], 0
    lines = text.splitlines(keepends=True)
    for ln in lines:
        starts.append(pos)
        pos += len(ln)
    bi = ei = None
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if s == begin and bi is None:
            bi = idx
        elif s == end and bi is not None and ei is None:
            ei = idx
    if bi is None or ei is None or ei <= bi:
        return None
    return (starts[bi] + len(lines[bi]), starts[ei])


def _const_zone(text: str, which: str):
    span = _zone_span(text, which)
    return text[span[0]:span[1]] if span else None


def _parse_prov(segment: str) -> dict:
    """Parse the LAST ``<!-- meta: k=v … -->`` in a block into a dict. Provenance
    lives at the block's foot, so taking the last comment stops a body example from
    poisoning it. Whitespace-separated ``key=value`` tokens with possibly-EMPTY
    values (e.g. ``amended=``) — split on the first ``=`` per token so an empty
    value can't swallow the next key."""
    pms = list(_PROV_RE.finditer(segment))
    prov = {}
    if pms:
        for tok in pms[-1].group(1).split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                prov[k] = v
    return prov


def parse_constitution(text: str) -> dict:
    """Split the two zones and parse Core (``C<n>``) + Articles (``R<n>``) with
    provenance. Returns ``{ok, errors, core:[{id,title}], articles:[{id,title,prov}]}``.
    Loud (ok=False) on missing/malformed markers or a reused rule id — the CLI
    surfaces that; hooks call this best-effort and stay silent on any problem."""
    res = {"ok": True, "errors": [], "core": [], "articles": []}
    core = _const_zone(text, "core")
    arts = _const_zone(text, "articles")
    if core is None:
        res["errors"].append("CORE zone markers missing or malformed")
    if arts is None:
        res["errors"].append("ARTICLES zone markers missing or malformed")
    if res["errors"]:
        res["ok"] = False
        return res
    for m in _CORE_ID_RE.finditer(core):
        res["core"].append({"id": m.group(1), "title": m.group(2).strip()})
    counts = {}
    for m in _ART_ID_RE.finditer(arts):
        rid = m.group(1)
        rest = arts[m.end():]
        nxt = re.search(r"^###\s", rest, re.MULTILINE)
        seg = rest[:nxt.start()] if nxt else rest
        prov = _parse_prov(seg)
        res["articles"].append({"id": rid, "title": m.group(2).strip(), "prov": prov,
                                "repealed": bool(prov.get("repealed"))})
        counts[rid] = counts.get(rid, 0) + 1
    dups = sorted(r for r, c in counts.items() if c > 1)
    if dups:
        res["ok"] = False
        res["errors"].append("duplicate rule id(s): " + ", ".join(dups))
    for a in res["articles"]:
        pid = a["prov"].get("id")
        if pid and pid != a["id"]:
            res["ok"] = False
            res["errors"].append(
                f"{a['id']}: provenance id={pid} mismatches heading")
    res["live"] = [a for a in res["articles"] if not a["repealed"]]
    return res


def cmd_constitution(args):
    action = getattr(args, "action", None) or "show"
    path = constitution_path()
    if action == "init":
        if os.path.exists(path):
            print(f"refusing to overwrite existing {path}", file=sys.stderr)
            return 1
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(_starter_constitution(now_iso()[:10]))
        print(f"wrote starter constitution -> {path}")
        print("Next: move the coordination conventions out of CLAUDE.md into the "
              "Articles so the law has a single source of truth, then run "
              "`constitution check`.")
        return 0
    if not os.path.exists(path):
        print(f"no constitution yet at {path} — a human runs `constitution init`.")
        return 0
    res = parse_constitution(open(path).read())
    if action == "check":
        if res["ok"]:
            print(f"constitution OK: {len(res['core'])} core item(s), "
                  f"{len(res['articles'])} article(s)")
            return 0
        for e in res["errors"]:
            print(f"constitution ERROR: {e}", file=sys.stderr)
        return 1
    # show (default)
    for e in res["errors"]:
        print(f"! {e}")
    print("CORE (entrenched — human-only):")
    for c in res["core"]:
        print(f"  {c['id']} — {c['title']}")
    print("ARTICLES (parliament-amendable, human-ratified):")
    for a in res["articles"]:
        meta = " ".join(f"{k}={v}" for k, v in a["prov"].items() if v)
        print(f"  {a['id']} — {a['title']}" + (f"   [{meta}]" if meta else ""))
    return 0


def _env_float(name: str, default: float) -> float:
    try:
        v = _env(name)  # AGORA_* with GROUPCHAT_* legacy fallback (like _env_int)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _rule_cite_sender_sets(conn, days: int = 0) -> dict:
    """``{rule_id: set(senders)}`` over the window (days=0 → all-time)."""
    if days and days > 0:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT rule_id, sender FROM rule_cites WHERE ts >= ?", (cutoff,)).fetchall()
    else:
        rows = conn.execute("SELECT rule_id, sender FROM rule_cites").fetchall()
    by = {}
    for r in rows:
        by.setdefault(r["rule_id"], set()).add(r["sender"])
    return by


def cmd_review(args):
    """Repeal-first, ADVISORY review: rank live Articles by distinct-sender cites,
    flag dead/rarely-cited rules for repeal, and surface cites for unknown/repealed
    ids. Changes nothing. (Drift-grep + diary-promotion are deferred to P2.5.)"""
    path = constitution_path()
    if not os.path.exists(path):
        print(f"no constitution yet at {path} — nothing to review.")
        return 0
    res = parse_constitution(open(path).read())
    if not res["ok"]:
        for e in res["errors"]:
            print(f"constitution ERROR: {e}", file=sys.stderr)
        return 1
    conn = connect()
    days = int(getattr(args, "days", 0) or 0)
    sets = _rule_cite_sender_sets(conn, days)
    live = {a["id"]: a for a in res["live"]}
    low = _env_int("GROUPCHAT_REVIEW_LOW", 1)
    window = f"last {days}d" if days else "all-time"
    print(f"Constitution review (window: {window}) — {len(live)} article(s), advisory")

    repeal, watch, kept = [], [], []
    for rid, a in live.items():
        senders = set(sets.get(rid, set()))
        senders.discard(a["prov"].get("by"))   # discount self-cites by the author
        n = len(senders)
        row = (rid, a["title"], n)
        (repeal if n == 0 else watch if n <= low else kept).append(row)

    print("\nRepeal candidates (never cited — dead letters):")
    for rid, t, n in repeal:
        print(f"  {rid} — {t}   ({n} cites)")
    if not repeal:
        print("  (none)")
    if watch:
        print(f"\nRarely cited (<= {low} distinct agent — watch):")
        for rid, t, n in watch:
            print(f"  {rid} — {t}   ({n} cites)")
    print("\nActive (kept):")
    for rid, t, n in kept:
        print(f"  {rid} — {t}   ({n} cites)")
    if not kept:
        print("  (none)")

    unknown = sorted(set(sets) - set(live))
    if unknown:
        print("\nUnknown / repealed cite ids (RULE_RE noise or dead rules):")
        for rid in unknown:
            print(f"  {rid}   ({len(sets[rid])} cites)")

    print("\nDrift-flag + diary-promotion checks: deferred to P2.5.")
    return 0


# --------------------------------------------------------------------------- #
# Constitution — Phase 3: the advisory parliament (motion / vote / amendments / ratify)
# --------------------------------------------------------------------------- #
def _parse_motion_id(token: str):
    t = (token or "").strip().lstrip("Mm")
    return int(t) if t.isdigit() else None


def _next_rule_id(conn, parsed) -> str:
    """Allocate a monotonic, never-reused R-id (high-water mark in meta)."""
    existing = [int(a["id"][1:]) for a in parsed["articles"] if a["id"][1:].isdigit()]
    floor = (max(existing) + 1) if existing else 1
    hw = int(get_meta(conn, "const_next_rule_id") or 0)
    nxt = max(floor, hw)
    set_meta(conn, "const_next_rule_id", str(nxt + 1))
    return f"R{nxt}"


def _article_block(text: str, rule_id: str):
    """Return ``(start, end, block_text)`` for the ``### R<id>`` Article block, or None.
    The block spans its heading through just before the next ``### `` or the zone end."""
    ab = text.find(_ART_BEGIN)
    ae = text.find(_ART_END)
    if ab == -1 or ae == -1 or ae < ab:
        return None
    zone_start = ab + len(_ART_BEGIN)
    m = re.search(rf"^###\s+{re.escape(rule_id)}\b.*$", text[zone_start:ae], re.MULTILINE)
    if not m:
        return None
    hstart = zone_start + m.start()
    nxt = re.search(r"^###\s", text[hstart + 1:ae], re.MULTILINE)
    hend = (hstart + 1 + nxt.start()) if nxt else ae
    return (hstart, hend, text[hstart:hend])


def _format_provenance(prov: dict) -> str:
    base = ["id", "added", "by", "ratified", "amended", "source"]
    parts = [f"{k}={prov.get(k, '')}" for k in base]
    if prov.get("repealed"):
        parts.append(f"repealed={prov['repealed']}")
    parts += [f"{k}={prov[k]}" for k in prov if k not in base and k != "repealed"]
    return "<!-- meta: " + " ".join(parts) + " -->"


def _apply_amendment(text: str, m, today: str) -> str:
    """Return the new file text for a ratified motion. Pure (no write)."""
    if m["op"] == "repeal":
        blk = _article_block(text, m["target"])
        if not blk:
            return text
        s, e, block = blk
        # Tombstone, don't delete: the id stays in the committed file so it is never
        # reused (durable no-reuse, independent of the runtime db) and prior cites
        # resolve to a known-repealed rule.
        prov = _parse_prov(block)
        prov.update(id=m["target"], by="parliament", repealed=today, source=f"M{m['id']}")
        tomb = f"### {m['target']} — (repealed {today})\n{_format_provenance(prov)}\n"
        return text[:s] + tomb + text[e:]
    if m["op"] == "amend":
        blk = _article_block(text, m["target"])
        if not blk:
            return text
        s, e, block = blk
        hm = re.match(r"(###[^\n]*)\n", block)
        heading = hm.group(1) if hm else f"### {m['target']}"
        prov = _parse_prov(block)
        prov.update(id=m["target"], by="parliament", amended=today,
                    ratified=today, source=f"M{m['id']}")
        new_block = f"{heading}\n{(m['change'] or '').strip()}\n{_format_provenance(prov)}\n"
        return text[:s] + new_block + text[e:]
    if m["op"] == "add":
        span = _zone_span(text, "articles")
        if not span:
            return text
        prov = {"id": m["new_id"], "added": today, "by": m["proposer"],
                "ratified": today, "amended": "", "source": f"M{m['id']}"}
        heading = (m["title"] or "").strip() or "(new rule)"
        block = (f"### {m['new_id']} — {heading}\n{(m['change'] or '').strip()}\n"
                 f"{_format_provenance(prov)}\n\n")
        i = span[1]  # offset of the ARTICLES:END marker line
        return text[:i] + block + text[i:]
    return text


def _unified_diff(old: str, new: str, path: str) -> str:
    import difflib
    rel = os.path.basename(path)
    return "".join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}"))


def motion_tally(conn, motion_id: int) -> dict:
    """Advisory tally: distinct registered voters, last vote per session wins.

    Also reports model DIVERSITY among the casting voters (the heterogeneous-model
    quorum): ``models`` = distinct known models, and ``single_model`` = True when 2+
    voters all share ONE known model (a homogeneous-fleet sweep — low epistemic
    independence, the capture signal). This is purely advisory annotation; it never
    gates or binds anything. Unknown (NULL) models don't count toward diversity, and the
    reading reflects each voter's CURRENT model (a post-vote `model` change re-annotates)
    — the conservative direction is silence, never a false bind."""
    rows = conn.execute(
        "SELECT voter_session, voter_handle, vote FROM votes WHERE motion_id=? ORDER BY id",
        (motion_id,)).fetchall()
    last = {}
    for r in rows:
        last[r["voter_session"]] = (r["voter_handle"], r["vote"])
    yea = sum(1 for _h, v in last.values() if v == "yea")
    nay = sum(1 for _h, v in last.values() if v == "nay")
    # Map each casting session to its current model (NULL/unknown excluded from diversity).
    models_by_session = {row["session_id"]: row["model"]
                         for row in conn.execute("SELECT session_id, model FROM agents")}
    known = [models_by_session.get(s) for s in last if models_by_session.get(s)]
    distinct = set(known)
    single_model = len(last) >= 2 and len(distinct) == 1 and len(known) == len(last)
    return {"yea": yea, "nay": nay, "voters": len(last),
            "models": len(distinct), "single_model": single_model,
            "detail": [(s, h, v) for s, (h, v) in last.items()]}


def _diversity_note(t: dict) -> str:
    """Advisory model-diversity annotation for a tally — surfaces the capture signal so a
    human can weigh it. Empty (dormant) until 2+ votes are cast."""
    if (t["yea"] + t["nay"]) < 2:
        return ""
    if t.get("single_model"):
        return " · ⚠ single-model vote — low epistemic independence (homogeneous fleet)"
    if t.get("models", 0) >= 2:
        return f" · {t['models']} models (cross-model support)"
    return ""


# --------------------------------------------------------------------------- #
# Parliamentary framing — sessions, agendas, decisions (binds NOTHING)
# --------------------------------------------------------------------------- #
# Connective tissue for the advisory parliament: a SESSION frames a bounded
# deliberation, an AGENDA is its open items (reusing the motions table; op='decide'
# for non-constitutional questions), and a DECISION is a kind='decision' RECORD of the
# room's outcome. The load-bearing rule, enforced in code (cmd_ratify): a decision item
# can NEVER reach the law — only a constitutional motion ratified by a human does. All
# additive, dormant-until-used, riding the one cursor via kind='session'/'decision'.
def parl_session(conn) -> dict | None:
    """The open deliberation session, or None. Auto-expires after the active window so a
    crashed opener can't leave a session open forever (mirrors ``standdown_active``). On
    detecting a stale pointer it REAPS — expires that session's leftover open items and
    clears the meta — so an abandoned session's decision items don't orphan into the
    unscoped agenda (lazy GC, like ``_clear_stale_team_size`` on register)."""
    oid = get_meta(conn, "parl_session")
    if not oid:
        return None
    if iso_age_seconds(get_meta(conn, "parl_session_at")) >= ACTIVE_WINDOW_SECONDS:
        conn.execute("UPDATE motions SET status='expired' "
                     "WHERE session_id=? AND status='open'", (oid,))
        del_meta(conn, "parl_session")
        del_meta(conn, "parl_session_at")
        del_meta(conn, "parl_session_title")
        conn.commit()
        return None
    return {"id": oid, "title": get_meta(conn, "parl_session_title") or "",
            "opened_at": get_meta(conn, "parl_session_at")}


def open_parl_session(conn, sender: str, title: str) -> int:
    """Open a session: post a ``kind='session'`` bookend (rides the cursor so every agent
    and every late joiner learns it) and stamp the meta pointer. Returns the session id."""
    title = " ".join((title or "").split())
    mid = send(conn, sender, f"[session opened] {title}", kind="session")
    set_meta(conn, "parl_session", str(mid))
    set_meta(conn, "parl_session_at", now_iso())
    set_meta(conn, "parl_session_title", title)
    return mid


def close_parl_session(conn, sender: str, summary: str | None = None) -> None:
    """Close the open session: post a closing bookend, expire its leftover open agenda
    items, and clear the meta pointer."""
    s = parl_session(conn)
    title = s["title"] if s else ""
    sid = get_meta(conn, "parl_session")
    send(conn, sender, f"[session closed] {title}" + (f" — {summary}" if summary else ""),
         kind="session")
    if sid:
        conn.execute("UPDATE motions SET status='expired' "
                     "WHERE session_id=? AND status='open'", (sid,))
        conn.commit()
    del_meta(conn, "parl_session")
    del_meta(conn, "parl_session_at")
    del_meta(conn, "parl_session_title")


def add_decision_item(conn, sender: str, question: str, because: str,
                      session_id: str | None = None) -> int:
    """Add a non-constitutional agenda item (``op='decide'``) — a question for the room
    to decide. Rides the motions table (so it's votable, supersede-able, and tallied) but
    has NO CONSTITUTION.md target, so ``ratify`` refuses it: it can never become law."""
    question = " ".join((question or "").split())
    if not question:
        raise ValueError("a decision item needs a question")
    mid = send(conn, sender,
               f"[decision item] {question}  (because: {because})", kind="motion")
    conn.execute(
        "INSERT INTO motions(id, ts, proposer, target, op, change, because, "
        "base_text, new_id, title, status, session_id) "
        "VALUES (?,?,?,?, 'decide', ?,?,?,?,?, 'open', ?)",
        (mid, now_iso(), sender, question, None, because, None, None, None, session_id))
    conn.commit()
    return mid


def agenda_items(conn, session_id: str | None = None) -> list[sqlite3.Row]:
    """The open agenda — open motions (constitutional + decision items), optionally scoped
    to a session."""
    if session_id:
        return conn.execute(
            "SELECT * FROM motions WHERE status='open' AND session_id=? ORDER BY id",
            (session_id,)).fetchall()
    return conn.execute("SELECT * FROM motions WHERE status='open' ORDER BY id").fetchall()


def record_decision(conn, sender: str, motion_id: int, outcome: str) -> tuple[int, str]:
    """Record the room's outcome on a DECISION item as a ``kind='decision'`` RECORD —
    advisory, queryable, inherited by the next cohort. Binds NOTHING and marks the item
    'decided'. Returns ``(msg_id, status)``; status is 'recorded', 'missing', or
    'not-a-decision' (a constitutional motion must be resolved by ``ratify``, not here —
    keeping the law lane and the decision lane cleanly separate)."""
    m = conn.execute("SELECT * FROM motions WHERE id=?", (motion_id,)).fetchone()
    if not m:
        return (0, "missing")
    if m["op"] != "decide":
        return (0, "not-a-decision")
    if m["status"] != "open":
        return (0, "already-resolved")  # already decided/expired/superseded — no dup record
    t = motion_tally(conn, motion_id)
    body = (f"[decision] M{motion_id} ({m['target']}): {' '.join((outcome or '').split())} "
            f"(advisory tally — yea {t['yea']} / nay {t['nay']}, {t['voters']} voters; "
            f"the room concluded this, it binds nothing)")
    did = send(conn, sender, body, kind="decision")
    conn.execute("UPDATE motions SET status='decided' WHERE id=?", (motion_id,))
    conn.commit()
    return (did, "recorded")


def list_decisions(conn, limit: int | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM messages WHERE kind='decision' ORDER BY id ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q).fetchall()


def _motion_summary(op, target, new_id, change, because, proposer, title=None) -> str:
    head = {"amend": f"Motion: amend {target}",
            "repeal": f"Motion: repeal {target}",
            "add": f"Motion: add {new_id}"}[op]
    out = f"{head} — proposed by {proposer}. because: {because}"
    if title and op == "add":
        out += f"  | title: {title}"
    if change and op != "repeal":
        out += f"  | new text: {change}"
    return out


def cmd_motion(args):
    conn = connect()
    a = _resolve_for_cli(conn, args)
    proposer = a["handle"] if a else (args.from_handle or "anon")
    because = (args.because or "").strip()
    if not because:
        print("a motion needs evidence: pass --because '<message ids / tests / diary>'",
              file=sys.stderr)
        return 1
    path = constitution_path()
    if not os.path.exists(path):
        print(f"no constitution at {path} — run `constitution init` first", file=sys.stderr)
        return 1
    text = open(path).read()
    parsed = parse_constitution(text)
    if not parsed["ok"]:
        print("constitution is malformed; fix it before legislating", file=sys.stderr)
        return 1
    if args.repeal:
        target, op, change = args.repeal, "repeal", None
    elif args.rule == "new":
        target, op, change = "new", "add", args.change
    else:
        target, op, change = args.rule, "amend", args.change
    if target and re.fullmatch(r"C\d+", target):
        print(f"{target} is entrenched Core — not amendable by motion (C1/C4)",
              file=sys.stderr)
        return 1
    if change is not None:
        for ln in change.splitlines():
            if (re.match(r"^\s*###\s", ln) or "CONSTITUTION:CORE:" in ln
                    or "CONSTITUTION:ARTICLES:" in ln or "<!-- meta:" in ln):
                print("--change may not contain a '### ' heading, a zone marker, or a "
                      "'<!-- meta:' comment (it would corrupt the law)", file=sys.stderr)
                return 1
    # The title becomes a single-line heading (`### <id> — <title>`), so it must be
    # one safe line — reject newlines and any markup that could break the heading or
    # forge a second article / zone / provenance comment.
    title = getattr(args, "title", None)
    if title is not None:
        # Reject every char str.splitlines() treats as a line boundary (not just
        # \n/\r) — VT/FF/FS/GS/RS/NEL/U+2028/U+2029 would render the heading as two
        # lines in the diff a human reviews even though the file stays one physical line.
        if any(c in title for c in "\n\r\v\f\x1c\x1d\x1e\x85  "):
            print("--title must be a single line", file=sys.stderr)
            return 1
        if ("###" in title or "CONSTITUTION:" in title
                or "<!--" in title or "-->" in title):
            print("--title may not contain '###', a zone marker, or HTML-comment "
                  "markers (it would corrupt the heading/law)", file=sys.stderr)
            return 1
    live = {x["id"] for x in parsed["live"]}
    base_text, new_id = None, None
    if op in ("amend", "repeal"):
        if target not in live:
            print(f"{target} is not a live Article", file=sys.stderr)
            return 1
        if op == "amend" and not (change or "").strip():
            print("amend needs --change '<new rule text>'", file=sys.stderr)
            return 1
        blk = _article_block(text, target)
        base_text = blk[2] if blk else None
    else:  # add
        if not (change or "").strip():
            print("add needs --change '<rule text>'", file=sys.stderr)
            return 1
        new_id = _next_rule_id(conn, parsed)
    tgt_key = new_id if op == "add" else target
    summary = _motion_summary(op, target, new_id, change, because, proposer,
                              getattr(args, "title", None))
    mid = send(conn, proposer, summary,
               session_id=(a["session_id"] if a else None), kind="motion")
    # Supersede only OTHER CONSTITUTIONAL motions on the same rule — never a decision
    # item that happens to share the target string (a degenerate question like "R2").
    # The law and decision lanes share the motions table but must not collide here.
    conn.execute("UPDATE motions SET status='superseded' "
                 "WHERE target=? AND status='open' AND op!='decide'", (tgt_key,))
    conn.execute(
        "INSERT INTO motions(id, ts, proposer, target, op, change, because, "
        "base_text, new_id, title, status) VALUES (?,?,?,?,?,?,?,?,?,?, 'open')",
        (mid, now_iso(), proposer, tgt_key, op, change, because, base_text, new_id,
         (getattr(args, "title", None) or None)))
    conn.commit()
    print(f"motion M{mid} opened: {op} {tgt_key} (advisory). Teammates vote with "
          f"`vote --session <sid> M{mid} yea|nay`; a human ratifies.")
    return 0


def cmd_vote(args):
    conn = connect()
    sid = getattr(args, "session", None)
    a = agent_by_session(conn, sid) if sid else None
    if not a:
        # A bare --from is unauthenticated (anyone can spoof a handle), so votes
        # require a registered session. Agents only know their handle, not their
        # session id — so point them at the ready-to-run line in their group-chat
        # briefing (which embeds the real session id and works on ANY host), and
        # give the Claude Code shortcut as a convenience. Host-neutral: the
        # briefing path doesn't depend on a Claude-only env var.
        print(
            "vote requires a registered session (a bare --from handle is "
            "unauthenticated and is not counted).\n"
            "Use the ready-to-run vote line from your group-chat briefing (it "
            "embeds your session id and works on any host), or pass --session "
            "<your-session-id>.\n"
            "In Claude Code your session id is in $CLAUDE_CODE_SESSION_ID:\n"
            f'    python3 "{os.path.abspath(__file__)}" vote '
            f'--session "$CLAUDE_CODE_SESSION_ID" {args.motion} {args.vote}',
            file=sys.stderr)
        return 1
    mid = _parse_motion_id(args.motion)
    if mid is None:
        print(f"bad motion id {args.motion!r} (expected M<number>)", file=sys.stderr)
        return 1
    m = conn.execute("SELECT * FROM motions WHERE id=?", (mid,)).fetchone()
    if not m:
        print(f"no motion M{mid}", file=sys.stderr)
        return 1
    if m["status"] != "open":
        print(f"M{mid} is {m['status']}, not open — vote not counted", file=sys.stderr)
        return 1
    send(conn, a["handle"], f"M{mid} {args.vote}",
         session_id=a["session_id"], kind="vote")
    conn.execute("INSERT INTO votes(ts, motion_id, voter_session, voter_handle, vote) "
                 "VALUES (?,?,?,?,?)",
                 (now_iso(), mid, a["session_id"], a["handle"], args.vote))
    conn.commit()
    print(f"recorded {args.vote} on M{mid} as {a['handle']} (advisory)")
    return 0


def cmd_amendments(args):
    conn = connect()
    show_all = getattr(args, "all", False)
    rows = conn.execute("SELECT * FROM motions ORDER BY id DESC").fetchall()
    # `amendments` is the CONSTITUTIONAL view — decision items (op='decide') live in
    # `agenda` / `decisions`, never here (they can't be ratified).
    rows = [m for m in rows if m["op"] != "decide" and (show_all or m["status"] == "open")]
    if not rows:
        print("no motions yet." if show_all else "no open motions.")
        return 0
    superq = _env_float("GROUPCHAT_AMEND_SUPERMAJORITY", 0.66)
    quorum = _env_int("GROUPCHAT_AMEND_QUORUM", 3)
    print("Motions — ADVISORY tally; the vote never gates, a human ratifies from "
          "evidence (see `ratify`):")
    for m in rows:
        t = motion_tally(conn, m["id"])
        cast = t["yea"] + t["nay"]
        frac = (t["yea"] / cast) if cast else 0.0
        flag = ("worth a human's ratify look"
                if (t["voters"] >= quorum and frac >= superq) else "below the advisory bar")
        print(f"  M{m['id']} [{m['status']}] {m['op']} {m['target']} — by {m['proposer']}")
        if m["title"]:
            print(f"      title: {m['title']}")
        print(f"      yea {t['yea']} / nay {t['nay']}  ({t['voters']} registered voters)"
              f"{_diversity_note(t)} — {flag}")
        print(f"      because: {(m['because'] or '')[:100]}")
    return 0


def cmd_ratify(args):
    """[human] Default: a READ-ONLY, repeatable evidence dossier + proposed diff
    (no status change, no announcement). With ``--confirm`` (run BEFORE you apply the
    diff — confirm-then-apply): the applicability guards require the target to be
    valid, non-colliding, and not-yet-applied, then it marks the motion ratified,
    prints the diff once more, and notifies the team; you then apply + ``git commit``
    it. Never writes the file itself — diff-only, C1. The vote is advisory."""
    conn = connect()
    mid = _parse_motion_id(args.motion)
    if mid is None:
        print(f"bad motion id {args.motion!r}", file=sys.stderr)
        return 1
    m = conn.execute("SELECT * FROM motions WHERE id=?", (mid,)).fetchone()
    if not m:
        print(f"no motion M{mid}", file=sys.stderr)
        return 1
    if m["status"] in ("ratified", "superseded", "withdrawn"):
        print(f"M{mid} is {m['status']} — nothing to ratify", file=sys.stderr)
        return 1
    if m["op"] == "decide":
        # The mechanical law/decision separation: a decision item never touches the
        # constitution. It can only be RECORDED (advisory) via `decision`, never ratified.
        print(f"M{mid} is a decision item, not a constitutional amendment — it binds "
              f"nothing and cannot be ratified. Record its outcome with "
              f'`decision M{mid} "..."`.', file=sys.stderr)
        return 1
    if re.fullmatch(r"C\d+", m["target"] or ""):
        print(f"{m['target']} is entrenched Core — cannot be ratified", file=sys.stderr)
        return 1
    path = constitution_path()
    if not os.path.exists(path):
        print(f"no constitution at {path}", file=sys.stderr)
        return 1
    text = open(path).read()
    parsed = parse_constitution(text)
    if not parsed["ok"]:
        print("constitution is malformed; fix it before ratifying", file=sys.stderr)
        return 1
    # Applicability guards run for BOTH the diff preview and --confirm: the target id
    # must be valid and non-colliding (a taken id / a changed base text is caught
    # here), and the rule must NOT already be in the file — so --confirm is run BEFORE
    # the human applies the diff (confirm-then-apply; see the closing guidance).
    if m["op"] in ("amend", "repeal"):
        blk = _article_block(text, m["target"])
        if not blk:
            print(f"{m['target']} no longer exists — re-motion", file=sys.stderr)
            return 1
        if (m["base_text"] or "").strip() != blk[2].strip():
            print(f"{m['target']} changed since M{mid} opened (base-text mismatch). "
                  "Re-motion against the current text.", file=sys.stderr)
            return 1
    if m["op"] == "add" and m["new_id"] in {a["id"] for a in parsed["articles"]}:
        print(f"{m['new_id']} already exists — re-motion (id now taken)", file=sys.stderr)
        return 1
    new_text = _apply_amendment(text, m, now_iso()[:10])
    if new_text == text:
        print(f"M{mid} would make no change to the law — refusing (re-motion).",
              file=sys.stderr)
        return 1

    if getattr(args, "confirm", False):
        # Ratification is the HUMAN's act (C1). Gate the status-changing --confirm like the
        # control plane: the operator (a bare invocation) or the lead may enact; a known
        # worker agent (or a forged --from) is rejected. The dossier above is read-only and
        # ungated — anyone can inspect the evidence + diff.
        ok, caller, lead = _control_caller_ok(conn, args)
        if not ok:
            print(f"only the operator (a bare invocation) or the lead (@{lead}) may "
                  f"`ratify --confirm` — you are @{caller}. (Ratification is a human act; "
                  f"the dossier without --confirm is open to all.)", file=sys.stderr)
            conn.close()
            return 1
        # Record the human's decision + notify the room ("re-read the law"); the human
        # then applies + commits the diff (C1, diff-only). The guards above ran first,
        # so a taken id or a changed base text is still refused.
        conn.execute("UPDATE motions SET status='ratified' WHERE id=?", (mid,))
        send(conn, "system",
             f"Constitution: M{mid} ratified ({m['op']} {m['target']}) — pending the "
             "operator's git commit; re-read the law once it lands.", kind="system")
        conn.commit()
        print(f"M{mid} marked ratified (pending your git commit) and the team notified. "
              "Apply this diff, then `git commit` it:")
        print(_unified_diff(text, new_text, path))
        return 0

    t = motion_tally(conn, mid)
    cc = len(_rule_cite_sender_sets(conn, 0).get(m["target"], set()))
    voters = ", ".join(f"{h}:{v}" for _s, h, v in t["detail"]) or "(none)"
    print(f"=== Ratify dossier — M{mid}: {m['op']} {m['target']} ===")
    print(f"proposer (self-asserted handle — a lead, not proof): {m['proposer']}")
    print(f"evidence (--because): {m['because']}")
    print(f"advisory votes (registered sessions): yea {t['yea']} / nay {t['nay']}  [{voters}]")
    if t["yea"] + t["nay"] >= 2:
        if t["single_model"]:
            print("model independence: ⚠ SINGLE-MODEL vote — a homogeneous fleet shares "
                  "priors; treat unanimity as one opinion, not a quorum.")
        elif t["models"] >= 2:
            print(f"model independence: {t['models']} distinct models among voters "
                  "(cross-model support)")
        # else: no/insufficient known models — stay quiet (match _diversity_note dormancy)
    print(f"behavioral signal: {m['target']} cited by {cc} distinct agent(s)")
    print("Votes are ADVISORY — read the evidence above, then commit the diff yourself.")
    print("\n--- proposed diff (apply by hand, then `git commit`) ---")
    diff = _unified_diff(text, new_text, path)
    print(diff if diff.strip() else "(no textual change)")
    print(f"\nThis view is READ-ONLY and repeatable. To enact: run "
          f"`ratify --confirm M{mid}` (records it + notifies the room), then apply the "
          f"diff above and `git commit` it.")
    return 0


def cmd_session(args):
    """Open / close / show a parliamentary session — a bounded deliberation window the
    whole room (and late joiners) inherit. Binds nothing; pure framing."""
    conn = connect()
    action = getattr(args, "saction", None)
    if action == "open":
        if parl_session(conn):
            cur = parl_session(conn)
            print(f"a session is already open ('{cur['title']}') — `session close` first.",
                  file=sys.stderr)
            conn.close(); return 1
        a = _resolve_for_cli(conn, args)
        sender = a["handle"] if a else (args.from_handle or "anon")
        title = " ".join(args.title).strip() if args.title else ""
        if not title:
            print("a session needs an agenda/title: session open \"<topic>\"", file=sys.stderr)
            conn.close(); return 1
        mid = open_parl_session(conn, sender, title)
        print(f"session opened (#{mid}): {title}\n"
              "Add items with `decide \"<question>\" --because ...`; record outcomes with "
              "`decision M<id> \"...\"`; close with `session close`.")
        conn.close(); return 0
    if action == "close":
        if not parl_session(conn):
            print("no open session to close")
            conn.close(); return 0
        a = _resolve_for_cli(conn, args)
        sender = a["handle"] if a else (args.from_handle or "anon")
        summary = " ".join(args.summary).strip() if getattr(args, "summary", None) else None
        close_parl_session(conn, sender, summary)
        print("session closed (open agenda items expired).")
        conn.close(); return 0
    # show
    s = parl_session(conn)
    if not s:
        print("(no open session)")
        conn.close(); return 0
    items = agenda_items(conn, s["id"])
    print(f"session #{s['id']}: {s['title']}  ({len(items)} open agenda item(s))")
    for m in items:
        t = motion_tally(conn, m["id"])
        kind = "decide" if m["op"] == "decide" else m["op"]
        print(f"  M{m['id']} [{kind}] {m['target']}  — yea {t['yea']}/nay {t['nay']}")
    conn.close(); return 0


def cmd_decide(args):
    """Put a non-constitutional question on the agenda — votable, but it can never become
    law (use `motion` for constitutional amendments)."""
    conn = connect()
    a = _resolve_for_cli(conn, args)
    sender = a["handle"] if a else (args.from_handle or "anon")
    question = " ".join(args.question).strip() if args.question else ""
    because = (args.because or "").strip()
    if not question:
        print('a decision needs a question: decide "<question>" --because "..."', file=sys.stderr)
        conn.close(); return 1
    if not because:
        print("a decision needs evidence: pass --because '<message ids / context>'",
              file=sys.stderr)
        conn.close(); return 1
    s = parl_session(conn)
    mid = add_decision_item(conn, sender, question, because,
                            session_id=(s["id"] if s else None))
    print(f"decision item M{mid} added{' to the open session' if s else ''} (advisory). "
          f"Vote with `vote --session <sid> M{mid} yea|nay`; the lead records the outcome "
          f"with `decision M{mid} \"...\"`.")
    conn.close(); return 0


def cmd_agenda(args):
    """Show the open agenda — motions + decision items, scoped to the open session if any."""
    conn = connect()
    s = parl_session(conn)
    items = agenda_items(conn, s["id"] if s else None)
    if s:
        print(f"agenda — session #{s['id']}: {s['title']}")
    if not items:
        print("(no open agenda items)")
        conn.close(); return 0
    print("Agenda — ADVISORY tallies; the room records an outcome with `decision`, "
          "nothing binds automatically:")
    for m in items:
        t = motion_tally(conn, m["id"])
        kind = "decide" if m["op"] == "decide" else m["op"]
        print(f"  M{m['id']} [{kind}] {m['target']}  — yea {t['yea']}/nay {t['nay']} "
              f"({t['voters']} voters){_diversity_note(t)} · because: {m['because']}")
    conn.close(); return 0


def cmd_decision(args):
    """[lead/operator] Record the room's outcome on a decision item — an advisory
    `kind='decision'` RECORD that binds nothing (only `ratify` + a human commit changes
    the law)."""
    conn = connect()
    ok, caller, lead = _control_caller_ok(conn, args)
    if not ok:
        print(f"only the lead (@{lead}) or the operator records decisions (you are "
              f"@{caller}).", file=sys.stderr)
        conn.close(); return 1
    mid = _parse_motion_id(args.motion)
    if mid is None:
        print(f"bad motion id {args.motion!r}", file=sys.stderr)
        conn.close(); return 1
    outcome = " ".join(args.outcome).strip() if args.outcome else ""
    if not outcome:
        print('a decision needs an outcome: decision M<id> "<what the room concluded>"',
              file=sys.stderr)
        conn.close(); return 1
    did, status = record_decision(conn, caller or "operator", mid, outcome)
    if status == "missing":
        print(f"no motion M{mid}", file=sys.stderr); conn.close(); return 1
    if status == "not-a-decision":
        print(f"M{mid} is a constitutional motion — resolve it with `ratify`, not "
              f"`decision` (decisions are for `decide` items).", file=sys.stderr)
        conn.close(); return 1
    if status == "already-resolved":
        print(f"M{mid} is already resolved (decided/expired) — not recording a duplicate.",
              file=sys.stderr)
        conn.close(); return 1
    print(f"decision recorded (#{did}) for M{mid} — advisory, binds nothing.")
    conn.close(); return 0


def cmd_decisions(args):
    """List the room's recorded decisions (the inheritance trail)."""
    conn = connect()
    rows = list_decisions(conn)
    if not rows:
        print("(no decisions recorded yet)")
    else:
        for r in rows:
            print(f"#{r['id']} {_hhmm(r['ts'])} {r['sender']}: {r['body']}")
        print(f"— {len(rows)} decision(s)")
    conn.close(); return 0


def cmd_audit(args):
    """Read-only deliberation trail — sessions, motions, votes, and decisions in order
    (the transparency / judicial view; changes nothing)."""
    conn = connect()
    rows = conn.execute(
        "SELECT id, ts, sender, kind, body FROM messages "
        "WHERE kind IN ('session','motion','vote','decision') ORDER BY id ASC").fetchall()
    if not rows:
        print("(no deliberation on record)")
        conn.close(); return 0
    for r in rows:
        print(f"#{r['id']} {_hhmm(r['ts'])} [{r['kind']}] {r['sender']}: {r['body']}")
    conn.close(); return 0


def cmd_doctor(args):
    """Run the health & staleness checker (.groupchat/doctor.py) as a first-class
    subcommand, so it's discoverable alongside the rest of the CLI rather than a
    hidden script. Loads doctor.py as a module and delegates to its main()."""
    import importlib.util
    dpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doctor.py")
    if not os.path.isfile(dpath):
        print("doctor.py not found alongside chat.py", file=sys.stderr)
        return 1
    spec = importlib.util.spec_from_file_location("_gc_doctor_cli", dpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main(["-q"] if getattr(args, "quiet", False) else [])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chat", description="Group chat bus for parallel Claude Code instances.")
    sub = p.add_subparsers(dest="command", required=True)

    def add_identity(sp):
        sp.add_argument("--session", help="Claude session id (preferred identity)")
        sp.add_argument("--from", dest="from_handle", help="agent handle to act as")

    sp = sub.add_parser("init", help="create the chat database")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("register", help="register/refresh this agent, print its handle")
    add_identity(sp)
    sp.add_argument("--cwd"); sp.add_argument("--pid", type=int); sp.add_argument("--status")
    sp.add_argument("--no-barrier", action="store_true",
                    help="this host has no Stop hook (opencode/generic) — don't let it "
                         "hold a hook team at the barrier")
    sp.set_defaults(func=cmd_register)

    sp = sub.add_parser("whoami", help="print this agent's handle")
    add_identity(sp)
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("send", aliases=["say"], help="post a message (use @handle to mention)")
    add_identity(sp)
    sp.add_argument("message", nargs="+", help="message text")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("read", help="show unread messages and advance read cursor")
    add_identity(sp)
    sp.add_argument("--peek", action="store_true", help="do not advance the read cursor")
    sp.add_argument("--include-own", action="store_true", help="include your own messages")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("inbox", help="show unread messages that @mention you")
    add_identity(sp)
    sp.add_argument("--peek", action="store_true")
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("log", help="show recent message history")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("who", help="list agents in the room")
    sp.add_argument("--all", action="store_true", help="include inactive agents")
    sp.set_defaults(func=cmd_who)

    sp = sub.add_parser("lead",
                        help="show / claim / hand off / release the lead (@human routing)")
    add_identity(sp)
    sp.add_argument("handle", nargs="?",
                    help="handle to designate / hand off to (omit to show the current lead)")
    sp.add_argument("--claim", action="store_true",
                    help="claim the lead for yourself (needs --from / --session)")
    sp.add_argument("--release", action="store_true",
                    help="step down → the deterministic floor takes over")
    sp.add_argument("--chair", action="store_true",
                    help="operate on the global CHAIR instead of your squad's captaincy")
    sp.set_defaults(func=cmd_lead)

    sp = sub.add_parser("council",
                        help="show the council: the chair + each squad's captain")
    sp.set_defaults(func=cmd_council)

    sp = sub.add_parser("questions", aliases=["escalations"],
                        help="[operator] the lead's open @human escalations awaiting your answer")
    sp.set_defaults(func=cmd_questions)

    sp = sub.add_parser("answer",
                        help="answer/relay an @human escalation by its message id "
                             "([operator] bare; a chair relays with --from <handle>)")
    add_identity(sp)
    sp.add_argument("msg_id", type=int, help="the escalation's message id (see `questions`)")
    sp.add_argument("message", nargs="+", help="your answer to the team")
    sp.set_defaults(func=cmd_answer)

    sp = sub.add_parser("tokens", help="show approximate token usage per agent")
    sp.add_argument("--all", action="store_true", help="include inactive agents")
    sp.set_defaults(func=cmd_tokens)

    sp = sub.add_parser("heartbeat", help="refresh last-seen / status")
    add_identity(sp)
    sp.add_argument("--cwd"); sp.add_argument("--pid", type=int); sp.add_argument("--status")
    sp.set_defaults(func=cmd_heartbeat)

    sp = sub.add_parser("done", help="mark your slice complete (wait at the team barrier)")
    add_identity(sp)
    sp.set_defaults(func=cmd_done)

    sp = sub.add_parser("expect", help="declare/show the expected number of agents this run")
    sp.add_argument("n", nargs="?", type=int, help="expected agent count (omit to show)")
    sp.add_argument("--squad", help="declare a sub-team's size (its own barrier)")
    sp.set_defaults(func=cmd_expect)

    sp = sub.add_parser("squad", help="show / join a sub-team (its own barrier; global lead)")
    add_identity(sp)
    sp.add_argument("name", nargs="*", help="squad to join (omit to show; empty to leave)")
    sp.set_defaults(func=cmd_squad)

    sp = sub.add_parser("model", help="show / set your model (annotates vote-tally diversity)")
    add_identity(sp)
    sp.add_argument("name", nargs="*", help="your model id (omit to show; empty to clear)")
    sp.set_defaults(func=cmd_model)

    sp = sub.add_parser("rename", help="change your handle (keeps your session/history)")
    add_identity(sp)
    sp.add_argument("new", help="your new handle (a-z, 0-9, '-', '_')")
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser("task", help="work-division ledger: add / list / claim / done")
    tsub = sp.add_subparsers(dest="action", required=True)
    ta = tsub.add_parser("add", help="add an open task to the ledger")
    ta.add_argument("rest", nargs="+", help="task title")
    ta.add_argument("--paths", help="optional path-glob hint (e.g. src/*.py)")
    add_identity(ta)
    tl = tsub.add_parser("list", help="list open + claimed tasks")
    tl.add_argument("--all", action="store_true", help="include done tasks")
    tc = tsub.add_parser("claim", help="claim an open task for yourself (atomic)")
    tc.add_argument("id", type=int, help="task id (see `task list`)")
    add_identity(tc)
    tdn = tsub.add_parser("done", help="mark a task done")
    tdn.add_argument("id", type=int, help="task id")
    add_identity(tdn)
    sp.set_defaults(func=cmd_task)

    sp = sub.add_parser("assign", help="give a teammate a task (durable ledger row + @mention)")
    add_identity(sp)
    sp.add_argument("handle", help="teammate to assign to")
    sp.add_argument("title", nargs="+", help="what to do")
    sp.add_argument("--paths", help="optional path-glob hint")
    sp.set_defaults(func=cmd_assign)

    sp = sub.add_parser("goal", help="show / set / clear the shared objective")
    sp.add_argument("text", nargs="*", help="objective text (omit to show)")
    sp.add_argument("--clear", action="store_true", help="clear the goal")
    sp.set_defaults(func=cmd_goal)

    sp = sub.add_parser("result", help="report a structured result to the orchestrator")
    add_identity(sp)
    sp.add_argument("message", nargs="+", help="the result / outcome to report")
    sp.add_argument("--task", type=int, help="task id this result closes (marks it done)")
    sp.set_defaults(func=cmd_result)

    sp = sub.add_parser("results", help="collect reported results (fan-in view)")
    sp.add_argument("--from", dest="from_handle", help="only this sender's results")
    sp.set_defaults(func=cmd_results)

    sp = sub.add_parser("summary", help="read-only digest: goal + roster + tasks + results")
    sp.set_defaults(func=cmd_summary)

    sp = sub.add_parser("worktrees", aliases=["harvest"],
                        help="read-only diff of bootstrap --worktree branches (never merges)")
    sp.add_argument("--base", help="base ref to diff against (default: current branch)")
    sp.add_argument("--cwd", help="repo dir (default: cwd)")
    sp.set_defaults(func=cmd_worktrees)

    sp = sub.add_parser("direct", help="imperatively redirect a teammate (a blocking @mention)")
    add_identity(sp)
    sp.add_argument("handle", help="the agent to direct")
    sp.add_argument("message", nargs="+", help="the instruction")
    sp.set_defaults(func=cmd_direct)

    sp = sub.add_parser("standdown", aliases=["disband"],
                        help="release the whole team from the barrier (teardown switch)")
    add_identity(sp)
    sp.add_argument("reason", nargs="*", help="optional reason to announce")
    sp.add_argument("--clear", "--lift", action="store_true", help="lift a standdown")
    sp.set_defaults(func=cmd_standdown)

    sp = sub.add_parser("dismiss",
                        help="[lead/operator] release ONE agent from the barrier")
    add_identity(sp)
    sp.add_argument("handle", help="the agent to dismiss")
    sp.set_defaults(func=cmd_dismiss)

    sp = sub.add_parser("focus", help="set / clear / show what you're working on now")
    add_identity(sp)
    sp.add_argument("text", nargs="*", help="focus text (omit to show)")
    sp.add_argument("--clear", action="store_true", help="clear your focus")
    sp.set_defaults(func=cmd_focus)

    sp = sub.add_parser("claim", help="announce intent to edit files (a soft file-claim)")
    add_identity(sp)
    sp.add_argument("glob", nargs="+", help="path or glob you're about to edit")
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("unclaim", help="release a file-claim")
    add_identity(sp)
    sp.add_argument("glob", nargs="+", help="the claim to release")
    sp.set_defaults(func=cmd_unclaim)

    sp = sub.add_parser("claims", help="list active file-claims (or --path to look one up)")
    sp.add_argument("--path", help="show who has claimed a given path")
    sp.set_defaults(func=cmd_claims)

    sp = sub.add_parser("bootstrap", aliases=["team"],
                        help="spawn other Claude instances as named teammates")
    sp.add_argument("spec", nargs="*",
                    help="a count (e.g. 3) or explicit names (e.g. frontend backend)")
    sp.add_argument("--method", choices=["terminal", "tmux", "print"],
                    help="how to launch (default: terminal on macOS, else print)")
    sp.add_argument("--cwd", help="working dir for spawned agents (default: cwd)")
    sp.add_argument("--prompt", help="initial prompt for each agent (default: none/idle)")
    sp.add_argument("--goal", help="shared objective for the team (recorded in the room; "
                                   "shown in every briefing and `who`)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the launch commands without spawning")
    sp.add_argument("--worktree", action="store_true",
                    help="give each spawned agent its own git worktree (branch "
                         "groupchat/<name>) so their file edits can't collide")
    sp.add_argument("--squad", help="spawn the agents into a sub-team with its own barrier")
    sp.add_argument("--model", help="stamp the spawned agents' model (for vote-tally "
                                    "diversity; defaults to the bootstrapper's $AGORA_MODEL)")
    sp.add_argument("--force", action="store_true",
                    help=f"a human's override: spawn past the count cap ({BOOTSTRAP_MAX}), "
                         "the spawn-depth limit, and the fleet ceiling")
    sp.set_defaults(func=cmd_bootstrap)

    sp = sub.add_parser("install", help="install group chat into a target repo")
    sp.add_argument("target", nargs="?", default=".", help="target repo root (default: cwd)")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("doctor", help="health & staleness check (code/schema/hooks/wiring)")
    sp.add_argument("-q", "--quiet", action="store_true",
                    help="only warnings/failures + summary")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("constitution", aliases=["const"],
                        help="show/init/check the coordination constitution")
    sp.add_argument("action", nargs="?", choices=["show", "init", "check"],
                    default="show", help="default: show")
    sp.set_defaults(func=cmd_constitution)

    sp = sub.add_parser("review", help="repeal-first constitution review (advisory)")
    sp.add_argument("--days", type=int, default=0, help="cite window in days (0 = all-time)")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("motion", help="propose a constitution amendment (advisory; evidence required)")
    add_identity(sp)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--rule", help="rule id to amend (R<n>), or 'new' to add an Article")
    g.add_argument("--repeal", help="rule id to repeal (R<n>)")
    sp.add_argument("--change", help="proposed new rule text (required for amend/add)")
    sp.add_argument("--title", help="short heading for a new Article (add only); else a placeholder")
    sp.add_argument("--because", required=True, help="evidence: message ids / tests / diary refs")
    sp.set_defaults(func=cmd_motion)

    sp = sub.add_parser("vote", help="cast an advisory vote on a motion (registered --session only)")
    add_identity(sp)
    sp.add_argument("motion", help="motion id, e.g. M12")
    sp.add_argument("vote", choices=["yea", "nay"])
    sp.set_defaults(func=cmd_vote)

    sp = sub.add_parser("amendments", help="list motions and their advisory tallies")
    sp.add_argument("--all", action="store_true", help="include closed/superseded/ratified motions")
    sp.set_defaults(func=cmd_amendments)

    sp = sub.add_parser("ratify", help="[human] show a motion's evidence + proposed diff (read-only); --confirm to enact")
    add_identity(sp)
    sp.add_argument("motion", help="motion id, e.g. M12")
    sp.add_argument("--confirm", action="store_true",
                    help="mark ratified + notify the team (run BEFORE applying the diff); then apply + git commit")
    sp.set_defaults(func=cmd_ratify)

    # Parliamentary framing — sessions / agendas / decisions (advisory; binds nothing).
    sp = sub.add_parser("session", help="open / close / show a deliberation session")
    ssub = sp.add_subparsers(dest="saction")
    so = ssub.add_parser("open", help="open a deliberation session")
    so.add_argument("title", nargs="+", help="the session's agenda / topic")
    add_identity(so)
    scl = ssub.add_parser("close", help="close the open session")
    scl.add_argument("--summary", nargs="*", help="optional closing summary")
    add_identity(scl)
    ssub.add_parser("show", help="show the open session + agenda")  # bare `session` too
    sp.set_defaults(func=cmd_session, saction=None)

    sp = sub.add_parser("decide", help="put a non-constitutional question on the agenda (votable)")
    add_identity(sp)
    sp.add_argument("question", nargs="+", help="the question to decide")
    sp.add_argument("--because", required=True, help="evidence: message ids / context")
    sp.set_defaults(func=cmd_decide)

    sp = sub.add_parser("agenda", help="show the open agenda (motions + decision items)")
    sp.set_defaults(func=cmd_agenda)

    sp = sub.add_parser("decision",
                        help="[lead/operator] record the room's outcome on a decision item")
    add_identity(sp)
    sp.add_argument("motion", help="the decision item's id, e.g. M12")
    sp.add_argument("outcome", nargs="+", help="what the room concluded")
    sp.set_defaults(func=cmd_decision)

    sp = sub.add_parser("decisions", help="list the room's recorded decisions")
    sp.set_defaults(func=cmd_decisions)

    sp = sub.add_parser("audit", help="read-only deliberation trail (sessions/motions/votes/decisions)")
    sp.set_defaults(func=cmd_audit)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    rc = args.func(args)
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
