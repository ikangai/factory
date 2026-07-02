"""Target-derived workforce seeding (reporting/staffing.py). The bench is a function of the
TARGET: stack specialists are detected from its manifests (deterministic — no LLM) and
re-derived whenever the factory is re-pointed. Additive only; retiring is the conductor's call."""
from factory.reporting import staffing


def test_seed_profiles_follow_the_target(store, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    added = staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo")
    names = {p["name"] for p in store.list_profiles(active_only=True)}
    assert {"generalist", "python-dev", "test-engineer", "docs-writer"} <= names
    assert "ts-dev" not in names
    assert set(added) == {"generalist", "python-dev", "test-engineer", "docs-writer"}
    assert staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo") == []   # idempotent


def test_repoint_adds_missing_stack_never_retires(store, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")   # py target first
    staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo")
    ts = tmp_path / "ts"
    ts.mkdir()
    (ts / "package.json").write_text("{}")
    (ts / "tsconfig.json").write_text("{}")
    added = staffing.ensure_seeded(store, str(ts), "acme/ts-repo")   # re-point
    names = {p["name"] for p in store.list_profiles(active_only=True)}
    assert "ts-dev" in names and "python-dev" in names            # additive, not destructive
    assert added == ["ts-dev"]
    assert "node-dev" not in names                                # ts-dev supersedes node-dev


def test_reseed_never_resurrects_or_clobbers(store, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo")
    store.retire_profile("python-dev")                              # the conductor's call
    staffing.ensure_seeded(store, str(tmp_path), "acme/py-repo-2")  # slug change forces a re-run
    assert "python-dev" not in {p["name"] for p in store.list_profiles(active_only=True)}
    # a retired stack profile counts as PRESENT — the re-seed must not add a second row either
    assert [p["name"] for p in store.list_profiles()].count("python-dev") == 1


def test_cmd_run_wiring_seeds_the_bench_and_is_best_effort(store, tmp_path, monkeypatch):
    """Task 5.2 wiring: orchestrator._seed_staffing derives the bench from the target root
    (config) — and is best-effort, so a config raise is swallowed, never breaking the run."""
    from factory.common import config
    from factory.orchestrator import orchestrator
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr(config, "clive_entry", lambda: (str(tmp_path), str(tmp_path / "x.py")))
    monkeypatch.setattr(config, "target_repo_slug", lambda: "acme/py-repo")
    seeded = orchestrator._seed_staffing(store)
    assert {"generalist", "python-dev"} <= set(seeded)
    assert orchestrator._seed_staffing(store) == []           # idempotent on the second run

    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(config, "clive_entry", _boom)         # a config hiccup is swallowed
    assert orchestrator._seed_staffing(store) == []


def test_detect_stacks_markers():
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "Cargo.toml"), "w").close()
        assert staffing.detect_stacks(d) == ["rust-dev"]
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "go.mod"), "w").close()
        open(os.path.join(d, "requirements.txt"), "w").close()
        assert set(staffing.detect_stacks(d)) == {"go-dev", "python-dev"}
