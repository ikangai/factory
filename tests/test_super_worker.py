"""Super-worker transport: a FULL-CAPABILITY `claude -p` bounded by ENVIRONMENT, not
brain. The inverse of the isolated role transport — instead of stripping the tools
(`--tools ""`), it ENABLES a curated set (`--allowedTools …` incl. Task/Workflow) under
`--permission-mode acceptEdits`, with its file tools steered into a disposable
per-worker workspace (`--add-dir`, cwd) and a deny-by-default env. The DEFAULT toolset
withholds Bash, so the agent has no shell to escape the workspace or read host
files/creds — a reasonable SOFT boundary (like `envs/local_sandbox.py`; the docker env
is the hard one). So a super-worker can fan out subagents/workflows internally while its
file work stays in its own sandbox.

Hermetic — NO live agent is ever spawned: subprocess.run is monkeypatched to capture
the argv/cwd/env. (The live super-worker only runs when the operator runs the factory.)
"""
import json
import os
import subprocess
import types

from factory.roles import common


def test_super_argv_enables_curated_tools_and_confines_to_workspace():
    argv = common._super_worker_argv("/tmp/ws", ["Read", "Bash", "Workflow"])
    assert argv[:2] == ["claude", "-p"]
    j = " ".join(argv)
    assert "--permission-mode acceptEdits" in j          # acts without approval prompts…
    assert "--add-dir /tmp/ws" in j                       # …but only inside its workspace
    assert "--allowedTools Read Bash Workflow" in j       # curated capability (incl. fan-out)
    assert "--max-turns" in argv                          # bounded — can't loop unboundedly
    # The distinction from the isolated transport is the TOOLS: it must NOT disable them.
    assert "--tools" not in argv                          # not the isolating `--tools ""`
    # plugins/hooks still dropped → no team-barrier hang in a headless worker
    assert "--setting-sources" in argv


def test_default_toolset_excludes_bash():
    """SECURITY: with no Bash, the file tools are confined by --add-dir to the
    sandbox, so the agent can't read env/creds/held-out outside it. Bash escapes
    that confinement, so it is NOT a default — it must be explicitly opted in (and
    then only behind a hard boundary like the docker env)."""
    assert "Bash" not in common.DEFAULT_SUPER_TOOLS
    assert "Workflow" in common.DEFAULT_SUPER_TOOLS and "Task" in common.DEFAULT_SUPER_TOOLS


def test_claude_super_env_is_allowlisted(monkeypatch):
    """SECURITY: the child env is DENY-BY-DEFAULT — only claude's runtime + auth/config
    families pass through, so host secrets in the environment (known OR unknown) never
    reach the worker. (On-disk creds are handled by withholding Bash, not here.)"""
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        captured["env"] = kw.get("env")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(
            {"result": "DID IT", "usage": {"input_tokens": 3, "output_tokens": 4},
             "total_cost_usd": 0.01}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shh")
    monkeypatch.setenv("GH_TOKEN", "ghp_x")
    monkeypatch.setenv("MYCO_DB_PASSWORD", "p")     # UNKNOWN secret → allowlist drops it
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://x")  # auth/config family → kept

    text, tokens, cost = common.claude_super("do x", workdir="/tmp/ws-1",
                                             allowed_tools=["Read"])
    env = captured["env"]
    assert (text, tokens, cost) == ("DID IT", 7, 0.01)
    assert captured["cwd"] == "/tmp/ws-1"
    assert "AWS_SECRET_ACCESS_KEY" not in env and "GH_TOKEN" not in env
    assert "MYCO_DB_PASSWORD" not in env            # deny-by-default catches unknown secrets
    assert env.get("HOME") and env.get("PATH")      # runtime preserved (claude can run/auth)
    assert "ANTHROPIC_BASE_URL" in env              # claude auth/config family kept
    assert "--add-dir" in captured["argv"]


def test_super_argv_can_load_user_settings_for_plugins_skills_mcp():
    """settings='user' lets a worker load the agora plugin, the diary skill, and MCP
    (chrome-devtools) — a full claude instance. Default '' stays isolated."""
    full = common._super_worker_argv("/ws", common.DEVELOPER_TOOLS, settings="user")
    assert "--setting-sources user" in " ".join(full)
    iso = common._super_worker_argv("/ws", common.DEFAULT_SUPER_TOOLS)
    assert "--setting-sources " in " ".join(iso) and "--setting-sources user" not in " ".join(iso)


def test_web_and_skill_in_developer_and_researcher_toolsets():
    for t in ("WebSearch", "WebFetch", "Skill"):
        assert t in common.DEVELOPER_TOOLS and t in common.RESEARCHER_TOOLS
    assert "Bash" in common.DEVELOPER_TOOLS          # developer edits/tests
    assert "Bash" not in common.RESEARCHER_TOOLS     # researcher investigates, doesn't edit/shell


def test_super_argv_runs_as_guest_house_user_with_its_own_claude_and_bash():
    argv = common._super_worker_argv("/ws", common.DEVELOPER_TOOLS, as_user="Agent",
                                     claude_bin="/Users/Agent/.local/bin/claude")
    assert argv[:5] == ["sudo", "-H", "-u", "Agent", "--"]    # -H → Agent's HOME/auth
    assert argv[5] == "/Users/Agent/.local/bin/claude"        # Agent's OWN claude, by path
    assert "--add-dir" in argv and "Bash" in argv             # Bash safe under the OS boundary


def test_claude_super_as_user_defers_env_to_the_target_user(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, env=kw.get("env"), cwd=kw.get("cwd"))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(
            {"result": "ok", "usage": {}, "total_cost_usd": 0.0}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    common.claude_super("do", workdir="/ws", allowed_tools=["Bash"], as_user="Agent",
                        claude_bin="/Users/Agent/.local/bin/claude")
    assert seen["argv"][:5] == ["sudo", "-H", "-u", "Agent", "--"]
    assert seen["env"] is None     # under sudo -H -u, the TARGET user's own env (their ~/.claude)
    assert seen["cwd"] == "/ws"


def test_claude_super_never_crashes_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(subprocess, "run", boom)
    text, tokens, cost = common.claude_super("x", workdir="/tmp/w")
    assert text.startswith("[claude -p") and tokens == 0 and cost == 0.0


def test_workspaces_are_isolated_and_disposable():
    seen = []
    with common.super_worker_workspace() as a:
        assert os.path.isdir(a)
        with common.super_worker_workspace() as b:
            assert os.path.isdir(b)
            assert a != b          # two super-workers → two distinct sandbox workspaces
            seen = [a, b]
    assert not os.path.isdir(seen[0]) and not os.path.isdir(seen[1])   # auto-removed


# ---------------------------------------------------------------------------
# opt-in routing: a role becomes a super-worker only when config says so
# ---------------------------------------------------------------------------
from factory.common import config           # noqa: E402
from factory.common.store import Blackboard  # noqa: E402


def test_is_super_worker_reads_config(monkeypatch):
    monkeypatch.setattr(config, "load_config",
                        lambda: {"roles": {"super_workers": ["proposer"]}})
    assert config.is_super_worker("proposer")
    assert not config.is_super_worker("judge")
    monkeypatch.setattr(config, "load_config", lambda: {"roles": {"super_workers": "*"}})
    assert config.is_super_worker("judge")           # wildcard
    monkeypatch.setattr(config, "load_config", lambda: {})
    assert not config.is_super_worker("proposer")    # SAFE DEFAULT: stay isolated


def _proposer_env(tmp_path, monkeypatch):
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(tmp_path / "rs"))
    os.makedirs(tmp_path / "rs", exist_ok=True)
    monkeypatch.setattr("factory.common.paths.CANDIDATES_DIR", str(tmp_path / "c"))
    os.makedirs(tmp_path / "c", exist_ok=True)


def test_proposer_routes_to_super_worker_when_enabled(tmp_path, monkeypatch):
    _proposer_env(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "load_config",
                        lambda: {"roles": {"super_workers": ["proposer"]},
                                 "spec": {"max_changed_open_keys": 1}})
    captured = {}

    def fake_super(prompt, **k):
        captured["prompt"] = prompt
        return ('```json\n{"open_key":"system_prompt","new_value":"NEW","summary":"s"}\n```',
                5, 0.0)

    monkeypatch.setattr(common, "run_super_worker", fake_super)
    monkeypatch.setattr(common, "claude_p", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("isolated claude_p must NOT run in super-worker mode")))

    with Blackboard(str(tmp_path / "f.db")) as store:
        store.init_db()
        cid = common.propose(store)

    assert cid is not None
    assert "super-worker" in captured["prompt"].lower()  # got the loop preamble…
    assert "Proposer" in captured["prompt"]              # …prepended to the role contract


def test_proposer_stays_isolated_by_default(tmp_path, monkeypatch):
    _proposer_env(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "load_config",
                        lambda: {"spec": {"max_changed_open_keys": 1}})  # no roles.super_workers
    used = {}
    monkeypatch.setattr(common, "claude_p",
                        lambda prompt, **k: used.__setitem__("p", prompt) or ("{}", 0, 0.0))
    monkeypatch.setattr(common, "run_super_worker", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("super-worker must NOT run by default")))

    with Blackboard(str(tmp_path / "f.db")) as store:
        store.init_db()
        common.propose(store)   # {} patch → returns None, but must have used the isolated path

    assert "p" in used and "Proposer" in used["p"]
