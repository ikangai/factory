"""GenericAdapter (target.provider: "generic") — the second registered adapter
(docs/plans/2026-07-09-generic-adapter-design.md): a fully config-driven command target
so the scenario-eval loop runs against ANY repo invocable as `<interpreter> <entry>
[args…]`. These tests drive the adapter hermetically with a stub target script — never
the real clive sibling.
"""
import json
import os
import stat
import sys

import pytest

from factory.adapters.base import TargetAdapter
from factory.adapters.generic import GenericAdapter
from factory.common import config


def _target_cfg(root, entry="echo_target.py", exec_block=None, **extra):
    cfg = {"provider": "generic", "root": str(root), "python": sys.executable,
           "entry": entry}
    if exec_block is not None:
        cfg["exec"] = exec_block
    cfg.update(extra)
    return cfg


@pytest.fixture()
def stub_target(tmp_path):
    """A stub target repo: an entry script that dumps its argv + selected env as JSON
    to stdout, prints a session marker to stderr, and exits 0."""
    root = tmp_path / "target"
    root.mkdir()
    script = root / "echo_target.py"
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps({\n"
        "    'argv': sys.argv[1:],\n"
        "    'env': {k: v for k, v in os.environ.items()\n"
        "            if k.startswith(('FACTORY_', 'MODEL', 'LLM_', 'SANDBOX_'))},\n"
        "    'cwd': os.getcwd(),\n"
        "}))\n"
        "print('session at /tmp/generic/abc123', file=sys.stderr)\n")
    return root


# --- registration ---------------------------------------------------------------------
def test_get_adapter_registers_generic(monkeypatch):
    monkeypatch.setattr(config, "target_config", lambda: {"provider": "generic"})
    adapter = config.get_adapter()
    assert isinstance(adapter, GenericAdapter)
    assert isinstance(adapter, TargetAdapter)
    assert adapter.name == "generic"


# --- actuate ---------------------------------------------------------------------------
def test_actuate_maps_open_scalars_to_prefixed_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "target_config",
                        lambda: _target_cfg(tmp_path, exec_block={"spec_env_prefix": "FACTORY_"}))
    spec = {"open": {"temperature": 0.2, "verbose": True, "quiet": False,
                     "mode": "fast",
                     "nested": {"a": 1},          # not actuatable generically
                     "listy": [1, 2]},
            "frozen": {}, "meta": {}}
    applied = GenericAdapter().actuate(spec, str(tmp_path), "minimal")
    assert applied.env["FACTORY_TEMPERATURE"] == "0.2"
    assert applied.env["FACTORY_VERBOSE"] == "1"
    assert applied.env["FACTORY_QUIET"] == "0"
    assert applied.env["FACTORY_MODE"] == "fast"
    assert applied.flags == []
    # nested structures are DECLARED but not actuatable -> pending + a note, never silent
    assert "nested" in applied.pending and "listy" in applied.pending
    assert applied.notes


def test_actuate_prefix_is_configurable_and_empty_open_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "target_config",
                        lambda: _target_cfg(tmp_path, exec_block={"spec_env_prefix": "APP_"}))
    applied = GenericAdapter().actuate({"open": {"knob": "x"}}, str(tmp_path), "minimal")
    assert applied.env == {"APP_KNOB": "x"}
    empty = GenericAdapter().actuate({}, str(tmp_path), "minimal")
    assert empty.env == {} and empty.pending == []


# --- panel_env ---------------------------------------------------------------------------
def test_panel_env_maps_model_entry_via_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(
        tmp_path, exec_block={"model_env": {"model": "MODEL", "provider": "LLM_PROVIDER",
                                            "base_url": "LLM_BASE_URL"}}))
    env = GenericAdapter().panel_env({"model": "m-1", "provider": "openrouter"})
    assert env == {"MODEL": "m-1", "LLM_PROVIDER": "openrouter"}  # no base_url -> omitted


def test_panel_env_defaults_to_empty_mapping(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(tmp_path))
    assert GenericAdapter().panel_env({"model": "m-1"}) == {}


# --- parse_session_dirs ---------------------------------------------------------------
def test_parse_session_dirs_uses_the_configured_regex(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(
        tmp_path, exec_block={"session_dir_regex": r"session at (\S+)"}))
    dirs = GenericAdapter().parse_session_dirs("x\nsession at /tmp/generic/abc123\ny")
    assert dirs == ["/tmp/generic/abc123"]


def test_parse_session_dirs_defaults_to_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(tmp_path))
    assert GenericAdapter().parse_session_dirs("anything at all") == []


# --- scrub_env ---------------------------------------------------------------------------
def test_scrub_env_drops_host_creds_but_keeps_neutral_vars(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(tmp_path))
    env = {"AWS_SECRET_ACCESS_KEY": "x", "GH_TOKEN": "y", "PATH": "/bin",
           "SANDBOX_OK": "1"}
    GenericAdapter().scrub_env(env)
    assert "AWS_SECRET_ACCESS_KEY" not in env and "GH_TOKEN" not in env
    assert env["PATH"] == "/bin" and env["SANDBOX_OK"] == "1"


# --- run -----------------------------------------------------------------------------------
def _run(adapter, goal="do the thing", **kw):
    defaults = dict(applied_env={}, applied_flags=[], env_vars={},
                    model_entry={}, max_tokens=1234, timeout_s=30)
    defaults.update(kw)
    return adapter.run(goal, **defaults)


def test_run_invokes_entry_with_goal_substituted_into_args(monkeypatch, stub_target):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(
        stub_target, exec_block={"args": ["--task", "{goal}", "--json"]}))
    res = _run(GenericAdapter(), goal="paint it blue")
    assert res.rc == 0 and not res.timed_out
    out = json.loads(res.stdout)
    assert out["argv"] == ["--task", "paint it blue", "--json"]
    assert os.path.realpath(out["cwd"]) == os.path.realpath(str(stub_target))
    assert res.argv[0] == sys.executable and res.argv[1].endswith("echo_target.py")


def test_run_appends_goal_when_the_template_has_no_placeholder(monkeypatch, stub_target):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(
        stub_target, exec_block={"args": ["--json"]}))
    res = _run(GenericAdapter(), goal="g")
    assert json.loads(res.stdout)["argv"] == ["--json", "g"]


def test_run_overlays_spec_model_and_token_env(monkeypatch, stub_target):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(
        stub_target, exec_block={"max_tokens_env": "FACTORY_MAX_TOKENS",
                                 "model_env": {"model": "MODEL"}}))
    res = _run(GenericAdapter(), applied_env={"FACTORY_KNOB": "7"},
               env_vars={"SANDBOX_OK": "1"}, model_entry={"model": "m-9"})
    env = json.loads(res.stdout)["env"]
    assert env["FACTORY_KNOB"] == "7"
    assert env["FACTORY_MAX_TOKENS"] == "1234"
    assert env["MODEL"] == "m-9"
    assert env["SANDBOX_OK"] == "1"
    # env overlays are recorded for the evidence trail
    assert res.env_overrides["FACTORY_MAX_TOKENS"] == "1234"


def test_run_scrubs_host_creds_from_the_child_env(monkeypatch, stub_target):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(
        stub_target, exec_block={"args": ["--json"]}))
    monkeypatch.setenv("GH_TOKEN", "leaky")
    root = stub_target
    script = root / "echo_target.py"
    script.write_text("import os\nprint('GH_TOKEN' in os.environ)\n")
    res = _run(GenericAdapter())
    assert res.stdout.strip() == "False"


def test_run_clive_root_override_grades_a_candidate_checkout(monkeypatch, tmp_path, stub_target):
    """clive_root (the seam's name for 'grade THIS checkout') must swap the SOURCE the
    entry runs from — the real-merge-grade path depends on it."""
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(stub_target))
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "echo_target.py").write_text("print('CANDIDATE COPY')\n")
    res = _run(GenericAdapter(), clive_root=str(candidate))
    assert res.stdout.strip() == "CANDIDATE COPY"


def test_run_times_out_and_reports_it(monkeypatch, tmp_path):
    root = tmp_path / "sleepy"
    root.mkdir()
    (root / "echo_target.py").write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(root))
    res = _run(GenericAdapter(), timeout_s=1)
    assert res.timed_out and res.rc != 0
    assert res.duration_s < 20


def test_run_missing_entry_raises_an_actionable_config_error(monkeypatch, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(root, entry="nope.py"))
    with pytest.raises(FileNotFoundError) as e:
        _run(GenericAdapter())
    assert "target.entry" in str(e.value)


# --- code-development seam defaults ----------------------------------------------------
def test_test_command_defaults_to_pytest_and_respects_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(tmp_path))
    cmd = GenericAdapter().test_command()
    assert cmd[0] == sys.executable and cmd[1:4] == ["-m", "pytest", "tests/"]

    monkeypatch.setattr(config, "target_config",
                        lambda: _target_cfg(tmp_path, test_command=["make", "test"]))
    assert GenericAdapter().test_command() == ["make", "test"]


def test_entry_and_interpreter_resolve_from_target_config(monkeypatch, stub_target):
    monkeypatch.setattr(config, "target_config", lambda: _target_cfg(stub_target))
    root, entry_abs = GenericAdapter().entry()
    assert os.path.realpath(root) == os.path.realpath(str(stub_target))
    assert entry_abs.endswith("echo_target.py")
    assert GenericAdapter().interpreter() == sys.executable
