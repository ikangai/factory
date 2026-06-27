# Auto issue-sync: the factory updates the target's GitHub issues on graduation

**Date:** 2026-06-27
**Status:** Approved design, pre-implementation

## Problem

When the factory graduates work to `base` and pushes it to the target repo's
`origin`, the resolved/advanced GitHub issues are not updated. A human has to
notice "commit X referenced #N" and comment/close by hand. This was done
manually on 2026-06-27 for the resilience graduation batch:

- Graduated `chore/extract-factory` → `factory/auto` (resolved GRAD #3, 12 commits)
  and pushed 31 commits to `github.com/ikangai/clive`.
- Commented progress on #40 and #41 — *deliberately not closed*, because both are
  multi-phase epics (#41's commit literally says "slice 1/2"; #40 is a whole
  L2/L3/L5 eval framework). Auto-closing either would have been wrong.

The factory should do this updating automatically.

## Key decisions (brainstormed 2026-06-27)

1. **Close policy — keyword-gated.** Comment on any issue a graduated commit
   references with a bare `#N` / `gh#N`. Auto-close *only* when a commit uses
   GitHub's keyword (`closes`/`fixes`/`resolves #N`). The developer super-worker
   decides per commit; epics referenced with a bare `#N` are never surprise-closed.
2. **Trigger — idempotent sync at push time.** One `factory issue sync` step that
   scans commits newly landed on `origin` and comments/closes accordingly, called
   automatically after the graduation push. Safe to re-run (won't double-post).
3. **Scope — full autonomy in the autopilot loop.** Graduate + push + sync run
   unattended from `cmd_run`, no human trigger. This *escalates* the previously
   human-gated graduation, so it ships with fail-safe rails (below). A manual
   `factory graduate` command also exists for testability and operator use.

### Why keyword-gated, specifically for this system

The factory's own researcher plans work from **open** issues
(`roles/research_feed.fetch_issues` → the proposer). Auto-closing an epic the
moment one slice ships would remove it from `fetch_issues`, so the factory would
**stop generating the remaining slices of its own work**. Keyword-gating keeps
unfinished epics open and in the planning loop.

## Architecture

### New module `reporting/issue_sync.py` (pure, injected I/O)

- `parse_issue_refs(message) -> {"closes": set[int], "mentions": set[int]}`
  Regex: `(?i)(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:gh)?#(\d+)` → `closes`;
  remaining bare `#(\d+)` / `gh#(\d+)` → `mentions`. A number in `closes` is never
  also counted as a mention.
- `plan_sync(commits) -> {issue_number: {"action": "close"|"comment", "commits": [...]}}`
  Groups commits by referenced issue. **close wins**: if any commit closes an
  issue, the action is `close`, else `comment`.
- `sync_issues(repo, commits, *, store, runner=subprocess.run, dry_run=False) -> list[dict]`
  For each issue: filter out `(issue, commit_sha)` pairs already in the store; if
  nothing new, skip. Otherwise post ONE comment summarizing the new commits, and
  if `action == "close"` close the issue. Record each `(issue, commit_sha, action,
  url)` in the store. `runner` is injected so tests never shell out.

A `commit` is a small dict: `{"sha", "subject", "body"}`.

### Store: new table `issue_sync`

```sql
CREATE TABLE IF NOT EXISTS issue_sync (
  issue_number INTEGER NOT NULL,
  commit_sha   TEXT    NOT NULL,
  action       TEXT    NOT NULL,         -- 'comment' | 'close'
  url          TEXT,                     -- comment/issue URL returned by gh
  created_at   TEXT    NOT NULL,
  PRIMARY KEY (issue_number, commit_sha)
);
```

Idempotency lives here (not comment-scraping). `store.issue_sync_seen(issue, sha)`
+ `store.record_issue_sync(...)`.

### Graduation mechanics — `reporting/issue_sync.graduate_and_push(...)` (or a thin orchestrator fn)

1. Re-check STOP → present ⇒ skip, return `{"action": "skip", "reason": "stop"}`.
2. `git -C <target> merge --ff-only <factory/auto>` onto `<base>`. Not a
   fast-forward ⇒ log + skip (NEVER force).
3. Capture `origin/<base>` sha (the pre-push tip).
4. `git -C <target> push origin <base>` (no `--force`). Rejected ⇒ skip sync.
5. Range = `<old origin sha>..<new base sha>`; read commits via
   `git log --format=%H%x00%s%x00%b`; call `sync_issues`.

Base branch + repo + root from config: `target.base_branch`
(default `chore/extract-factory`), `target.repo`, `target.root`.

### Wiring into the loop

In `cmd_run`, after `run_shift` returns with `shipped > 0` and `real` is true,
call `graduate_and_push`. Wrapped in try/except → log + continue. The loop's
existing STOP / deadline / token-budget brakes are unchanged.

## Safety rails (every step fails closed)

- **ff-only, never force**; non-fast-forward ⇒ skip.
- **push without `--force`**; rejected push ⇒ no sync (never comment about work not
  on origin).
- **STOP-aware** — re-checked immediately before graduate/push.
- **real-mode + advanced-only** — no-op shifts never push.
- **never crashes the loop** — full try/except; a `gh`/GitHub outage degrades to
  "pushed, issues unsynced".
- **idempotent** — store-tracked `(issue, commit)`; resume/re-run never double-posts.
- **dry-run** flag for tests and a `factory issue sync --dry-run` preview.

## Testing (TDD, injected `runner`/`now` — no real git/gh/network)

- `parse_issue_refs`: keyword variants, `gh#N`, bare `#N`, multiple refs, none,
  a number that is both closed and mentioned (closes wins).
- `plan_sync`: grouping across commits; close-wins-over-comment.
- `sync_issues`: idempotency (2nd run no-ops), keyword-gated close only,
  comment-body format, dry-run posts nothing, records to store.
- `graduate_and_push`: ff success, non-ff ⇒ skip + no force, push-range capture,
  push-fail ⇒ no sync, STOP ⇒ skip.
- Loop integration: `cmd_run` calls graduate when `shipped>0 ∧ real`;
  STOP / non-real skip it.

## Out of scope (YAGNI)

- Cross-repo issue refs (`owner/repo#N`) — target repo only.
- Reopening issues — keyword-gating already avoids wrong closes.
- Editing/upserting a single rolling comment — one comment per sync batch is fine.
