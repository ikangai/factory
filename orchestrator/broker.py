"""orchestrator/broker.py — Component C of the publication broker
(docs/plans/2026-08-06-publication-broker-design.md). Runs as the OPERATOR, from the
operator's own factory checkout — a LaunchAgent (Component G), or `factory broker
run-once|watch|status|pin|unpin|pins` run by hand. NEVER touches the blackboard (the
operator has no business reading the guest-house's DB, and in production can't — guest-
house homes are 700): every input here is a file (the shared envelope spool + the
operator's OWN allowlist/pin-store/spent-ledger).

THE AUTHORITY LINE (corrected, security fix round 2026-08-07 — the envelope's shas alone
are NOT authority over CONTENT, only over destination + liveness):

- `~/.factory-broker.yaml` (the allowlist) authorizes the DESTINATION — repo_slug +
  base_branch must match an entry the operator wrote; no entry => reject.
- `~/.factory-broker/pins` (operator-owned, NEVER the shared/factory-writable spool)
  authorizes the CONTENT — `tip_sha` must be a sha the OPERATOR explicitly pinned (either
  ahead of time via `factory broker pin <sha>`, or interactively, in this same run, by
  reviewing the operator-DERIVED diff — never the envelope's own claims — and confirming).
  This is what makes "cannot publish anything other than the exact artifact approved"
  true: under the guest-house threat model the factory (and everything it writes,
  including the envelope's own base_sha/tip_sha/range/n_commits fields) is attacker-
  controlled, so those fields are DISPLAY-ONLY claims, never trusted as consent.
- `~/.factory-broker/spent` (operator-owned) authorizes that a nonce has not already been
  acted on — the REAL replay guard (the shared spool's own `.receipt.json` copy is
  informational only; it lives in factory-writable space and could be deleted/re-created
  by the very actor the guard exists to stop).
- Every field is ALSO re-verified against LIVE state immediately before acting: content
  hash, schema version, nonce/filename identity, expiry, then a live `git ls-remote`
  against the real remote (has anyone moved the branch since approval?) and
  `merge-base --is-ancestor` (fast-forward only, NEVER a force push).
- WHAT THIS DOES NOT DEFEND AGAINST: an allowlist entry with `require_pin: false`, or an
  operator who pins a sha without actually reviewing it, gets none of the content-
  authenticity guarantee above — only destination/liveness checks remain. `watch`
  (unattended, no human in the loop) REQUIRES `require_pin` to stay in force for exactly
  this reason; it never bypasses the pin gate itself.

Any failure writes a receipt and marks the nonce spent — NEVER retried silently for the
SAME envelope; the caller must prepare a fresh one. The one exception is "unpinned, running
unattended": that is a SOFT/pending outcome (not spent, not archived) so a later
`factory broker pin <sha>` followed by a later run can still complete it — see `run_once`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import yaml

from ..reporting import envelope as envelope_mod

# A nonce must look like this BEFORE it is ever used to build a path (pin/spent/receipt
# filenames) — defense in depth against a malicious envelope's own `nonce` FIELD (free-
# form JSON, unlike a filename-derived nonce, which the OS already constrains) ever being
# used to construct a path outside the intended directory.
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# gh issue actions: a hard cap so a compromised factory can't hand the broker an
# unbounded action list; op must be exactly one of these — never reinterpreted.
_VALID_ISSUE_OPS = {"close", "comment"}
MAX_ISSUE_ACTIONS = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _valid_nonce(nonce) -> bool:
    return isinstance(nonce, str) and bool(_NONCE_RE.match(nonce))


def _atomic_write(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # noqa: BLE001 — best-effort on platforms/filesystems that refuse it
        pass


# -- the allowlist: authorizes the DESTINATION -------------------------------------------
def load_allowlist(path: str) -> list[dict]:
    """The operator's own publication allowlist (600, operator home) — a list of
    {repo_slug, remote_url, base_branch, bare_path, allow_issue_ops, require_pin}.
    Missing/unreadable/malformed => an EMPTY allowlist (fail-closed: every envelope then
    has no matching entry and is rejected, never silently trusted)."""
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
    IS the destination-authorization check — an envelope names no allowlist entry of its
    own; it can only match (or fail to match) one the operator already wrote."""
    repo = env.get("repo_slug", "")
    base = env.get("base_branch", "")
    for entry in allowlist:
        if entry.get("repo_slug") == repo and entry.get("base_branch") == base:
            return entry
    return None


# -- the pin store: authorizes the CONTENT (CRITICAL-1) ------------------------------------
def load_pins(path: str) -> dict:
    """{tip_sha: {"note": str, "pinned_at": iso}} — the operator's own record of APPROVED
    content. Missing/corrupt file => {} (fail-closed: nothing is pinned, so `require_pin`
    rejects everything until the operator actually pins something)."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_pinned(path: str, tip_sha: str) -> bool:
    return bool(tip_sha) and tip_sha in load_pins(path)


def pin_tip(path: str, tip_sha: str, *, note: str = "") -> None:
    """Record the operator's approval of an exact sha — `factory broker pin <sha>`, or the
    interactive confirm flow in `run_once`. Raises ValueError on a malformed sha (never
    silently accepts garbage as "pinned")."""
    if not re.match(r"^[0-9a-fA-F]{7,40}$", tip_sha or ""):
        raise ValueError(f"not a sha: {tip_sha!r}")
    pins = load_pins(path)
    pins[tip_sha] = {"note": note, "pinned_at": _now_iso()}
    _atomic_write(path, json.dumps(pins, sort_keys=True, indent=2))


def unpin_tip(path: str, tip_sha: str) -> bool:
    pins = load_pins(path)
    if tip_sha not in pins:
        return False
    del pins[tip_sha]
    _atomic_write(path, json.dumps(pins, sort_keys=True, indent=2))
    return True


def list_pins(path: str) -> dict:
    return load_pins(path)


# -- the spent-nonce ledger: THE replay-guard authority (IMPORTANT-2/3) --------------------
def load_spent(path: str) -> set:
    """Every nonce this broker has ever finalized (pushed, rejected, expired, or declined)
    — an append-only JSONL ledger, operator-owned. A malformed line is skipped, not fatal
    (a corrupt tail must not resurrect every prior spend)."""
    spent: set = set()
    if not path or not os.path.isfile(path):
        return spent
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                n = row.get("nonce") if isinstance(row, dict) else None
                if isinstance(n, str):
                    spent.add(n)
    except OSError:
        return spent
    return spent


def is_spent(path: str, nonce: str) -> bool:
    return nonce in load_spent(path)


def mark_spent(path: str, nonce: str, *, status: str = "") -> None:
    """Append-only — never rewrites/removes a prior line (a real ledger, not a set that
    could be truncated back to 'unspent' by a partial write)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps({"nonce": nonce, "status": status, "spent_at": _now_iso()},
                      sort_keys=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:  # noqa: BLE001
        pass


# -- verification: destination (allowlist) + liveness (live git) + content (pin) -----------
def verify_envelope(env: Optional[dict], *, filename_nonce: str, hash_path: str,
                    allowlist: list[dict], spent_path: str, pins_path: str,
                    runner: Callable = subprocess.run) -> dict:
    """Every check the (corrected) authority line requires, IN ORDER, any failure =>
    reject/pending. Returns `{'ok': True, 'entry': <allowlist row>}`,
    `{'ok': False, 'status': 'unpinned', 'reason': str, 'entry': <allowlist row>}` (the
    ONLY structurally-clean-but-not-yet-authorized outcome — the caller may still resolve
    it via an interactive confirm), or `{'ok': False, 'status': 'expired'|'rejected',
    'reason': str}`."""
    if not _valid_nonce(filename_nonce):
        return {"ok": False, "status": "rejected",
               "reason": f"malformed nonce (refusing to use it to build a path): "
                         f"{filename_nonce!r}"}
    if env is None:
        return {"ok": False, "status": "rejected", "reason": "unreadable envelope (bad JSON)"}
    nonce = env.get("nonce")
    if not _valid_nonce(nonce):
        return {"ok": False, "status": "rejected",
               "reason": f"envelope carries a malformed/missing nonce: {nonce!r}"}
    # IMPORTANT-3: nonce identity must be SINGLE — the filename (what list_outbox/run_once
    # actually key spend/receipt records on) and the envelope's own content-declared nonce
    # must agree, or the replay guard is bypassable by simply desynchronizing them.
    if nonce != filename_nonce:
        return {"ok": False, "status": "rejected",
               "reason": (f"nonce mismatch: envelope content says {nonce!r}, "
                         f"filename says {filename_nonce!r}")}
    if not envelope_mod.verify_hash(env, hash_path):
        return {"ok": False, "status": "rejected",
               "reason": "content hash mismatch (tamper or truncation)"}
    if env.get("schema_version") != envelope_mod.SCHEMA_VERSION:
        return {"ok": False, "status": "rejected",
               "reason": f"unknown schema_version {env.get('schema_version')!r}"}
    # IMPORTANT-2: the spent check is now the OPERATOR-owned ledger, never the shared
    # spool's receipt copy (factory-writable — an attacker who can delete a receipt file
    # can already re-drop the archived envelope too, since both used to live in the SAME
    # factory-writable tree; see run_once's own processed_dir fix).
    if is_spent(spent_path, nonce):
        return {"ok": False, "status": "rejected",
               "reason": "nonce already spent — refusing a replay"}
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

    # CRITICAL-1b: the AUTHENTICITY gate. Everything above proves the envelope is
    # internally consistent and points at a live, reachable, fast-forward destination —
    # NONE of that proves the OPERATOR ever saw or approved this exact content (base_sha/
    # tip_sha/range/n_commits are all factory-supplied claims under the guest-house threat
    # model). require_pin defaults TRUE — an entry must opt OUT explicitly and knowingly.
    if entry.get("require_pin", True) and not is_pinned(pins_path, tip_sha):
        return {"ok": False, "status": "unpinned",
               "reason": (f"tip {tip_sha[:9]} not pinned by the operator — run "
                         f"`factory broker run-once` interactively to review + confirm, "
                         f"or `factory broker pin {tip_sha}` ahead of time"),
               "entry": entry}

    return {"ok": True, "entry": entry}


# -- content derivation: operator TRUTH, never envelope claims (CRITICAL-1a) ----------------
def derive_publish_content(bare_path: str, base_sha: str, tip_sha: str, *,
                           runner: Callable = subprocess.run) -> dict:
    """What `base_sha..tip_sha` ACTUALLY contains, read directly from the bare repo's own
    git objects — never from the envelope's own (factory-supplied, unverified) claims.
    This is what the interactive confirm shows the operator."""
    log = runner(["git", "-C", bare_path, "log", "--oneline", f"{base_sha}..{tip_sha}"],
                capture_output=True, text=True, timeout=30)
    diffstat = runner(["git", "-C", bare_path, "diff", "--stat", f"{base_sha}..{tip_sha}"],
                      capture_output=True, text=True, timeout=30)
    names = runner(["git", "-C", bare_path, "diff", "--name-only", f"{base_sha}..{tip_sha}"],
                   capture_output=True, text=True, timeout=30)
    return {
        "commits": (getattr(log, "stdout", "") or "").strip(),
        "diffstat": (getattr(diffstat, "stdout", "") or "").strip(),
        "changed_paths": [p for p in (getattr(names, "stdout", "") or "").splitlines() if p],
    }


def render_confirmation(env: dict, content: dict) -> str:
    """The exact text shown before an interactive publish — operator-derived facts marked
    as such, envelope-supplied fields explicitly labeled as unverified claims."""
    lines = [
        f"Envelope {(env.get('nonce') or '')[:12]} REQUESTS: {env.get('action')} "
        f"{env.get('repo_slug')}#{env.get('base_branch')}",
        f"  base_sha={(env.get('base_sha') or '')[:12]}  "
        f"tip_sha={(env.get('tip_sha') or '')[:12]}",
        f"  envelope CLAIMS {env.get('n_commits')} commit(s) over {env.get('range', '')} "
        f"(display-only — a factory-supplied claim, NOT trusted)",
        "  operator-DERIVED commits (read directly from the bare repo):",
    ]
    for ln in (content.get("commits") or "(none)").splitlines():
        lines.append(f"    {ln}")
    lines.append("  operator-DERIVED diffstat:")
    for ln in (content.get("diffstat") or "(no diff)").splitlines():
        lines.append(f"    {ln}")
    return "\n".join(lines)


def _default_confirm(prompt_text: str) -> bool:
    print(prompt_text)
    try:
        ans = input("Pin and publish this EXACT content? [y/N] ")
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


# -- issue actions: validated, never reinterpreted (IMPORTANT-4) ---------------------------
def _valid_issue_number(n) -> Optional[int]:
    if isinstance(n, bool):            # bool is an int subclass — exclude explicitly
        return None
    if isinstance(n, int):
        return n if n > 0 else None
    if isinstance(n, str) and n.isdigit():
        v = int(n)
        return v if v > 0 else None
    return None


def _execute_issue_action(repo: str, action: dict, *, runner: Callable) -> dict:
    """Execute exactly what the envelope previewed — the body text was rendered by the
    factory at prepare time (`reporting.issue_sync._issue_actions_from_sync`); the broker
    never re-derives it. `number`/`op` are validated BEFORE ever touching argv — an
    invalid action is dropped and reported, never coerced/reinterpreted (IMPORTANT-4). One
    action's `gh` failure must not abort the others."""
    op = action.get("op")
    number = _valid_issue_number(action.get("number"))
    if op not in _VALID_ISSUE_OPS or number is None:
        return {"number": action.get("number"), "op": action.get("op"), "ok": False,
               "detail": "invalid issue action (bad op/number) — dropped, never sent to gh"}
    body = action.get("body", "")
    if not isinstance(body, str):
        return {"number": number, "op": op, "ok": False,
               "detail": "invalid issue action (non-string body) — dropped"}
    try:
        # `--` ends flag parsing for the trailing positional: even if `str(number)`
        # somehow contained a leading '-' (it cannot — _valid_issue_number only accepts
        # digit strings/positive ints), gh would still be unable to read it as a flag.
        out = runner(["gh", "issue", "comment", "-R", repo, "--body", body, "--", str(number)],
                    capture_output=True, text=True, timeout=30)
        if getattr(out, "returncode", 1) != 0:
            raise RuntimeError((out.stderr or "gh issue comment failed").strip())
        if op == "close":
            cl = runner(["gh", "issue", "close", "-R", repo, "--", str(number)],
                       capture_output=True, text=True, timeout=30)
            if getattr(cl, "returncode", 1) != 0:
                raise RuntimeError((cl.stderr or "gh issue close failed").strip())
        return {"number": number, "op": op, "ok": True}
    except Exception as e:  # noqa: BLE001 — one issue's gh failure must not abort the rest
        return {"number": number, "op": op, "ok": False, "detail": str(e)[:200]}


def execute_envelope(env: dict, entry: dict, *, runner: Callable = subprocess.run) -> dict:
    """Push `tip_sha` to the REAL remote (the operator's own git credential, via `runner`
    — never anything the factory user could have supplied) then run the envelope's own
    issue actions (capped at MAX_ISSUE_ACTIONS; anything beyond is dropped and reported,
    never silently truncated without a trace). Returns `{'ok': True, 'sha', 'detail',
    'issue_results'}` or `{'ok': False, 'detail': str}` on a push failure (no issue
    actions run)."""
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
    dropped_excess = max(0, len(actions) - MAX_ISSUE_ACTIONS)
    actions = actions[:MAX_ISSUE_ACTIONS]
    if actions and entry.get("allow_issue_ops", True):
        repo = env.get("repo_slug", "")
        issue_results = [_execute_issue_action(repo, act, runner=runner) for act in actions]
    elif actions:
        issue_results = [{"number": a.get("number"), "op": a.get("op"), "ok": False,
                          "detail": "issue ops not allowed for this allowlist entry"}
                         for a in actions]
    if dropped_excess:
        issue_results.append({"number": None, "op": None, "ok": False,
                              "detail": f"{dropped_excess} issue action(s) dropped "
                                        f"(exceeds MAX_ISSUE_ACTIONS={MAX_ISSUE_ACTIONS})"})
    return {"ok": True, "sha": tip_sha,
           "detail": f"pushed {tip_sha[:9]} to {remote_url}#{base_branch}",
           "issue_results": issue_results}


def _archive_processed(json_path: str, hash_path: str, processed_dir: str) -> None:
    """Move a FINALIZED envelope+hash pair OUT of the shared/factory-writable outbox into
    the operator-owned processed_dir (IMPORTANT-2c) — the old outbox/done sat inside the
    SAME factory-writable tree as outbox itself, so the very actor the replay guard exists
    to stop could re-drop an archived envelope right back into outbox/. The real replay
    guard is the spent ledger (operator-owned); this is just tidiness + a forensic trail,
    now somewhere that tidiness can't be weaponized."""
    os.makedirs(processed_dir, exist_ok=True)
    for p in (json_path, hash_path):
        if os.path.isfile(p):
            os.replace(p, os.path.join(processed_dir, os.path.basename(p)))
    try:
        os.chmod(processed_dir, 0o700)
    except OSError:  # noqa: BLE001
        pass


def _finalize(nonce: str, status: str, *, detail: str, receipt_sha: str, policy: str,
             receipts_dir: str, spent_path: str, json_path: str, hash_path: str,
             processed_dir: str) -> None:
    """Common tail for every TERMINAL outcome (pushed/rejected/expired/declined): mark the
    ledger (the real authority), write the informational spool-receipt copy (factory
    reads this), archive the envelope out of factory-writable space. NEVER called for the
    'pending' (unattended + unpinned) outcome — that one is deliberately left retryable."""
    mark_spent(spent_path, nonce, status=status)
    envelope_mod.write_receipt(nonce=nonce, status=status, receipts_dir=receipts_dir,
                               receipt_sha=receipt_sha, detail=detail, policy_hash=policy)
    _archive_processed(json_path, hash_path, processed_dir)


def run_once(*, outbox_dir: str, receipts_dir: str, allowlist_path: str, spent_path: str,
            pins_path: str, processed_dir: str, unattended: bool = False,
            confirm_fn: Optional[Callable[[str], bool]] = None,
            runner: Callable = subprocess.run) -> list[dict]:
    """Process every envelope currently in `outbox_dir`.

    For each: verify (allowlist authorizes the destination, live git re-verifies
    liveness, the operator ledger checks it isn't a replay). Three outcomes:

    - VERIFIED + PINNED (or `require_pin: false`) => execute for real; a terminal
      'pushed'/'rejected' receipt, ledgered as spent, envelope archived.
    - VERIFIED but UNPINNED, `unattended=True` => a soft 'pending' outcome: NOT ledgered,
      NOT archived — left in the outbox so a later `factory broker pin <sha>` followed by
      a later run (interactive OR unattended) can still complete it.
    - VERIFIED but UNPINNED, `unattended=False` (the DEFAULT): render the operator-derived
      diff (`derive_publish_content`/`render_confirmation`) and prompt (`confirm_fn`,
      default a real terminal `input()`). Confirmed => pin the tip, re-verify (now passes
      the pin gate), execute. Declined => terminal 'rejected' ("operator declined"),
      ledgered as spent (the operator said no to THIS envelope; a genuine change of mind
      needs a fresh approval/envelope, not a silent retry loop).
    - Any other verification failure (tamper/expiry/replay/base-moved/etc.) => terminal,
      ledgered, archived, exactly as before.

    Returns one `{'nonce', 'status'}` (status includes 'pending' for the soft outcome) per
    envelope inspected."""
    allowlist = load_allowlist(allowlist_path)
    confirm_fn = confirm_fn or _default_confirm
    results = []
    for nonce in envelope_mod.list_outbox(outbox_dir):
        json_path = os.path.join(outbox_dir, f"{nonce}.json")
        hash_path = f"{json_path}.sha256"
        env = envelope_mod.read_envelope(json_path)
        policy = (env or {}).get("policy_hash", "")

        def _finalize_here(status: str, detail: str, receipt_sha: str = "") -> None:
            _finalize(nonce, status, detail=detail, receipt_sha=receipt_sha, policy=policy,
                     receipts_dir=receipts_dir, spent_path=spent_path,
                     json_path=json_path, hash_path=hash_path, processed_dir=processed_dir)

        verdict = verify_envelope(env, filename_nonce=nonce, hash_path=hash_path,
                                  allowlist=allowlist, spent_path=spent_path,
                                  pins_path=pins_path, runner=runner)
        if verdict["ok"]:
            outcome = execute_envelope(env, verdict["entry"], runner=runner)
            status = "pushed" if outcome["ok"] else "rejected"
            _finalize_here(status, outcome.get("detail", ""), outcome.get("sha", ""))
            results.append({"nonce": nonce, "status": status})
            continue

        if verdict.get("status") == "unpinned":
            if unattended:
                results.append({"nonce": nonce, "status": "pending",
                                "reason": verdict.get("reason", "")})
                continue
            entry = verdict["entry"]
            content = derive_publish_content(entry["bare_path"], env.get("base_sha", ""),
                                             env.get("tip_sha", ""), runner=runner)
            text = render_confirmation(env, content)
            if confirm_fn(text):
                pin_tip(pins_path, env["tip_sha"],
                       note=f"confirmed via broker run-once ({nonce[:8]})")
                verdict2 = verify_envelope(env, filename_nonce=nonce, hash_path=hash_path,
                                           allowlist=allowlist, spent_path=spent_path,
                                           pins_path=pins_path, runner=runner)
                if verdict2["ok"]:
                    outcome = execute_envelope(env, verdict2["entry"], runner=runner)
                    status = "pushed" if outcome["ok"] else "rejected"
                    _finalize_here(status, outcome.get("detail", ""), outcome.get("sha", ""))
                else:
                    status = verdict2.get("status", "rejected")
                    _finalize_here(status, verdict2.get("reason", ""))
            else:
                status = "rejected"
                _finalize_here(status, "operator declined")
            results.append({"nonce": nonce, "status": status})
            continue

        status = verdict.get("status", "rejected")
        _finalize_here(status, verdict.get("reason", ""))
        results.append({"nonce": nonce, "status": status})
    return results


def watch(*, outbox_dir: str, receipts_dir: str, allowlist_path: str, spent_path: str,
         pins_path: str, processed_dir: str, unattended: bool = False,
         runner: Callable = subprocess.run, poll_s: float = 5.0,
         max_iters: Optional[int] = None, sleep_fn: Callable = time.sleep) -> int:
    """Poll loop: `run_once` every `poll_s` seconds. CRITICAL-1c: a persistent background
    loop cannot prompt a human, so this REFUSES to run at all unless `unattended=True` is
    passed explicitly — `require_pin` (default True, per allowlist entry) stays fully in
    force regardless; unattended + unpinned is a 'pending' outcome (never auto-approved,
    never silently published). The LaunchAgent (Component G) always passes
    `unattended=True`, mirrored by `--unattended` on the CLI. Returns the number of
    iterations run (0 if refused — a test/caller seam)."""
    if not unattended:
        raise ValueError(
            "watch() requires unattended=True — a persistent poll loop can't prompt "
            "interactively; pin tips ahead of time with `factory broker pin <sha>`, or "
            "run `factory broker run-once` by hand to review + confirm")
    n = 0
    while max_iters is None or n < max_iters:
        run_once(outbox_dir=outbox_dir, receipts_dir=receipts_dir,
                 allowlist_path=allowlist_path, spent_path=spent_path, pins_path=pins_path,
                 processed_dir=processed_dir, unattended=True, runner=runner)
        n += 1
        if max_iters is None or n < max_iters:
            sleep_fn(poll_s)
    return n


def status(*, outbox_dir: str, receipts_dir: str) -> dict:
    """A cheap snapshot for `factory broker status` — no allowlist load, no git/gh calls."""
    return {"pending": envelope_mod.list_outbox(outbox_dir),
           "receipts": envelope_mod.list_receipts(receipts_dir)}
