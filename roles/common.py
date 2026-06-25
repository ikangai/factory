"""Role engine (spec §5). All roles are stateless `claude -p` workers: each
invocation assembles a context slice from the store, calls `claude -p` with the
role's prompt file, and writes the result back. No role retains state.

Blindness is structural: the proposer's context slice is assembled here and
deliberately excludes grader internals (checks/) and held-out scenarios. For the
isolated transport this is enforced by capability removal (`--tools ""`). For a
super-worker it is enforced instead by CONFINEMENT — the default Bash-less toolset has
no shell and its file tools are limited to a /tmp sandbox that contains neither the
grader nor held-out — so blindness survives only as long as Bash/web stay off (else it
degrades to prompt-only; see the super-worker transport note below).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from typing import Optional

import yaml

from . import check_validate
from ..common import config, paths, scoring, specs
from ..common.store import Blackboard


# ---------------------------------------------------------------------------
# claude -p transport (ISOLATED — see tests/test_factory_role_isolation.py)
# ---------------------------------------------------------------------------
# With --strict-mcp-config this loads ZERO MCP servers.
_EMPTY_MCP_CONFIG = '{"mcpServers": {}}'


def _isolated_claude_argv(json_output: bool = True) -> list[str]:
    """Argv for an ISOLATED `claude -p` role call. Mirrors
    llm._build_claude_cli_argv (keep the two in sync). A role is bounded reasoning
    over a context slice — never a Claude Code agent — so it must load no plugins
    or hooks (no group-chat ghost, no 600s team barrier), no MCP, and no tools:

      --setting-sources ""   drop user+project settings → `enabledPlugins` never
                             read → no plugins/hooks. Keeps subscription/keychain
                             auth (unlike --bare, which would disable it).
      --tools ""             zero tools (text/JSON only)
      --strict-mcp-config    ignore all ambient MCP config...
      --mcp-config {…}       ...and load an empty server set
    """
    argv = ["claude", "-p"]
    if json_output:
        argv += ["--output-format", "json"]
    argv += ["--setting-sources", "", "--tools", "",
             "--strict-mcp-config", "--mcp-config", _EMPTY_MCP_CONFIG]
    return argv


def claude_p(prompt: str, *, timeout: int = 180) -> tuple[str, int, float]:
    """Call `claude -p` (print mode, ISOLATED), prompt on stdin. Returns
    (text, tokens, cost). Tries JSON output for usage; falls back to plain text.
    Auth is the operator's subscription keychain (roles run under the real HOME)."""
    # Run from a neutral cwd so no project .claude/CLAUDE.md is discovered
    # (defense in depth alongside --setting-sources "").
    neutral_cwd = tempfile.gettempdir()
    try:
        p = subprocess.run(_isolated_claude_argv(json_output=True),
                           input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=neutral_cwd)
        if p.returncode == 0 and p.stdout.strip():
            try:
                obj = json.loads(p.stdout)
                text = obj.get("result", "") or obj.get("text", "")
                usage = obj.get("usage", {}) or {}
                tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
                cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
                if text:
                    return text, tokens, cost
            except json.JSONDecodeError:
                return p.stdout, 0, 0.0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"[claude -p unavailable: {e}]", 0, 0.0
    # fallback: plain text (still isolated)
    try:
        p = subprocess.run(_isolated_claude_argv(json_output=False), input=prompt,
                           capture_output=True, text=True, timeout=timeout, cwd=neutral_cwd)
        return p.stdout, 0, 0.0
    except Exception as e:
        return f"[claude -p failed: {e}]", 0, 0.0


# ---------------------------------------------------------------------------
# Super-worker transport — a FULL-CAPABILITY `claude -p` in a SOFT-bounded sandbox.
# ---------------------------------------------------------------------------
# The INVERSE of _isolated_claude_argv: instead of stripping the tools, it ENABLES a
# curated set (incl. Task/Workflow for internal fan-out) under acceptEdits, with file
# tools steered into a disposable per-worker workspace (--add-dir + cwd) and a
# deny-by-default child env. Plugins/hooks stay dropped (--setting-sources "") so a
# headless worker can't hang on a team barrier, and --max-turns bounds it.
#
# BOUNDARY HONESTY (cf. envs/local_sandbox.py): the DEFAULT toolset withholds Bash, so
# the agent has no shell to escape the workspace or read host files/creds — the file
# tools (Read/Write/Edit) are confined by cwd + --add-dir to the sandbox, which holds
# no held-out scenarios and no credentials. That is a reasonable SOFT boundary. It is
# NOT a hard jail: --add-dir does not confine Bash, and enabling Bash/web (or any tool
# that shells out) reopens host-file + network reach. For those, run the worker under a
# HARD boundary — the docker env (envs/docker_env.py, --network none) — which is not yet
# wired for super-workers. So: keep Bash off unless/until docker backs it.
import contextlib
import shutil

# Curated default toolset: fan out (Workflow/Task) + read/write/grep scratch — but NO
# Bash and NO web. Bash escapes --add-dir and web is an exfil surface; both require the
# docker hard boundary and must be opted in per role, never defaulted on.
DEFAULT_SUPER_TOOLS = ("Read", "Write", "Edit", "Grep", "Glob", "Task", "Workflow")
# A DEVELOPER super-worker runs in the Guest House (a separate OS user, via `as_user`),
# so the OS boundary protects the operator and Bash is SAFE to grant — the worker needs
# it to run the target's tests, edit code, and git-commit a candidate branch.
DEVELOPER_TOOLS = ("Read", "Write", "Edit", "Bash", "Grep", "Glob", "Task", "Workflow")

# Deny-by-default child env: only claude's runtime + auth/config families pass through,
# so host secrets in the environment (known AND unknown) never reach the worker.
_SUPER_ENV_ALLOW = {
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "PWD", "TERM", "COLORTERM",
    "LANG", "LANGUAGE", "TZ", "TMPDIR", "TMP", "TEMP",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
}
_SUPER_ENV_ALLOW_PREFIX = ("ANTHROPIC_", "CLAUDE_", "XDG_", "LC_")


def _super_worker_env() -> dict:
    """Build the child env from an explicit allowlist (deny-by-default), preserving
    claude's runtime + subscription auth while dropping every other host secret."""
    return {k: v for k, v in os.environ.items()
            if k in _SUPER_ENV_ALLOW or any(k.startswith(p) for p in _SUPER_ENV_ALLOW_PREFIX)}


def _super_worker_argv(workdir: str, allowed_tools, *, max_turns: int = 24,
                       as_user: Optional[str] = None, json_output: bool = True) -> list[str]:
    argv: list[str] = []
    if as_user:
        # Run the worker as the Guest-House Standard User — OS-enforced isolation of the
        # operator's files/creds (docs/plans/2026-06-25). Needs a passwordless
        # `sudo -u <user>` grant for the claude command. With this HARD boundary, Bash
        # in the toolset is safe.
        argv += ["sudo", "-u", as_user, "--"]
    argv += ["claude", "-p"]
    if json_output:
        argv += ["--output-format", "json"]
    argv += ["--setting-sources", "",            # no plugins/hooks → no team-barrier hang
             "--permission-mode", "acceptEdits",  # act without approval prompts…
             "--add-dir", workdir,                # …file tools steered into the workspace
             "--max-turns", str(max_turns),       # bound the agentic loop (not just wall-clock)
             "--allowedTools", *list(allowed_tools)]
    return argv


@contextlib.contextmanager
def super_worker_workspace(prefix: str = "role"):
    """A disposable workspace for ONE super-worker — a fresh tempdir under /tmp. Two
    parallel workers get distinct dirs, and with the default (Bash-less) toolset their
    file tools can't reach outside it, so their work doesn't collide. Auto-removed."""
    root = tempfile.mkdtemp(prefix=f"sw-{prefix}-", dir="/tmp")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def claude_super(prompt: str, *, workdir: str, allowed_tools=DEFAULT_SUPER_TOOLS,
                 as_user: Optional[str] = None, max_turns: int = 24,
                 timeout: int = 900) -> tuple[str, int, float]:
    """Run a FULL-CAPABILITY `claude -p` super-worker in `workdir` with a curated
    toolset + acceptEdits and a turn cap. Returns (text, tokens, cost). Never crashes —
    a transport error yields the `[claude -p …]` sentinel callers fall back on.

    Boundary: WITHOUT `as_user` it's a SOFT boundary (same OS user; keep Bash/web off, a
    deny-by-default env withholds host secrets). WITH `as_user` it runs as the Guest-
    House Standard User — a HARD, OS-enforced boundary that protects the operator's
    files/creds, so Bash is safe; the target user's own env/`~/.claude` auth is used
    (we don't impose the operator's env)."""
    env = None if as_user else _super_worker_env()
    try:
        p = subprocess.run(_super_worker_argv(workdir, allowed_tools, max_turns=max_turns,
                                              as_user=as_user, json_output=True),
                           input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=workdir, env=env)
        if p.returncode == 0 and p.stdout.strip():
            try:
                obj = json.loads(p.stdout)
                text = obj.get("result", "") or obj.get("text", "")
                usage = obj.get("usage", {}) or {}
                tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
                cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
                if text:
                    return text, tokens, cost
            except json.JSONDecodeError:
                return p.stdout, 0, 0.0
        return f"[claude -p super-worker failed: rc={p.returncode}]", 0, 0.0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"[claude -p unavailable: {e}]", 0, 0.0


def run_super_worker(prompt: str, *, allowed_tools=DEFAULT_SUPER_TOOLS,
                     timeout: int = 900) -> tuple[str, int, float]:
    """Provision a disposable workspace, run one super-worker in it, tear it down.
    The conductor's contract is unchanged: in = prompt, out = (text, tokens, cost)."""
    with super_worker_workspace() as wd:
        return claude_super(prompt, workdir=wd, allowed_tools=allowed_tools, timeout=timeout)


def _load_prompt(role: str) -> str:
    with open(os.path.join(paths.ROLES_DIR, role, "prompt.md"), "r", encoding="utf-8") as fh:
        return fh.read()


def _extract(text: str, langs=("yaml", "json", "")) -> str:
    """Pull a fenced code block (yaml/json) from an LLM reply, else the whole text.
    Uses a GREEDY body match so triple-backticks INSIDE the block (e.g. a ```bash
    mention inside a system_prompt value) don't truncate the extraction at the
    inner fence."""
    for lang in langs:
        m = re.search(rf"```{lang}[^\n]*\n(.*)```", text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return text.strip()


def _first_json_object(text: str) -> str | None:
    """Return the first balanced {...} JSON object in `text`, tracking string state
    so braces/backticks inside string values don't confuse it. Robust to a JSON
    payload whose values contain ``` fences or { } characters."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_obj(reply: str):
    """Best-effort parse of an LLM reply into a dict: balanced-brace JSON first
    (backtick-safe), then a fenced block, then YAML."""
    cand = _first_json_object(reply)
    if cand:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    raw = _extract(reply, ("json", "yaml", ""))
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return yaml.safe_load(raw)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Proposer (generation) — emits ONE bounded change to `open`. Blind.
# ---------------------------------------------------------------------------
def _staged_research_briefs(limit: int = 8) -> list[dict]:
    """GROUNDED staged technique briefs (research/staging) as compact DIRECTION for
    the Proposer — this is the 'research → proposal' arrow.

    Ungrounded briefs (those the researcher flagged with `provenance_warning` — a
    citation NOT among the papers/repos we actually fetched) are EXCLUDED, so the
    proposer is fed only briefs whose source the factory really retrieved. Newest
    first (by mtime), capped. The proposer stays blind to held-out: a public-paper
    technique carries no held-out signal. Best-effort — any error yields [] so the
    proposer never crashes on bookkeeping."""
    import glob

    scored: list[tuple[float, dict]] = []
    try:
        for path in glob.glob(os.path.join(paths.RESEARCH_STAGING_DIR, "*.yaml")):
            # Per-file skip: a single unreadable, deleted-mid-read, or malformed brief
            # (e.g. an operator hand-edit during vetting) must drop only ITSELF, not
            # collapse every valid brief. getmtime shares this try — same FS race.
            try:
                mtime = os.path.getmtime(path)
                with open(path, "r", encoding="utf-8") as fh:
                    b = yaml.safe_load(fh) or {}
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(b, dict) or not b.get("suggested_change"):
                continue
            if b.get("provenance_warning"):   # ungrounded → keep it from the proposer
                continue
            scored.append((mtime, {
                "applies_to": b.get("applies_to"),
                "technique": b.get("technique"),
                "suggested_change": b.get("suggested_change"),
                "rationale": b.get("rationale"),
                "cite": b.get("arxiv_id") or b.get("repo") or b.get("url"),
            }))
    except Exception:  # noqa: BLE001 — direction is best-effort, never fatal
        return []
    scored.sort(key=lambda t: t[0], reverse=True)
    return [b for _, b in scored[:limit]]


def propose(store: Blackboard) -> Optional[str]:
    champ_row = store.get_champion()
    champ_path = champ_row["spec_path"] if champ_row else paths.CHAMPION_YAML
    champ = specs.load_spec(champ_path)

    # Context slice — champion spec (so the proposer knows the rules incl. what is
    # frozen), recent WORKING-set failures, and the history of changes tried.
    # Deliberately NO grader internals, NO held-out scenarios.
    failures = store.recent_failures(limit=12)
    # Held-out-derived signal is redacted from the tried-history: the proposer is
    # blind to the held-out set (scoring.proposer_safe_scores).
    tried = [{"id": c["id"], "change": c["change_summary"], "stage": c["stage"],
              "scores": scoring.proposer_safe_scores(json.loads(c["scores_json"] or "{}"))}
             for c in store.list_candidates()]

    ctx = {
        "champion_open": champ.get("open", {}),
        "champion_frozen_keys": list((champ.get("frozen", {}) or {}).keys()),
        "open_keys_you_may_change": specs.OPEN_KEYS,
        "recent_failures": [
            {"scenario": f.get("scenario_goal", ""), "outcome": f["outcome"],
             "detail": json.loads(f.get("check_json", "{}") or "{}").get("detail", "")}
            for f in failures],
        "changes_already_tried": tried,
        # Grounded literature DIRECTION (the 'research → proposal' arrow). Optional:
        # the recorded failures still take priority; a brief is a hint, not an order.
        "research_briefs": _staged_research_briefs(),
    }
    prompt = _load_prompt("proposer") + "\n\n## CONTEXT (JSON)\n```json\n" \
        + json.dumps(ctx, indent=2, default=str) + "\n```\n"
    if config.is_super_worker("proposer"):
        # Super-worker: a builder-reviewer LOOP that may fan out via /workflows +
        # subagents in its sandbox, then converge — but its FINAL message must still
        # be the strict one-change JSON patch parsed below (the conductor's contract).
        reply, tokens, cost = run_super_worker(
            _load_prompt("super_worker") + "\n\n" + prompt)
    else:
        reply, tokens, cost = claude_p(prompt)
    store.add_budget("proposer", tokens, cost, notes="propose")

    patch = _parse_obj(reply)
    if not isinstance(patch, dict) or "open_key" not in patch or "new_value" not in patch:
        return None
    key = patch["open_key"]
    if key not in specs.OPEN_KEYS:
        return None

    # Construct the candidate from champion + the single field change. Frozen is
    # copied verbatim, so it is structurally impossible to mutate it.
    candidate = {"meta": {"version": (champ.get("meta", {}).get("version", 1)) + 1,
                          "parent": champ_row["id"] if champ_row else "champion"},
                 "open": dict(champ.get("open", {})),
                 "frozen": champ.get("frozen", {})}
    candidate["open"][key] = patch["new_value"]
    candidate["meta"]["hash"] = specs.compute_hash(candidate["open"], candidate["frozen"])

    res = specs.validate_candidate(candidate, champ,
                                   max_changed_open_keys=config.load_config()
                                   .get("spec", {}).get("max_changed_open_keys", 1))
    if not res.ok:
        return None

    cid = f"cand-{uuid.uuid4().hex[:10]}"
    spec_path = os.path.join(paths.CANDIDATES_DIR, f"{cid}.yaml")
    os.makedirs(paths.CANDIDATES_DIR, exist_ok=True)
    specs.dump_spec(candidate, spec_path)
    summary = patch.get("summary") or specs.change_summary(res.diff)
    # Provenance: if this change was grounded in a research brief, record the
    # citation on the candidate so a research-driven proposal is traceable to its
    # source (axis B: every research proposal cites where it came from).
    cite = str(patch.get("cite", "")).strip()
    if cite:
        summary = f"{summary} [cite: {cite}]"
    store.add_candidate(cid, candidate["meta"]["parent"], spec_path,
                        change_summary=summary, diff=res.diff, stage="proposed")
    return cid


# ---------------------------------------------------------------------------
# Judge (measurement, supplementary) — annotates only; never sets pass/fail.
# ---------------------------------------------------------------------------
def judge(store: Blackboard, run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        return {}
    scenario = store.get_scenario(run["scenario_id"]) or {}
    transcript_path = os.path.join(run["evidence_path"], "transcript.txt")
    transcript = ""
    if os.path.exists(transcript_path):
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            transcript = fh.read()[:16000]
    prompt = _load_prompt("judge") + (
        f"\n\n## SCENARIO GOAL\n{scenario.get('goal','')}\n"
        f"\n## DETERMINISTIC OUTCOME (already decided; do not override)\n{run['outcome']}\n"
        f"\n## RUN TRANSCRIPT (truncated)\n```\n{transcript}\n```\n")
    reply, tokens, cost = claude_p(prompt)
    store.add_budget("judge", tokens, cost, notes=run_id)
    flags = _parse_obj(reply)
    if not isinstance(flags, dict):
        flags = {"note": reply[:2000]}
    store.add_judge_note(run_id, flags)
    return flags


# ---------------------------------------------------------------------------
# Reporter (arbitration support) — deterministic signals + prose digest.
# ---------------------------------------------------------------------------
def report(store: Blackboard, candidate_id: str) -> str:
    champ = store.get_champion()
    champ_id = champ["id"] if champ else None
    cfg = config.load_config()
    promo = scoring.evaluate_promotion(store, candidate_id, champ_id, cfg)
    diverg = scoring.divergence_signal(store, candidate_id, champ_id)
    cand = store.get_candidate(candidate_id) or {}
    runs = store.runs_for_candidate(candidate_id)

    facts = {
        "candidate": candidate_id, "change": cand.get("change_summary", ""),
        "promotion_eligibility": promo, "divergence": diverg,
        "run_outcomes": [{"scenario": r["scenario_id"], "model": r["model"],
                          "outcome": r["outcome"], "partition": r["partition"]}
                         for r in runs],
        "safety_flags": store.safety_flags_for_candidate(candidate_id),
    }
    prompt = _load_prompt("reporter") + "\n\n## COMPUTED FACTS (authoritative)\n```json\n" \
        + json.dumps(facts, indent=2, default=str) + "\n```\n"
    reply, tokens, cost = claude_p(prompt)
    store.add_budget("reporter", tokens, cost, notes=candidate_id)

    digest = reply.strip() or "(no digest)"
    digest_path = os.path.join(paths.RUNS_DIR, f"{candidate_id}.digest.md")
    os.makedirs(paths.RUNS_DIR, exist_ok=True)
    with open(digest_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Promotion digest — {candidate_id}\n\n"
                 f"```json\n{json.dumps(facts, indent=2, default=str)}\n```\n\n{digest}\n")
    # store digest reference on the candidate
    store.set_candidate_scores(candidate_id, {**promo["candidate_scores"],
                                              "digest_path": digest_path,
                                              "divergence": diverg})
    return digest_path


# ---------------------------------------------------------------------------
# Scenario Miner (corpus intake) — writes CANDIDATE scenarios to staging only.
# ---------------------------------------------------------------------------
def mine_scenarios(store: Blackboard, limit: int = 10) -> list[str]:
    log_path = os.path.expanduser("~/.clive_session_log.jsonl")
    lines: list[str] = []
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().strip().splitlines()[-limit:]
    prompt = _load_prompt("scenario-miner") + (
        "\n\n## RECENT PRODUCTION SESSIONS (JSONL)\n```\n" + "\n".join(lines) + "\n```\n")
    reply, tokens, cost = claude_p(prompt)
    store.add_budget("scenario-miner", tokens, cost, notes="mine")

    raw = _extract(reply, ("yaml", "json", ""))
    try:
        proposed = yaml.safe_load(raw)
    except Exception:
        return []
    if isinstance(proposed, dict):
        proposed = proposed.get("scenarios", [])
    if not isinstance(proposed, list):
        return []

    written: list[str] = []
    os.makedirs(paths.STAGING_DIR, exist_ok=True)
    for i, sc in enumerate(proposed):
        if not isinstance(sc, dict) or not sc.get("goal"):
            continue
        # Mined scenarios are CANDIDATES for operator vetting. Never enter the
        # corpus, and never the held-out partition, without human approval.
        sc.setdefault("id", f"mined-{uuid.uuid4().hex[:6]}")
        sc["source"] = "mined"
        sc["partition"] = "staging"   # not 'working'/'held-out' — vetting required
        sc["leakage_count"] = 0
        path = os.path.join(paths.STAGING_DIR, f"{sc['id']}.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(sc, fh, sort_keys=False, allow_unicode=True)
        written.append(path)
    return written


def synth_check(store: Blackboard, staged_id: str) -> Optional[str]:
    """Synthesize a deterministic acceptance check (Python) for a STAGED scenario.

    claude -p writes a `def acceptance(ctx) -> CheckResult` module from the
    scenario's goal + seed_files + check description. The result is saved for
    OPERATOR REVIEW and the staged scenario's `check` is pointed at it. The human
    reviews the generated code before promoting the scenario — the check is the
    product, so it is never trusted unread.
    """
    staged = os.path.join(paths.STAGING_DIR, f"{staged_id}.yaml")
    if not os.path.exists(staged):
        return None
    with open(staged, "r", encoding="utf-8") as fh:
        sc = yaml.safe_load(fh) or {}
    ctx = {"goal": sc.get("goal", ""), "seed_files": sc.get("seed_files", {}),
           "check_description": sc.get("check", ""), "class": sc.get("class", "single")}
    prompt = _load_prompt("check-synth") + "\n\n## SCENARIO\n```json\n" \
        + json.dumps(ctx, indent=2, default=str) + "\n```\n"
    reply, tokens, cost = claude_p(prompt)
    if store:
        store.add_budget("check-synth", tokens, cost, notes=staged_id)

    code = _extract(reply, ("python", ""))
    if "def acceptance" not in code:
        return None

    # Deterministic backstop (#64): a synthesized check must not gate the candidate
    # on an LLM-guessed oracle literal. validate_synth_check exercises it against
    # its own recomputed-correct and a perturbed end-state — a check that fails its
    # own correct answer (the literal-first ordering bug) is REJECTED here, before
    # the human gate, rather than silently steering the proposer toward a wrong
    # answer. Unverifiable checks (shell-based, no evidence['expected']) pass.
    ok, reason = check_validate.validate_synth_check(code, sc)

    rel = f"checks/scenarios/{staged_id.replace('-', '_')}_check.py"
    path = os.path.join(paths.FACTORY_ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(code if code.endswith("\n") else code + "\n")

    if not ok:
        # Keep the file for human inspection but do NOT adopt it as the scenario's
        # check — a wrong oracle is worse than no check (it poisons the proposer).
        sc.pop("check", None)
        sc["check_synth_rejected"] = reason
        with open(staged, "w", encoding="utf-8") as fh:
            yaml.safe_dump(sc, fh, sort_keys=False, allow_unicode=True)
        print(f"[check-synth] {staged_id}: generated check REJECTED — {reason}. "
              f"Wrote {rel} for review; scenario NOT pointed at it.", file=sys.stderr)
        return None

    # Adopt the check (operator still reviews before promoting). Record whether it
    # was deterministically validated or only passed the structural gate.
    sc["check"] = rel
    sc["check_validation"] = reason
    sc.pop("check_synth_rejected", None)
    with open(staged, "w", encoding="utf-8") as fh:
        yaml.safe_dump(sc, fh, sort_keys=False, allow_unicode=True)
    return path


def _research_focus(query: Optional[str], mission_file: Optional[str]) -> str:
    """Resolve the research focus (genericity). Explicit `query` wins; else derive
    it from MISSION.md's `## Research focus` (falling back to `## Mission`); else the
    deterministic clive-default DEFAULT_QUERY. The topic is mission-driven."""
    from ..research.arxiv import DEFAULT_QUERY
    from ..research.focus import read_research_focus
    if query:
        return query
    if mission_file:
        focus = read_research_focus(mission_file)
        if focus:
            return focus
    return DEFAULT_QUERY


def _research_fetch(focus: str, max_papers: int, max_repos: int):
    """Deterministically fetch papers (arXiv) and repos (GitHub) for `focus`. Each
    source degrades independently — a network/parse failure yields [] for that
    source so the role never crashes. Returns (papers, repos)."""
    from ..research.arxiv import search_arxiv
    from ..research.git_repos import search_repos
    try:
        papers = search_arxiv(focus, max_results=max_papers) if max_papers > 0 else []
    except Exception as e:  # noqa: BLE001 — retrieval is best-effort
        print(f"[researcher] arXiv retrieval failed: {e}")
        papers = []
    try:
        repos = search_repos(focus, max_results=max_repos) if max_repos > 0 else []
    except Exception as e:  # noqa: BLE001 — retrieval is best-effort
        print(f"[researcher] GitHub retrieval failed: {e}")
        repos = []
    return papers, repos


def _research_material_section(mission_file: Optional[str]):
    """Fetch the human's MISSION.md material and render a HIGH-PRIORITY prompt
    section for it. Returns (section_text, fetched_paper_ids, fetched_repo_names).
    Degrades to ("", set(), set()) on any failure — never crashes the loop."""
    if not mission_file:
        return "", set(), set()
    try:
        from ..research.ingest import parse_material
        material = parse_material(mission_file)
    except Exception as e:  # noqa: BLE001 — ingestion is best-effort
        print(f"[researcher] human-material ingestion failed: {e}")
        return "", set(), set()
    if not material:
        return "", set(), set()

    ids: set[str] = set()
    names: set[str] = set()
    blocks: list[str] = []
    unfetched: list[str] = []
    for m in material:
        if m["kind"] == "arxiv" and m.get("paper") is not None:
            p = m["paper"]
            ids.add(re.sub(r"v\d+$", "", p.arxiv_id))
            blocks.append(p.brief())
        elif m["kind"] == "repo" and m.get("repo") is not None:
            r = m["repo"]
            names.add(r.full_name.lower())
            blocks.append(r.brief())
        elif m["kind"] == "unfetched":
            unfetched.append(f"- {m.get('value', m.get('source', '?'))} "
                             f"({m.get('reason', 'not fetched')})")
        else:  # arxiv/repo that we tried but couldn't fetch
            unfetched.append(f"- {m.get('source', '?')} "
                             f"({m.get('error', 'fetch returned nothing')})")

    if not blocks and not unfetched:
        return "", ids, names
    section = "\n\n## MATERIAL THE HUMAN ASKED YOU TO READ (HIGH PRIORITY)\n\n"
    if blocks:
        section += ("The operator explicitly dropped these into MISSION.md — give "
                    "them precedence when distilling briefs.\n\n"
                    + "\n\n".join(blocks) + "\n")
    if unfetched:
        section += ("\nNOTE — lines the operator listed but the factory did NOT "
                    "fetch (cite only material shown above, never these):\n"
                    + "\n".join(unfetched) + "\n")
    return section, ids, names


def research_cli_agents(store: Blackboard, query: Optional[str] = None,
                        max_papers: int = 8, max_repos: int = 6,
                        mission_file: Optional[str] = None) -> list[str]:
    """Researcher role: distill GROUNDED technique briefs from recent literature.

    Generic & mission-driven: the focus comes from `query`, else MISSION.md's
    `## Research focus` (fallback `## Mission`), else the clive-default query.
    Retrieval is deterministic Python from TWO sources — arXiv papers AND GitHub
    repositories — plus any material the human dropped into MISSION.md. The LLM only
    distills the fetched text, so the role stays ISOLATED like every other claude -p
    role (no web tools). Briefs are STAGED for operator vetting and only FEED the
    Proposer; nothing is applied automatically. Each source degrades independently —
    never crashes the loop."""
    focus = _research_focus(query, mission_file)
    papers, repos = _research_fetch(focus, max_papers, max_repos)
    material_section, human_ids, human_repos = _research_material_section(mission_file)

    # Provenance corpus: a brief may cite a fetched paper id OR a fetched repo name.
    fetched_ids = {re.sub(r"v\d+$", "", p.arxiv_id) for p in papers} | human_ids
    fetched_repos = {r.full_name.lower() for r in repos} | human_repos
    if not papers and not repos and not material_section:
        return []

    prompt = _load_prompt("researcher")
    if papers:
        prompt += ("\n\n## PAPERS (arXiv)\n\n"
                   + "\n\n".join(p.brief() for p in papers) + "\n")
    if repos:
        prompt += ("\n\n## REPOSITORIES (GitHub)\n\n"
                   + "\n\n".join(r.brief() for r in repos) + "\n")
    prompt += material_section
    reply, tokens, cost = claude_p(prompt)
    store.add_budget("researcher", tokens, cost, notes="research")

    try:
        parsed = yaml.safe_load(_extract(reply, ("yaml", "json", "")))
    except Exception:
        return []
    briefs = parsed.get("briefs", []) if isinstance(parsed, dict) else parsed
    if not isinstance(briefs, list):
        return []

    os.makedirs(paths.RESEARCH_STAGING_DIR, exist_ok=True)
    written: list[str] = []
    for b in briefs:
        if not isinstance(b, dict) or not b.get("suggested_change"):
            continue
        # Grounding guard: a brief must cite EITHER a paper id OR a repo full_name
        # that we actually fetched. Flag anything else for the operator to verify.
        aid = re.sub(r"v\d+$", "", str(b.get("arxiv_id", "")).strip())
        repo_cite = str(b.get("repo", "")).strip()
        repo_name = re.sub(r"^https?://(?:www\.)?github\.com/", "",
                           repo_cite, flags=re.IGNORECASE).rstrip("/").lower()
        grounded = (aid and aid in fetched_ids) or (repo_name and repo_name in fetched_repos)
        if not grounded:
            b["provenance_warning"] = (
                "citation (arxiv_id/repo) not among the fetched papers or repos — "
                "verify before acting on this")
        b["id"] = f"rb-{uuid.uuid4().hex[:6]}"
        b["status"] = "staged"   # operator vetting required; feeds the Proposer
        path = os.path.join(paths.RESEARCH_STAGING_DIR, f"{b['id']}.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(b, fh, sort_keys=False, allow_unicode=True)
        written.append(path)
    return written


def run_role(name: str, store: Blackboard, **kw):
    if name == "proposer":
        return propose(store)
    if name == "judge":
        return judge(store, kw["run_id"])
    if name == "reporter":
        return report(store, kw["candidate_id"])
    if name in ("scenario-miner", "scenario_miner", "miner"):
        return mine_scenarios(store, kw.get("limit", 10))
    if name in ("researcher", "research"):
        return research_cli_agents(store, kw.get("query"), kw.get("max_papers", 8),
                                   kw.get("max_repos", 6), kw.get("mission_file"))
    raise ValueError(f"unknown role {name!r}")
