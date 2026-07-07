"""Auto issue-sync: after the factory graduates work to base and pushes it to the
target's origin, comment on / close the GitHub issues those commits reference.

Keyword-gated close: a bare `#N` / `gh#N` reference earns a progress *comment*; a
GitHub keyword (`closes` / `fixes` / `resolves #N`) *closes* the issue. Epics
referenced with a bare `#N` stay open, so the researcher's open-issue planning
loop (`roles.research_feed.fetch_issues`) keeps generating their remaining slices.

All git/gh I/O goes through an injected `runner` (default `subprocess.run`) so the
logic is testable without a real repo or network. Idempotency is store-tracked
(the `issue_sync` table): a (issue, commit) pair already synced is never re-posted.

Design: docs/plans/2026-06-27-factory-auto-issue-sync-design.md
"""
from __future__ import annotations

import re
import subprocess

# A close keyword immediately followed by an (optionally gh-prefixed) issue ref.
# \b anchors the keyword to a word start so 'prefix #9' does not read as 'fix #9'.
_CLOSE_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:gh)?#(\d+)")
# Any issue reference at all (gh# or #).
_REF_RE = re.compile(r"(?:gh)?#(\d+)")


def parse_issue_refs(message: str) -> dict:
    """Split a commit message's issue references into {'closes', 'mentions'} (sets of
    ints). A number that appears in a close keyword is a close, never also a mention."""
    message = message or ""
    closes = {int(n) for n in _CLOSE_RE.findall(message)}
    mentions = {int(n) for n in _REF_RE.findall(message)} - closes
    return {"closes": closes, "mentions": mentions}


def _commit_text(commit: dict) -> str:
    return f"{commit.get('subject', '')}\n{commit.get('body', '')}"


def plan_sync(commits: list[dict]) -> dict:
    """Group commits by the issue they reference. Returns {issue_number: {'action':
    'comment'|'close', 'commits': [commit, ...]}}. Action is 'close' if ANY commit
    closed the issue with a keyword (close wins, order-independent), else 'comment'.
    Commit order within an issue follows input order."""
    plan: dict = {}
    for c in commits:
        refs = parse_issue_refs(_commit_text(c))
        for n in sorted(refs["closes"] | refs["mentions"]):
            entry = plan.setdefault(n, {"action": "comment", "commits": []})
            entry["commits"].append(c)
            if n in refs["closes"]:
                entry["action"] = "close"
    return plan


def _format_comment(issue: int, commits: list[dict], action: str) -> str:
    """A progress comment listing the graduated commits that referenced this issue."""
    bullets = "\n".join(
        f"- `{c.get('sha', '')[:9]}` {c.get('subject', '')}" for c in commits)
    head = ("**Resolved by the autonomous factory** (graduated to base, pushed):"
            if action == "close" else
            "**Progress from the autonomous factory** (graduated to base, pushed):")
    foot = ("" if action == "close" else
            "\n\nReferenced with a bare `#%d`, so leaving this open — close it from a "
            "commit with `closes #%d` when the last slice lands." % (issue, issue))
    return f"{head}\n\n{bullets}{foot}"


def sync_issues(repo: str, commits: list[dict], *, store, runner=subprocess.run,
                dry_run: bool = False) -> list[dict]:
    """Comment on / close the target-repo issues referenced by `commits`.

    One comment per issue summarizing only its NOT-yet-synced commits; the issue is
    closed only when a commit closed it with a keyword (close wins). Idempotent via
    `store` — a (issue, commit) pair already recorded is skipped. `runner(argv, …)`
    runs `gh`; injected for tests. `dry_run` posts and records nothing.

    Returns a per-issue result list: action is one of comment/close/skip/error.
    """
    results: list[dict] = []
    plan = plan_sync(commits)
    for issue in sorted(plan):
        entry = plan[issue]
        action = entry["action"]
        fresh = [c for c in entry["commits"] if not store.issue_sync_seen(issue, c["sha"])]
        if not fresh:
            results.append({"issue": issue, "action": "skip", "commits": []})
            continue
        shas = [c["sha"] for c in fresh]
        if dry_run:
            results.append({"issue": issue, "action": action, "commits": shas,
                            "url": "", "dry_run": True})
            continue
        try:
            body = _format_comment(issue, fresh, action)
            out = runner(["gh", "issue", "comment", str(issue), "-R", repo,
                          "--body", body], capture_output=True, text=True, timeout=30)
            if out.returncode != 0:
                raise RuntimeError((out.stderr or "gh issue comment failed").strip())
            url = (out.stdout or "").strip()
            if action == "close":
                cl = runner(["gh", "issue", "close", str(issue), "-R", repo],
                            capture_output=True, text=True, timeout=30)
                if cl.returncode != 0:
                    raise RuntimeError((cl.stderr or "gh issue close failed").strip())
        except Exception as e:  # noqa: BLE001 — one issue's gh failure must not abort the rest
            results.append({"issue": issue, "action": "error", "commits": shas,
                            "error": str(e)})
            continue
        for sha in shas:                       # record ONLY after gh succeeded → a failure retries
            store.record_issue_sync(issue, sha, action, url)
        results.append({"issue": issue, "action": action, "commits": shas, "url": url})
    return results


def commits_in_range(root: str, rng: str, *, runner=subprocess.run) -> list[dict]:
    """Parse `git log <rng>` (oldest-first) into [{sha, subject, body}, …]. Fields are
    unit-separated and records record-separated so multiline bodies survive. A failed
    git call yields []."""
    out = runner(["git", "-C", root, "log", "--reverse",
                  "--format=%H%x1f%s%x1f%b%x1e", rng],
                 capture_output=True, text=True, timeout=30)
    if getattr(out, "returncode", 1) != 0:
        return []
    commits: list[dict] = []
    for rec in (out.stdout or "").split("\x1e"):
        rec = rec.lstrip("\n")                  # drop the inter-record newline git emits
        if not rec.strip():
            continue
        parts = rec.split("\x1f")
        commits.append({
            "sha": parts[0].strip(),
            "subject": parts[1] if len(parts) > 1 else "",
            "body": (parts[2] if len(parts) > 2 else "").rstrip("\n"),
        })
    return commits


def graduate_and_push(*, root: str, base: str, repo: str, store,
                      auto_branch: str = "factory/auto", remote: str = "origin",
                      runner=subprocess.run, stop_check=None,
                      dry_run: bool = False) -> dict:
    """Graduate (ff `base` → `auto_branch`), push `base` to `remote`, then sync the
    issues referenced by the newly-pushed commits. Fails CLOSED at every step — skips
    (never forces) when STOP is set, the repo isn't on `base`, the merge isn't a
    fast-forward, or the push is rejected. `dry_run` mutates nothing and previews the
    `remote/base..auto_branch` range. git+gh go through `runner` (injected for tests)."""
    if stop_check and stop_check():
        return {"action": "skip", "reason": "stop"}

    def git(*args):
        return runner(["git", "-C", root, *args], capture_output=True, text=True, timeout=60)

    if dry_run:
        rng = f"{remote}/{base}..{auto_branch}"
        commits = commits_in_range(root, rng, runner=runner)
        synced = sync_issues(repo, commits, store=store, runner=runner, dry_run=True)
        return {"action": "dry_run", "range": rng, "n_commits": len(commits), "synced": synced}

    cur = git("rev-parse", "--abbrev-ref", "HEAD")
    if getattr(cur, "returncode", 1) != 0 or (cur.stdout or "").strip() != base:
        return {"action": "skip", "reason": "not-on-base"}

    ff = git("merge", "--ff-only", auto_branch)     # ff-only: a divergence fails, never forces
    if getattr(ff, "returncode", 1) != 0:
        return {"action": "skip", "reason": "not-fast-forward"}

    old = git("rev-parse", f"{remote}/{base}")      # pre-push tip → the sync range floor
    old_sha = (old.stdout or "").strip() if getattr(old, "returncode", 1) == 0 else ""
    if not old_sha:
        return {"action": "skip", "reason": "no-remote-ref"}

    # No-op guard (Theme 4): never push (or keyword-close an issue for) a change that is empty
    # once whitespace is ignored. The merge gate only rejects a fully-empty diff, so a
    # whitespace-only "fix" could otherwise reach production and auto-close a real issue.
    # `git diff -w --quiet` exits 0 iff there is NO non-whitespace change in the pushed range;
    # only that certain-no-op case skips — any other exit (has-diff, or a diff error) proceeds.
    noop = git("diff", "-w", "--quiet", old_sha, "HEAD")
    if getattr(noop, "returncode", 1) == 0:
        return {"action": "skip", "reason": "no-op"}

    push = git("push", remote, base)                # plain push: a rejected push fails, never forces
    if getattr(push, "returncode", 1) != 0:
        return {"action": "skip", "reason": "push-failed"}

    new_sha = (git("rev-parse", "HEAD").stdout or "").strip()
    rng = f"{old_sha}..{new_sha}"
    commits = commits_in_range(root, rng, runner=runner)
    synced = sync_issues(repo, commits, store=store, runner=runner)
    return {"action": "synced", "range": rng, "n_commits": len(commits), "synced": synced}
