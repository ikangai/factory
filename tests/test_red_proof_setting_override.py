"""F10 (round-2 integration fix, docs/plans/2026-08-06-publication-broker-design.md,
Component E): `super_worker.red_proof` was listed in SETTINGS_SPEC (dashboard-editable,
"applied at next shift") and read by org._bounds_text as board-controlled, but
develop.py's OWN internal fallback read `config.load_config()` directly — the value
never actually threaded through `common.config.resolve_setting`, so a STORE OVERRIDE
(exactly what the dashboard promises) was silently ignored. Mirrors test_run_cli.py's
own full-lifecycle idiom — a new file, that one untouched.
"""
import pytest

from factory.common.store import Blackboard
from factory.orchestrator import develop as developmod
from factory.orchestrator import orchestrator, shift as shiftmod
from factory.roles import research_feed


@pytest.fixture(autouse=True)
def _no_real_research(monkeypatch):
    monkeypatch.setattr(research_feed, "propose_directions", lambda store, **k: [])
    monkeypatch.setattr(orchestrator, "_read_mission_md", lambda: None)
    monkeypatch.setattr(orchestrator, "_write_mission_md", lambda statement: None)
    monkeypatch.setattr(orchestrator, "_seed_staffing", lambda store: [])


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def test_store_override_of_red_proof_reaches_the_dispatched_worker(tmp_path, monkeypatch):
    """The exact F10 regression: config.yaml's default is False; a STORE OVERRIDE sets it
    True; the value the worker actually gets must be True. Deliberately does NOT inject a
    custom `executor` (that would bypass the very orchestrator.py:_k()-based wiring this
    fix lands in) — instead monkeypatches develop_task, the callable cmd_run's OWN
    internal executor resolves to."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(orchestrator.config, "load_config",
                        lambda: {"autonomy": {}, "super_worker": {"red_proof": False}})
    captured = {}

    def capture_dev(text, **kw):
        captured.update(kw)
        return {"action": "merged", "merge_sha": "abc123"}

    monkeypatch.setattr(developmod, "develop_task", capture_dev)

    with _store(tmp_path) as s:
        s.set_mission("ship it")
        s.set_setting("super_worker.red_proof", "true")     # the dashboard's own write path
        s.add_task("t1", "fix a thing", source="research")

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            orchestrator.cmd_task(store, "claim", rest="t1")
            return {"status": "completed", "tokens_used": 5}

        orchestrator.cmd_run(s, conductor=conductor, token_budget=100, wall_clock_s=5)

    assert captured.get("red_proof") is True


def test_no_override_falls_back_to_config_yaml_false(tmp_path, monkeypatch):
    """Regression safety: with no store override, the config.yaml value (False here)
    still reaches the worker — the fix must not flip the default."""
    monkeypatch.setattr(shiftmod.killswitch, "is_halted", lambda: False)
    monkeypatch.setattr(orchestrator.config, "load_config",
                        lambda: {"autonomy": {}, "super_worker": {"red_proof": False}})
    captured = {}

    def capture_dev(text, **kw):
        captured.update(kw)
        return {"action": "merged", "merge_sha": "abc123"}

    monkeypatch.setattr(developmod, "develop_task", capture_dev)

    with _store(tmp_path) as s:
        s.set_mission("ship it")
        s.add_task("t1", "fix a thing", source="research")

        def conductor(store, *, shift_id, mission, token_budget, wall_clock_s):
            orchestrator.cmd_task(store, "claim", rest="t1")
            return {"status": "completed", "tokens_used": 5}

        orchestrator.cmd_run(s, conductor=conductor, token_budget=100, wall_clock_s=5)

    assert captured.get("red_proof") is False
