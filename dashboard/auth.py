"""dashboard/auth.py — the board's write-side credential.

WHY THIS EXISTS (Phase 3 prerequisite, docs/plans/2026-08-09-worker-isolation-design.md):
`fleet_server.do_POST` serves nine state-changing routes — `/api/queue/approval`,
`/api/resume`, `/api/settings`, `/api/mission`, … — on 127.0.0.1 with no authentication at
all. Its only guard was a CSRF check whose first branch is `if not origin: return True`, so
a request that simply sends no `Origin` header (any `curl`, any script) passed it. Binding
to localhost is not an identity boundary: every local account — the factory user, a worker,
a grader, any other login — reaches 127.0.0.1. So ANY local process could approve a
publication, clear the killswitch, flip a merge gate, or re-steer the mission.

That also made the roadmap's Phase-2 canonicality claim ("dashboard = authenticated command
submission only") false, and it would have let Phase 3's own isolation drill pass while the
isolated process simply asked the dashboard to act on its behalf.

THE MODEL: a shared secret in a file only the factory user can read (0600 in FACTORY_ROOT,
which is 0700 in a correct guest house). Every write route requires it; reads are
unchanged. The operator obtains it once — the board prints a `?k=…` URL at startup — and
the page keeps it in localStorage and sends it as `X-Factory-Token` on writes.

WHAT THIS IS NOT: it is not per-user auth and not a session system. One secret, one
purpose — proving the caller could read a file inside the factory's home. That is exactly
the property an isolated grader/worker must not have, and nothing more.
"""
from __future__ import annotations

import hmac
import os
import secrets
from typing import Optional

from ..common import paths

HEADER = "X-Factory-Token"
QUERY_KEY = "k"
_TOKEN_BASENAME = ".dashboard-token"


def token_path(root: Optional[str] = None) -> str:
    return os.path.join(root or paths.FACTORY_ROOT, _TOKEN_BASENAME)


def load_token(root: Optional[str] = None) -> str:
    """The current token, or '' when there is none. Never creates one — a verifier that
    silently minted a secret would accept the next caller to ask."""
    try:
        with open(token_path(root), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def ensure_token(root: Optional[str] = None) -> str:
    """Return the existing token, creating one (0600) if absent. Called by the SERVER at
    startup, never by a request path."""
    existing = load_token(root)
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    path = token_path(root)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)          # explicit: umask must not widen it
    except OSError:  # noqa: BLE001
        pass
    return token


def verify(provided: Optional[str], root: Optional[str] = None) -> bool:
    """Constant-time comparison against the on-disk token. Fails CLOSED: an absent or empty
    token file denies every write rather than allowing them — a server that cannot read its
    own secret has no way to tell an operator from a worker."""
    expected = load_token(root)
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.strip(), expected)


def board_url(host: str, port: int, token: str) -> str:
    return f"http://{host}:{port}/?{QUERY_KEY}={token}"


def startup_banner(host: str, port: int, token: str) -> str:
    """What the operator needs, printed where they will actually see it. The token file
    lives inside the factory's 0700 home, so in a guest-house deployment this banner (and
    the daemon log it lands in) is the operator's channel to it."""
    return (f"\n  Board is write-protected. Open it ONCE with this URL — the page keeps the\n"
            f"  key in localStorage afterwards:\n\n"
            f"    {board_url(host, port, token)}\n\n"
            f"  Reads need no key; every write route does. Anyone who cannot read\n"
            f"  {token_path()} cannot approve, resume, or re-steer.\n")
