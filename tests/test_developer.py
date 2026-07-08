"""Developer super-worker (design: docs/plans/2026-06-25-autonomous-code-factory.md):
runs in a clone of the target toward a task, makes a bounded code change, gets the
target's tests green, and commits to a branch. Hermetic — claude_super is monkeypatched
to capture the assembled prompt and the toolset; no live agent is spawned."""
from factory.common import paths
from factory.roles import common


def test_develop_candidate_builds_a_task_specific_prompt_with_bash(monkeypatch):
    captured = {}

    def fake_super(prompt, *, workdir, allowed_tools, as_user=None, claude_bin="claude", **k):
        captured.update(prompt=prompt, workdir=workdir, allowed_tools=tuple(allowed_tools),
                        as_user=as_user, claude_bin=claude_bin)
        return ("changed X; tests pass", 100, 0.02)

    monkeypatch.setattr(common, "claude_super", fake_super)
    out = common.develop_candidate(
        "/tmp/clone", task="make `clive count` handle empty input",
        branch="factory/cand-1", test_cmd="python -m pytest tests/ -q",
        frozen=["src/clive/execution/", "src/clive/selfmod/"])

    assert out["branch"] == "factory/cand-1" and out["tokens"] == 100
    p = captured["prompt"]
    assert "make `clive count` handle empty input" in p     # the task
    assert "factory/cand-1" in p                              # the branch to commit to
    assert "python -m pytest tests/ -q" in p                 # the test command to iterate on
    assert "src/clive/selfmod/" in p                         # the frozen surface it must avoid
    assert captured["workdir"] == "/tmp/clone"
    assert "Bash" in captured["allowed_tools"]                # a developer needs the shell


def test_develop_candidate_injects_profile_overlay_and_model(monkeypatch):
    """Phase 5: the capability profile's overlay is injected at {PROFILE} and its resolved model
    tier is threaded to the transport. No overlay → a generalist placeholder + the account default."""
    captured = {}

    def fake_super(prompt, *, model="", **k):
        captured.update(prompt=prompt, model=model)
        return ("done", 1, 0.0)

    monkeypatch.setattr(common, "claude_super", fake_super)
    common.develop_candidate("/clone", task="t", branch="b", test_cmd="pytest", frozen=[],
                             profile_overlay="PERSONA-MARKER: senior Python engineer",
                             model="claude-sonnet-4-6")
    assert "PERSONA-MARKER" in captured["prompt"] and captured["model"] == "claude-sonnet-4-6"

    common.develop_candidate("/clone", task="t", branch="b", test_cmd="pytest", frozen=[])
    assert "generalist" in captured["prompt"] and captured["model"] == ""


def test_develop_candidate_is_a_full_instance_with_web_and_own_squad(monkeypatch):
    captured = {}
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: captured.update(k) or ("done", 1, 0.0))
    monkeypatch.setattr(common.config, "load_config",
                        lambda: {"super_worker": {"settings": "user",
                                                  "extra_tools": ["mcp__chrome-devtools"],
                                                  "squad": "factory-workers"}})
    common.develop_candidate("/clone", task="t", branch="factory/cand-aaa111", test_cmd="pytest", frozen=[])
    assert captured["settings"] == "user"                     # full instance (agora/diary/MCP)
    assert "WebSearch" in captured["allowed_tools"]            # web search
    assert "mcp__chrome-devtools" in captured["allowed_tools"]  # chrome-devtools (config extra)
    env = captured["extra_env"]
    assert env["AGORA_SQUAD"] == "factory-workers-aaa111"      # UNIQUE per worker → solo, no hang
    assert env["AGORA_DIR"].endswith((".groupchat", ".agora"))  # posts to the FACTORY bus, not the clone's
    assert env["AGORA_SOLO_GRACE"] == "0"                      # one-shot: announce + work + exit, no park

    captured2 = {}
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: captured2.update(k) or ("done", 1, 0.0))
    common.develop_candidate("/clone", task="t", branch="factory/cand-bbb222", test_cmd="pytest", frozen=[])
    assert captured2["extra_env"]["AGORA_SQUAD"] != env["AGORA_SQUAD"]   # parallel workers → distinct squads


def test_develop_candidate_substitutes_factory_root(monkeypatch):
    """Task 8: the prompt's {FACTORY_ROOT} seam must resolve to the real absolute path — an
    unreplaced literal reaching the LLM would be a broken command in its final report."""
    captured = {}
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: captured.update(prompt=prompt) or ("done", 1, 0.0))
    common.develop_candidate("/clone", task="t", branch="b", test_cmd="pytest", frozen=[])
    assert paths.FACTORY_ROOT in captured["prompt"]
    assert "{FACTORY_ROOT}" not in captured["prompt"]


def test_develop_candidate_runs_as_guest_house_user_when_configured(monkeypatch):
    captured = {}
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: captured.update(k) or ("done", 1, 0.0))
    common.develop_candidate("/clone", task="t", branch="b", test_cmd="pytest",
                             frozen=[], as_user="agent",
                             claude_bin="/Users/agent/.local/bin/claude")
    assert captured["as_user"] == "agent"
    assert captured["claude_bin"] == "/Users/agent/.local/bin/claude"
