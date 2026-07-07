"""clive_invoke source-override tests (real-merge-grade Piece 1).

To grade a CODE candidate the eval must run the CANDIDATE's clive source (its cand_repo
checkout), not the globally-configured sibling. build()/run() take an optional clive_root
override; when None the behaviour is exactly today's config.clive_entry(). Concurrency-safe:
the override travels with the call (no global/config mutation), so parallel rail workers each
grade their own checkout."""
import types

from factory.common import clive_invoke, config


def _stub(monkeypatch):
    monkeypatch.setattr(config, "clive_python", lambda: "python3")
    monkeypatch.setattr(config, "clive_entry", lambda: ("/global/clive", "/global/clive/clive.py"))
    monkeypatch.setattr(config, "target_config", lambda: {"entry": "clive.py"})
    monkeypatch.setattr(clive_invoke, "panel_env", lambda me: {})


def test_build_uses_the_global_clive_when_no_override(monkeypatch):
    _stub(monkeypatch)
    argv, _ = clive_invoke.build("goal", applied_env={}, applied_flags=[], env_vars={},
                                 model_entry={"name": "m"}, max_tokens=1000)
    assert argv[1] == "/global/clive/clive.py"          # the configured global source (unchanged)


def test_build_runs_the_candidate_source_when_clive_root_overridden(monkeypatch):
    _stub(monkeypatch)
    argv, _ = clive_invoke.build("goal", applied_env={}, applied_flags=[], env_vars={},
                                 model_entry={"name": "m"}, max_tokens=1000,
                                 clive_root="/cand/repo")
    assert argv[1] == "/cand/repo/clive.py"             # the CANDIDATE's clive source
    assert "/global/clive" not in " ".join(argv)


def test_run_executes_the_candidate_source_from_its_root(monkeypatch):
    _stub(monkeypatch)
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"], captured["cwd"] = argv, kw.get("cwd")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(clive_invoke.subprocess, "run", fake_run)
    clive_invoke.run("goal", applied_env={}, applied_flags=[], env_vars={},
                     model_entry={"name": "m"}, max_tokens=1000, timeout_s=5,
                     clive_root="/cand/repo")
    assert captured["argv"][1] == "/cand/repo/clive.py"
    assert captured["cwd"] == "/cand/repo"              # runs FROM the candidate root (import shim)


def test_clive_adapter_forwards_clive_root_to_invoke(monkeypatch):
    """The adapter is the pass-through the runner uses — clive_root must reach clive_invoke.run."""
    from factory.adapters.clive import CliveAdapter
    seen = {}

    def fake_invoke_run(goal, **kw):
        seen.update(kw)
        return types.SimpleNamespace(rc=0, stdout="", stderr="", duration_s=0.0,
                                     timed_out=False, argv=[], env_overrides={})

    monkeypatch.setattr(clive_invoke, "run", fake_invoke_run)
    CliveAdapter().run("goal", applied_env={}, applied_flags=[], env_vars={},
                       model_entry={"name": "m"}, max_tokens=100, timeout_s=5,
                       clive_root="/cand/repo")
    assert seen.get("clive_root") == "/cand/repo"
