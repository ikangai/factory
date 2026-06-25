"""#2: unattended corpus INTAKE — mine → synth+validate oracle → auto-promote.

`cmd_intake` is the missing intake arrow that lets the loop grow its own WORKING
grading corpus unattended, so the gain governor keeps finding genuinely new
champion failures instead of stalling on a static corpus.

The safety contract (operator's decision):
  * a mined scenario auto-promotes to WORKING **only** when its synthesized oracle
    passes the #64 validator (check_validation starts with 'validated:');
  * a merely-structural/unverifiable oracle ('unverified: …') stays STAGED;
  * a rejected oracle (synth returns None) stays STAGED;
  * HELD-OUT is NEVER auto-grown — overfit hygiene stays a human action.

Hermetic — no LLM: we stub mine_scenarios (writes a staged scenario) and
synth_check (writes a compilable check + stamps check_validation), and exercise
the REAL cmd_promote_scenario path + store.
"""
import os

import pytest
import yaml

from factory.common import paths
from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Redirect every path cmd_intake/promote touches into tmp."""
    staging = tmp_path / "staging"
    working = tmp_path / "working"
    heldout = tmp_path / "heldout"
    for d in (staging, working, heldout):
        d.mkdir()
    monkeypatch.setattr(paths, "FACTORY_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "STAGING_DIR", str(staging))
    monkeypatch.setattr(paths, "WORKING_DIR", str(working))
    monkeypatch.setattr(paths, "HELD_OUT_DIR", str(heldout))
    return tmp_path


def _stub_mine(monkeypatch, sid="mined-abc"):
    def fake_mine(store, limit=10):
        os.makedirs(paths.STAGING_DIR, exist_ok=True)
        p = os.path.join(paths.STAGING_DIR, f"{sid}.yaml")
        with open(p, "w", encoding="utf-8") as fh:
            yaml.safe_dump({"id": sid, "goal": "do a thing", "class": "single",
                            "source": "mined", "partition": "staging",
                            "seed_files": {"in.txt": "x"}, "leakage_count": 0},
                           fh, sort_keys=False)
        return [p]
    monkeypatch.setattr("factory.roles.common.mine_scenarios", fake_mine)


def _stub_synth(monkeypatch, *, validation, adopt=True):
    """Stub synth_check: when adopt, write a compilable check + stamp validation
    onto the staged yaml and return the path; else return None (rejected)."""
    def fake_synth(store, sid):
        staged = os.path.join(paths.STAGING_DIR, f"{sid}.yaml")
        with open(staged, "r", encoding="utf-8") as fh:
            sc = yaml.safe_load(fh) or {}
        if not adopt:
            sc["check_synth_rejected"] = "stub reject"
            with open(staged, "w", encoding="utf-8") as fh:
                yaml.safe_dump(sc, fh, sort_keys=False)
            return None
        rel = f"checks/scenarios/{sid.replace('-', '_')}_check.py"
        abspath = os.path.join(paths.FACTORY_ROOT, rel)
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "w", encoding="utf-8") as fh:
            fh.write("def acceptance(ctx):\n    return None\n")
        sc["check"] = rel
        sc["check_validation"] = validation
        with open(staged, "w", encoding="utf-8") as fh:
            yaml.safe_dump(sc, fh, sort_keys=False)
        return abspath
    monkeypatch.setattr("factory.roles.common.synth_check", fake_synth)


def _store(tmp_path):
    bb = Blackboard(str(tmp_path / "f.db"))
    bb.init_db()
    return bb


def test_validated_scenario_autopromotes_to_working(env, monkeypatch):
    _stub_mine(monkeypatch)
    _stub_synth(monkeypatch, validation="validated: passes in.txt='x', fails perturbations")
    with _store(env) as store:
        res = orch.cmd_intake(store)
        working_ids = [s["id"] for s in store.list_scenarios(partition="working")]

    assert res["promoted"] == ["mined-abc"]
    assert "mined-abc" in working_ids
    assert os.path.exists(os.path.join(paths.WORKING_DIR, "mined-abc.yaml"))
    assert not os.path.exists(os.path.join(paths.STAGING_DIR, "mined-abc.yaml"))  # left staging


def test_unverified_oracle_stays_staged(env, monkeypatch):
    _stub_mine(monkeypatch)
    _stub_synth(monkeypatch, validation="unverified: check uses ctx.run (shell)")
    with _store(env) as store:
        res = orch.cmd_intake(store)
        working_ids = [s["id"] for s in store.list_scenarios(partition="working")]

    assert res["promoted"] == []
    assert res["unverified"] == ["mined-abc"]
    assert working_ids == []
    assert os.path.exists(os.path.join(paths.STAGING_DIR, "mined-abc.yaml"))  # kept for human


def test_rejected_oracle_stays_staged(env, monkeypatch):
    _stub_mine(monkeypatch)
    _stub_synth(monkeypatch, validation="(n/a)", adopt=False)
    with _store(env) as store:
        res = orch.cmd_intake(store)
        working_ids = [s["id"] for s in store.list_scenarios(partition="working")]

    assert res["promoted"] == []
    assert res["rejected"] == ["mined-abc"]
    assert working_ids == []
    assert os.path.exists(os.path.join(paths.STAGING_DIR, "mined-abc.yaml"))


def test_intake_errored_when_staged_yaml_unreadable_after_synth(env, monkeypatch):
    """If a staged YAML can't be re-read after synth (transient FS fault), the
    scenario must land in a visible 'errored' bucket — not silently vanish from the
    disposition tally — and never be promoted."""
    _stub_mine(monkeypatch)

    def fake_synth(store, sid):
        rel = f"checks/scenarios/{sid.replace('-', '_')}_check.py"
        ab = os.path.join(paths.FACTORY_ROOT, rel)
        os.makedirs(os.path.dirname(ab), exist_ok=True)
        with open(ab, "w", encoding="utf-8") as fh:
            fh.write("def acceptance(ctx):\n    return None\n")
        os.remove(os.path.join(paths.STAGING_DIR, f"{sid}.yaml"))  # vanish before re-read
        return ab

    monkeypatch.setattr("factory.roles.common.synth_check", fake_synth)
    with _store(env) as store:
        res = orch.cmd_intake(store)
        working_ids = [s["id"] for s in store.list_scenarios(partition="working")]

    assert res["errored"] == ["mined-abc"]
    assert res["promoted"] == []
    assert working_ids == []


def test_intake_never_autogrows_heldout(env, monkeypatch):
    """Even a perfectly validated oracle goes to WORKING only — never held-out."""
    _stub_mine(monkeypatch)
    _stub_synth(monkeypatch, validation="validated: passes, fails perturbations")
    with _store(env) as store:
        orch.cmd_intake(store)
        heldout_ids = [s["id"] for s in store.list_scenarios(partition="held-out")]

    assert heldout_ids == []
    assert os.listdir(paths.HELD_OUT_DIR) == []
