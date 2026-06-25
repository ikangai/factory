"""Developer super-worker (design: docs/plans/2026-06-25-autonomous-code-factory.md):
runs in a clone of the target toward a task, makes a bounded code change, gets the
target's tests green, and commits to a branch. Hermetic — claude_super is monkeypatched
to capture the assembled prompt and the toolset; no live agent is spawned."""
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


def test_develop_candidate_runs_as_guest_house_user_when_configured(monkeypatch):
    captured = {}
    monkeypatch.setattr(common, "claude_super",
                        lambda prompt, **k: captured.update(k) or ("done", 1, 0.0))
    common.develop_candidate("/clone", task="t", branch="b", test_cmd="pytest",
                             frozen=[], as_user="agent",
                             claude_bin="/Users/agent/.local/bin/claude")
    assert captured["as_user"] == "agent"
    assert captured["claude_bin"] == "/Users/agent/.local/bin/claude"
