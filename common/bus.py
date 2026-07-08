"""common/bus.py — the factory's ONLY programmatic gateway to its vendored coordination
bus (vendor/agora/chat.py). Design: docs/plans/2026-07-08-factory-owned-bus-human-queue-
design.md §1.

Why subprocess, not `import`: vendor/agora/chat.py is a ~4.5k-line CLI module with its own
argparse surface and a stable *command-line* contract — the same one workers, conductors,
and humans already use interactively (`python3 vendor/agora/chat.py send --from <handle>
"..."`). Importing it would couple us to its internals (module-level globals, a sqlite
connection lifecycle, CLI-only helpers) instead of the contract it actually promises.
Shelling out to the exact binary agents invoke also guarantees the factory reads EXACTLY
what a worker would see on the bus — zero drift between "the bus as workers use it" and
"the bus as the dashboard/human-queue reads it".

Why every public function here NEVER raises: the bus is a coordination nicety, not a build
dependency. A locked/missing chat.db, a stale vendor path, or a hung subprocess must
degrade the caller (dashboard feed, human queue, a role's `announce`) to an empty/False
result — never take down a shift, a role call, or the fleet server. Each public function is
a hard boundary: it catches everything `_run`/parsing can throw, prints one `[bus]`-
prefixed line (non-fatal, log-and-continue), and returns its type's neutral value.

No `--json` flag exists on the vendored CLI (checked via `chat.py <subcommand> --help` for
every subcommand used here) — `open_questions`/`who`/`recent` are therefore best-effort
line parsers tied to the CLI's current text formatting (`format_message` / `cmd_questions`
/ `cmd_who` in vendor/agora/chat.py). A cosmetic change to that formatting on re-vendor
would silently degrade these parsers to []; VENDORED.md's re-vendor procedure should note
re-running the discovery in tests/test_bus.py.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional

from . import paths

VENDORED_CHAT = paths.factory("vendor", "agora", "chat.py")


def _default_bus_dir() -> str:
    """Where the factory's OWN bus lives on disk, absent an explicit override. Mirrors
    roles/common.py:factory_agora_dir() — duplicated rather than imported: common/ is the
    base layer every plane depends on (ARCHITECTURE.md), so it must not import from
    roles/. Keep the two search orders in sync if either ever changes."""
    for name in (".agora", ".groupchat"):
        d = paths.factory(name)
        if os.path.isdir(d):
            return d
    return paths.factory(".groupchat")


def bus_db_path(bus_dir: Optional[str] = None) -> Optional[str]:
    """The ONE resolution point for chat.db's filesystem path — shared by read-only sqlite
    consumers (reporting/collab.py) and the subprocess calls in this module, so they always
    agree on which bus they're touching. `bus_dir` is an explicit override and wins over the
    env vars (mirrors how AGORA_DIR is set for `_run` below); with none given, this matches
    the bus-resolution order collab.py used before this module existed: AGORA_DIR /
    GROUPCHAT_DIR env override, else the factory-local .agora/.groupchat. None when no
    chat.db exists anywhere in the search order — callers must degrade gracefully (no bus
    on disk is a normal, expected state, e.g. before the first `send`)."""
    if bus_dir:
        p = os.path.join(bus_dir, "chat.db")
        if os.path.exists(p):
            return p
    for env in ("AGORA_DIR", "GROUPCHAT_DIR"):
        d = os.environ.get(env)
        if d and os.path.exists(os.path.join(d, "chat.db")):
            return os.path.join(d, "chat.db")
    for name in (".agora", ".groupchat"):
        p = paths.factory(name, "chat.db")
        if os.path.exists(p):
            return p
    return None


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


def send(text: str, frm: str = "factory", bus_dir: Optional[str] = None,
          runner=subprocess.run, timeout: int = 30) -> bool:
    """Post `text` to the bus as `frm`. True on success; False on ANY failure (bad bus_dir,
    a raising runner, a non-zero exit) — never raises."""
    try:
        r = _run(["send", "--from", frm, text], bus_dir=bus_dir, runner=runner, timeout=timeout)
        if r.returncode != 0:
            _fail("send", r)
            return False
        return True
    except Exception as e:  # bus outage must not kill the caller
        print(f"[bus] send unavailable: {e}")
        return False


def answer(msg_id, text: str, frm: str = "operator", bus_dir: Optional[str] = None,
           runner=subprocess.run, timeout: int = 30) -> bool:
    """Answer escalation `msg_id`. `frm` defaults to 'operator' — an UNREGISTERED handle
    with no --session, which the vendored CLI resolves to no caller identity and so posts
    the reply as the bare 'human' (vendor/agora/chat.py:cmd_answer) — exactly the
    operator-answer path (verified empirically in tests/test_bus.py). True on success;
    False on any failure — never raises."""
    try:
        r = _run(["answer", str(msg_id), text, "--from", frm],
                  bus_dir=bus_dir, runner=runner, timeout=timeout)
        if r.returncode != 0:
            _fail("answer", r)
            return False
        return True
    except Exception as e:
        print(f"[bus] answer unavailable: {e}")
        return False


# `chat questions` prints indented rows "  #<id> <HH:MM> @<handle>: <body>" under an
# "awaiting you" header (vendor/agora/chat.py:cmd_questions:_show) — see that function's
# docstring for the exact format this mirrors.
_QUESTION_LINE_RE = re.compile(r"^\s*#(?P<id>\d+) (?P<ts>\d{2}:\d{2}) @(?P<sender>\S+): (?P<text>.*)$")


def open_questions(bus_dir: Optional[str] = None, runner=subprocess.run,
                    timeout: int = 30) -> list[dict]:
    """Open @human escalations awaiting the OPERATOR, as [{id, ts, sender, text}, ...] —
    parsed from `chat questions`. A captain's escalation still in flight to the chair
    (printed under a separate "N escalation(s) in flight to the chair" section) is NOT the
    operator's to answer directly (vendor/agora/chat.py:cmd_questions) and is deliberately
    excluded here. [] on any failure or when nothing is awaiting a reply — never raises."""
    try:
        r = _run(["questions"], bus_dir=bus_dir, runner=runner, timeout=timeout)
        if r.returncode != 0:
            _fail("questions", r)
            return []
        out: list[dict] = []
        awaiting = False       # True only while inside the "awaiting you" section
        for line in (r.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("open escalation"):
                awaiting = True
                continue
            if "escalation(s) in flight" in stripped or stripped.startswith("(no "):
                awaiting = False
                continue
            if not awaiting:
                continue
            m = _QUESTION_LINE_RE.match(line)
            if m:
                out.append({"id": int(m.group("id")), "ts": m.group("ts"),
                            "sender": m.group("sender"), "text": m.group("text")})
        return out
    except Exception as e:
        print(f"[bus] questions unavailable: {e}")
        return []


# `chat who` prints "<flag> <handle>[ crown][ squad][ status][ focus][ cwd]  (seen HH:MM)..."
# per active agent (vendor/agora/chat.py:cmd_who) — flag is one of ● / ◐ / ○. The handle is
# always the first whitespace-delimited token after the flag.
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


# `chat log` prints "[#<id> <HH:MM> <sender>[ → @a @b]] <body>" per message
# (vendor/agora/chat.py:format_message) — an optional leading "★ " highlight star and an
# optional " (<kind>)" tag (non-chat kinds) are tolerated but not captured.
_LOG_LINE_RE = re.compile(
    r"^(?:★ )?\[#(?P<id>\d+) (?P<ts>\d{2}:\d{2}) (?P<sender>[^\s\]]+)"
    r"(?: → [^\]]+)?\](?: \(\w+\))? (?P<text>.*)$")


def recent(n: int = 50, bus_dir: Optional[str] = None, runner=subprocess.run,
           timeout: int = 30) -> list[dict]:
    """Last `n` bus messages, oldest-first (the CLI's own `log` ordering) as
    [{id, ts, sender, text}, ...]. [] on any failure or an empty bus — never raises."""
    try:
        r = _run(["log", "--limit", str(n)], bus_dir=bus_dir, runner=runner, timeout=timeout)
        if r.returncode != 0:
            _fail("log", r)
            return []
        out = []
        for line in (r.stdout or "").splitlines():
            m = _LOG_LINE_RE.match(line)
            if m:
                out.append({"id": int(m.group("id")), "ts": m.group("ts"),
                            "sender": m.group("sender"), "text": m.group("text")})
        return out
    except Exception as e:
        print(f"[bus] log unavailable: {e}")
        return []
