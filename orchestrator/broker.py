"""orchestrator/broker.py — Component C of the publication broker
(docs/plans/2026-08-06-publication-broker-design.md). Runs as the OPERATOR, from the
operator's own factory checkout — a LaunchAgent (Component G), or `factory broker
run-once|watch|status` run by hand. NEVER touches the blackboard (the operator has no
business reading the guest-house's DB, and in production can't — guest-house homes are
700): every input here is a file (the envelope spool + the operator's own allowlist).

THE AUTHORITY LINE, enforced here in code (not trusted from the envelope): the envelope
REQUESTS; `~/.factory-broker.yaml` — the operator's own file, on the operator's own
account — AUTHORIZES. No allowlist entry matching an envelope's repo_slug/base_branch =>
reject. Every field is re-verified against LIVE state immediately before acting:
content hash, schema version, expiry, nonce-not-yet-spent, then a live `git ls-remote`
against the real remote (has anyone moved the branch since approval?) and
`merge-base --is-ancestor` (fast-forward only, NEVER a force push). Any failure writes a
receipt and is NEVER retried silently — the caller must prepare a fresh envelope.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Callable, Optional

import yaml

from ..reporting import envelope as envelope_mod


def load_allowlist(path: str) -> list[dict]:
    """The operator's own publication allowlist (600, operator home) — a list of
    {repo_slug, remote_url, base_branch, bare_path, allow_issue_ops}. Missing/unreadable/
    malformed => an EMPTY allowlist (fail-closed: every envelope then has no matching
    entry and is rejected, never silently trusted)."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return []
    pubs = data.get("publications") if isinstance(data, dict) else None
    if not isinstance(pubs, list):
        return []
    return [p for p in pubs if isinstance(p, dict)]


def find_entry(allowlist: list[dict], env: dict) -> Optional[dict]:
    """The allowlist row matching this envelope's repo_slug + base_branch, or None. This
    IS the authorization check — an envelope names no allowlist entry of its own; it can
    only match (or fail to match) one the operator already wrote."""
    repo = env.get("repo_slug", "")
    base = env.get("base_branch", "")
    for entry in allowlist:
        if entry.get("repo_slug") == repo and entry.get("base_branch") == base:
            return entry
    return None


def verify_envelope(env: Optional[dict], *, hash_path: str, receipts_dir: str,
                    allowlist: list[dict], runner: Callable = subprocess.run) -> dict:
    """Every check the authority line requires, IN ORDER, any failure => reject. Returns
    `{'ok': True, 'entry': <allowlist row>}` or `{'ok': False, 'status': 'expired'|
    'rejected', 'reason': str}` — `status` feeds straight into the receipt written by the
    caller (drill 3's exact matrix: base-moved / tamper / replayed-nonce / expiry /
    happy-path)."""
    if env is None:
        return {"ok": False, "status": "rejected", "reason": "unreadable envelope (bad JSON)"}
    nonce = env.get("nonce") or ""
    if not nonce:
        return {"ok": False, "status": "rejected", "reason": "envelope carries no nonce"}
    if not envelope_mod.verify_hash(env, hash_path):
        return {"ok": False, "status": "rejected",
               "reason": "content hash mismatch (tamper or truncation)"}
    if env.get("schema_version") != envelope_mod.SCHEMA_VERSION:
        return {"ok": False, "status": "rejected",
               "reason": f"unknown schema_version {env.get('schema_version')!r}"}
    if envelope_mod.has_receipt(receipts_dir, nonce):
        return {"ok": False, "status": "rejected",
               "reason": "nonce already has a receipt — refusing a replay"}
    if envelope_mod.is_expired(env):
        return {"ok": False, "status": "expired", "reason": "envelope expired"}
    entry = find_entry(allowlist, env)
    if entry is None:
        return {"ok": False, "status": "rejected",
               "reason": f"no allowlist entry for {env.get('repo_slug')!r} / "
                         f"{env.get('base_branch')!r}"}
    bare_path = entry.get("bare_path") or ""
    remote_url = entry.get("remote_url") or ""
    base_sha = env.get("base_sha") or ""
    tip_sha = env.get("tip_sha") or ""
    if not (bare_path and remote_url and base_sha and tip_sha):
        return {"ok": False, "status": "rejected",
               "reason": "incomplete allowlist entry or envelope shas"}

    have = runner(["git", "-C", bare_path, "cat-file", "-e", f"{tip_sha}^{{commit}}"],
                 capture_output=True, text=True, timeout=30)
    if getattr(have, "returncode", 1) != 0:
        return {"ok": False, "status": "rejected",
               "reason": f"tip_sha {tip_sha[:9]} is not an object in the bare repo"}

    # Live re-verification — nobody moved the branch since approval (drill 3's exact check).
    lsr = runner(["git", "ls-remote", remote_url, f"refs/heads/{env.get('base_branch')}"],
                capture_output=True, text=True, timeout=30)
    if getattr(lsr, "returncode", 1) != 0:
        return {"ok": False, "status": "rejected",
               "reason": "ls-remote failed — cannot verify the live base"}
    out = (lsr.stdout or "").strip()
    remote_sha = out.split()[0] if out else ""
    if remote_sha != base_sha:
        return {"ok": False, "status": "rejected",
               "reason": (f"base moved: envelope pinned {base_sha[:9]}, live "
                         f"{env.get('base_branch')} is {remote_sha[:9] if remote_sha else '(absent)'}")}

    # Fast-forward only — NEVER a force push.
    ff = runner(["git", "-C", bare_path, "merge-base", "--is-ancestor", base_sha, tip_sha],
               capture_output=True, text=True, timeout=30)
    if getattr(ff, "returncode", 1) != 0:
        return {"ok": False, "status": "rejected",
               "reason": f"{tip_sha[:9]} is not a fast-forward of {base_sha[:9]}"}

    return {"ok": True, "entry": entry}


def _execute_issue_action(repo: str, action: dict, *, runner: Callable) -> dict:
    """Execute exactly what the envelope previewed — the body text was rendered by the
    factory at prepare time (`reporting.issue_sync._issue_actions_from_sync`); the broker
    never re-derives it. One action's `gh` failure must not abort the others (mirrors
    `reporting.issue_sync.sync_issues`'s own per-issue try/except)."""
    number = action.get("number")
    body = action.get("body", "")
    op = action.get("op", "comment")
    try:
        out = runner(["gh", "issue", "comment", str(number), "-R", repo, "--body", body],
                    capture_output=True, text=True, timeout=30)
        if getattr(out, "returncode", 1) != 0:
            raise RuntimeError((out.stderr or "gh issue comment failed").strip())
        if op == "close":
            cl = runner(["gh", "issue", "close", str(number), "-R", repo],
                       capture_output=True, text=True, timeout=30)
            if getattr(cl, "returncode", 1) != 0:
                raise RuntimeError((cl.stderr or "gh issue close failed").strip())
        return {"number": number, "op": op, "ok": True}
    except Exception as e:  # noqa: BLE001 — one issue's gh failure must not abort the rest
        return {"number": number, "op": op, "ok": False, "detail": str(e)[:200]}


def execute_envelope(env: dict, entry: dict, *, runner: Callable = subprocess.run) -> dict:
    """Push `tip_sha` to the REAL remote (the operator's own git credential, via `runner`
    — never anything the factory user could have supplied) then run the envelope's own
    issue actions. Returns `{'ok': True, 'sha', 'detail', 'issue_results'}` or
    `{'ok': False, 'detail': str}` on a push failure (no issue actions run)."""
    bare_path = entry["bare_path"]
    remote_url = entry["remote_url"]
    base_branch = env["base_branch"]
    tip_sha = env["tip_sha"]
    push = runner(["git", "-C", bare_path, "push", remote_url,
                  f"{tip_sha}:refs/heads/{base_branch}"],
                 capture_output=True, text=True, timeout=120)
    if getattr(push, "returncode", 1) != 0:
        detail = (getattr(push, "stderr", "") or getattr(push, "stdout", "") or "").strip()[:300]
        return {"ok": False, "detail": f"push failed: {detail}"}

    issue_results = []
    actions = env.get("issue_actions") or []
    if actions and entry.get("allow_issue_ops", True):
        repo = env.get("repo_slug", "")
        issue_results = [_execute_issue_action(repo, act, runner=runner) for act in actions]
    elif actions:
        issue_results = [{"number": a.get("number"), "op": a.get("op"), "ok": False,
                          "detail": "issue ops not allowed for this allowlist entry"}
                         for a in actions]
    return {"ok": True, "sha": tip_sha,
           "detail": f"pushed {tip_sha[:9]} to {remote_url}#{base_branch}",
           "issue_results": issue_results}


def _archive_outbox(json_path: str, hash_path: str, done_dir: str) -> None:
    os.makedirs(done_dir, exist_ok=True)
    for p in (json_path, hash_path):
        if os.path.isfile(p):
            os.replace(p, os.path.join(done_dir, os.path.basename(p)))


def run_once(*, outbox_dir: str, receipts_dir: str, allowlist_path: str,
            runner: Callable = subprocess.run, done_dir: Optional[str] = None) -> list[dict]:
    """Process every envelope currently in `outbox_dir`: verify (allowlist authorizes,
    live git re-verifies), execute on success, ALWAYS write exactly one receipt, then
    archive the envelope out of the live outbox (so a crash mid-loop reprocesses only what
    never got a receipt — has_receipt inside verify_envelope is the belt to this
    suspenders). Returns one `{'nonce', 'status'}` per envelope processed."""
    done_dir = done_dir or os.path.join(outbox_dir, "done")
    allowlist = load_allowlist(allowlist_path)
    results = []
    for nonce in envelope_mod.list_outbox(outbox_dir):
        json_path = os.path.join(outbox_dir, f"{nonce}.json")
        hash_path = f"{json_path}.sha256"
        env = envelope_mod.read_envelope(json_path)
        verdict = verify_envelope(env, hash_path=hash_path, receipts_dir=receipts_dir,
                                  allowlist=allowlist, runner=runner)
        policy = (env or {}).get("policy_hash", "")
        if verdict["ok"]:
            outcome = execute_envelope(env, verdict["entry"], runner=runner)
            status = "pushed" if outcome["ok"] else "rejected"
            envelope_mod.write_receipt(nonce=nonce, status=status, receipts_dir=receipts_dir,
                                       receipt_sha=outcome.get("sha", ""),
                                       detail=outcome.get("detail", ""), policy_hash=policy)
        else:
            status = verdict.get("status", "rejected")
            envelope_mod.write_receipt(nonce=nonce, status=status, receipts_dir=receipts_dir,
                                       detail=verdict.get("reason", ""), policy_hash=policy)
        _archive_outbox(json_path, hash_path, done_dir)
        results.append({"nonce": nonce, "status": status})
    return results


def watch(*, outbox_dir: str, receipts_dir: str, allowlist_path: str,
         runner: Callable = subprocess.run, poll_s: float = 5.0,
         max_iters: Optional[int] = None, sleep_fn: Callable = time.sleep,
         done_dir: Optional[str] = None) -> int:
    """Poll loop: `run_once` every `poll_s` seconds. `max_iters=None` runs forever (the
    manual/dev fallback — `factory broker watch`); the LaunchAgent instead uses WatchPaths
    on the outbox and fires `run-once` per filesystem event (Component G), so production
    never actually calls this with `max_iters=None`. Returns the number of iterations run
    (a test seam)."""
    n = 0
    while max_iters is None or n < max_iters:
        run_once(outbox_dir=outbox_dir, receipts_dir=receipts_dir,
                 allowlist_path=allowlist_path, runner=runner, done_dir=done_dir)
        n += 1
        if max_iters is None or n < max_iters:
            sleep_fn(poll_s)
    return n


def status(*, outbox_dir: str, receipts_dir: str) -> dict:
    """A cheap snapshot for `factory broker status` — no allowlist load, no git/gh calls."""
    return {"pending": envelope_mod.list_outbox(outbox_dir),
           "receipts": envelope_mod.list_receipts(receipts_dir)}
