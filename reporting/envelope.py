"""reporting/envelope.py — Component A of the publication broker
(docs/plans/2026-08-06-publication-broker-design.md). Factory side.

THE AUTHORITY LINE (this design's spine, repeated here because this module is the shape
of the thing it bounds): the factory PREPARES publications; it never executes them when
the broker is armed. An envelope is a REQUEST — the broker's own operator-side allowlist,
never any envelope field, is the authority on what may be pushed where. The broker
re-verifies every sha against live state immediately before acting; mismatch, expiry, or
a reused nonce invalidates the envelope permanently.

This module is pure I/O + pure functions — no store, no subprocess, no policy about WHEN
to build an envelope (that's reporting/issue_sync.py's graduate_and_prepare_envelope /
promote_and_prepare_envelope) or how to verify one against live git state (that's
orchestrator/broker.py, which runs as the OPERATOR and cannot even import this factory-
tree module in production — it vendors nothing from the factory user's 700 home; in the
dev/single-user layout the SAME code paths are exercised because operator and factory are
the same account, which is exactly why the envelope format is a self-contained JSON file
rather than anything DB-shaped).
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

SCHEMA_VERSION = 1

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


def build_envelope(*, action: str, repo_slug: str, base_branch: str, base_sha: str,
                   tip_sha: str, range_: str, n_commits: int, approval_id: int,
                   policy_hash: str, issue_actions: Optional[list] = None,
                   ttl_hours: float = 24.0, nonce: Optional[str] = None,
                   now: Optional[datetime] = None) -> dict:
    """Build one envelope dict (pure — no I/O; `write_envelope` persists it). `action` is
    'graduate' or 'promote'. `nonce`/`now` are test seams; production always mints a fresh
    uuid4 nonce and the real clock."""
    created = now or _now()
    expires = created + timedelta(hours=ttl_hours)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "repo_slug": repo_slug,
        "base_branch": base_branch,
        "base_sha": base_sha,
        "tip_sha": tip_sha,
        "range": range_,
        "n_commits": int(n_commits),
        "issue_actions": issue_actions or [],
        "approval_id": approval_id,
        "policy_hash": policy_hash,
        "created_at": _iso(created),
        "expires_at": _iso(expires),
        "nonce": nonce or uuid.uuid4().hex,
    }


def content_hash(envelope: dict) -> str:
    """sha256 of the envelope's canonical JSON (sorted keys, compact separators) — the
    broker recomputes this from the file on disk and refuses on mismatch (a cheap
    tamper/truncation guard, per the design's Component A)."""
    blob = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _atomic_write(path: str, text: str) -> None:
    """Write-to-temp-then-rename so a concurrent reader (the broker's poll) never sees a
    half-written file — `os.replace` is atomic on the same filesystem."""
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_envelope(envelope: dict, outbox_dir: str) -> tuple[str, str]:
    """Write `<nonce>.json` + `<nonce>.json.sha256` to `outbox_dir` (created if absent).
    Returns (json_path, hash_path)."""
    os.makedirs(outbox_dir, exist_ok=True)
    nonce = envelope["nonce"]
    json_path = os.path.join(outbox_dir, f"{nonce}.json")
    hash_path = f"{json_path}.sha256"
    _atomic_write(json_path, json.dumps(envelope, sort_keys=True, indent=2))
    _atomic_write(hash_path, content_hash(envelope))
    return json_path, hash_path


def read_envelope(json_path: str) -> Optional[dict]:
    """The envelope dict, or None on any I/O/parse failure (a broker that can't read an
    envelope must reject it, never crash — the caller maps None to a 'rejected' receipt)."""
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def verify_hash(envelope: dict, hash_path: str) -> bool:
    """True iff the sidecar hash file's content matches the recomputed hash of `envelope`
    — the tamper/truncation guard the broker runs BEFORE trusting anything else in it."""
    try:
        with open(hash_path, "r", encoding="utf-8") as fh:
            stored = fh.read().strip()
    except OSError:
        return False
    return stored == content_hash(envelope)


def is_expired(envelope: dict, *, now: Optional[datetime] = None) -> bool:
    """Fail-CLOSED: a missing/unparsable `expires_at` counts as expired, never as
    unbounded validity."""
    now = now or _now()
    try:
        exp = datetime.strptime(envelope["expires_at"], _TS_FMT).replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True
    return now >= exp


def list_outbox(outbox_dir: str) -> list[str]:
    """Nonces with a live `<nonce>.json` in `outbox_dir`, oldest first (mtime) — the
    broker's poll source. A `.tmp-*` (mid-write) file is never listed."""
    if not os.path.isdir(outbox_dir):
        return []
    nonces = [n[:-len(".json")] for n in os.listdir(outbox_dir)
             if n.endswith(".json") and ".tmp" not in n]
    nonces.sort(key=lambda n: os.path.getmtime(os.path.join(outbox_dir, f"{n}.json")))
    return nonces


# -- receipts (broker -> factory) -------------------------------------------------------
def write_receipt(*, nonce: str, status: str, receipts_dir: str, receipt_sha: str = "",
                  detail: str = "", policy_hash: str = "",
                  now: Optional[datetime] = None) -> str:
    """`status` is one of 'pushed' | 'rejected' | 'expired' (the design's receipt shape).
    Written by the broker; read by the factory (`ingest_broker_receipts`)."""
    os.makedirs(receipts_dir, exist_ok=True)
    path = os.path.join(receipts_dir, f"{nonce}.receipt.json")
    payload = {"nonce": nonce, "status": status, "receipt_sha": receipt_sha,
              "detail": detail, "policy_hash": policy_hash,
              "executed_at": _iso(now or _now())}
    _atomic_write(path, json.dumps(payload, sort_keys=True, indent=2))
    return path


def read_receipt(receipts_dir: str, nonce: str) -> Optional[dict]:
    path = os.path.join(receipts_dir, f"{nonce}.receipt.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def has_receipt(receipts_dir: str, nonce: str) -> bool:
    """True iff `nonce` already has a receipt — a SPENT nonce. The broker refuses to
    execute an envelope whose nonce already has one (at-least-once prep, exactly-once
    intent; see run_once's own idempotency note for the residual gh-side risk)."""
    return os.path.isfile(os.path.join(receipts_dir, f"{nonce}.receipt.json"))


def list_receipts(receipts_dir: str) -> list[str]:
    """Nonces with an unarchived receipt in `receipts_dir` (its top level — NOT the
    `done/` subdir), oldest first."""
    if not os.path.isdir(receipts_dir):
        return []
    suffix = ".receipt.json"
    names = [n[:-len(suffix)] for n in os.listdir(receipts_dir)
            if n.endswith(suffix) and os.path.isfile(os.path.join(receipts_dir, n))]
    names.sort(key=lambda n: os.path.getmtime(os.path.join(receipts_dir, f"{n}{suffix}")))
    return names


def archive_receipt(receipts_dir: str, nonce: str, done_dir: str) -> Optional[str]:
    """Move a CONSUMED receipt out of the live `receipts_dir` into `done_dir`, so a repeat
    ingestion sweep never reprocesses it. Returns the new path, or None if there was no
    live receipt for `nonce` (already archived, or never existed)."""
    src = os.path.join(receipts_dir, f"{nonce}.receipt.json")
    if not os.path.isfile(src):
        return None
    os.makedirs(done_dir, exist_ok=True)
    dst = os.path.join(done_dir, f"{nonce}.receipt.json")
    os.replace(src, dst)
    return dst


def policy_hash(config_path: Optional[str] = None) -> str:
    """sha256 of config.yaml's raw bytes — recorded into every envelope/receipt as an
    INFORMATIONAL field (the design: 'operator config may legitimately differ'); never
    itself an authority check."""
    from ..common import paths
    path = config_path or paths.CONFIG_YAML
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""
