"""Orchestrator loop-wiring tests.

Task 5.1 — unattended-failure event trigger: when the REAL-mode post-shift
graduation/issue-sync path fails, the swallowed error must become a deduped,
conductor-only backlog task + a durable factory learning (gated OFF by
default via autonomy.failure_tasks) instead of vanishing into a log print.

Hermetic: tmp_path store, injected graduate_fn (no real git/gh/network), the
config gate monkeypatched — zero writes to the live blackboard.db, no LLM.
"""
from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch
from factory.reporting import factory_memory


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


class _Boom:
    """A graduate_fn that always raises — the failure the trigger must react to."""

    def __init__(self, msg="ff push rejected: non-fast-forward"):
        self.msg = msg
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        raise RuntimeError(self.msg)


def _gate(monkeypatch, *, on: bool):
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"failure_tasks": on}})


# -- prod-push quality gate wiring (Theme 4) --------------------------------
def test_graduation_retest_wires_the_adapter_suite_when_on(tmp_path, monkeypatch):
    import types
    with _store(tmp_path) as s:
        captured = {}

        def capture(**kw):
            captured.update(kw)
            return {"action": "synced"}

        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"autonomy": {"graduation_retest": True}})
        monkeypatch.setattr(orch.config, "get_adapter",
                            lambda: types.SimpleNamespace(run_tests=lambda cwd, **k: (True, "ok")))
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=capture,
                                   repo="o/r", root="/root", base="base")
        assert captured.get("test_fn") is not None
        assert captured["test_fn"]("/root") == (True, "ok")     # routes to the adapter's suite


def test_graduation_retest_passes_no_test_fn_when_off(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        captured = {}

        def capture(**kw):
            captured.update(kw)
            return {"action": "synced"}

        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"autonomy": {"graduation_retest": False}})
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=capture,
                                   repo="o/r", root="/root", base="base")
        assert captured.get("test_fn") is None


# -- _graduate_after_shift wiring -------------------------------------------
def test_graduation_error_files_conductor_task_and_learning_when_gated_on(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        _gate(monkeypatch, on=True)
        g = _Boom("ff push rejected: non-fast-forward")
        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "error"                      # still swallowed
        # exactly one backlog task, filed with a store-legal source (no 'factory' value)
        tasks = s.list_tasks(status="open")
        assert len(tasks) == 1
        t = tasks[0]
        assert t["source"] == "worker"                       # tasks.source CHECK has no 'factory'
        assert t["source_ref"] == "graduation"               # the dedup marker
        assert t["id"].startswith("task-")
        # detail is an explicit conductor-only instruction carrying the error
        assert "do NOT claim" in t["detail"]
        assert "@human" in t["detail"]
        assert "cannot fix factory infrastructure" in t["detail"]
        assert "non-fast-forward" in t["detail"]
        # a durable factory learning, scope='graduation'
        rows = s.learnings_for_role("factory", limit=50)
        grad = [r for r in rows if r.get("scope") == "graduation"]
        assert len(grad) == 1
        assert "non-fast-forward" in grad[0]["content"]


def test_graduation_error_is_noop_when_gate_off_by_default(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        # autonomy present but the key absent -> default OFF
        monkeypatch.setattr(orch.config, "load_config", lambda: {"autonomy": {}})
        g = _Boom()
        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "error"
        assert s.list_tasks(status="open") == []
        assert s.learnings_for_role("factory", limit=50) == []


def test_graduation_failure_deduped_against_open(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        _gate(monkeypatch, on=True)
        g = _Boom()
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                   repo="o/r", root="/x", base="base")
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                   repo="o/r", root="/x", base="base")
        # a second failure must NOT re-spam a second open task
        assert len(s.list_tasks(status="open")) == 1


def test_graduation_failure_deduped_against_blocked(tmp_path, monkeypatch):
    """A scope-rejected/handled failure task can leave 'open' OR be moved to 'blocked';
    open-AND-blocked dedup keeps the trigger from re-spamming either way."""
    with _store(tmp_path) as s:
        _gate(monkeypatch, on=True)
        g = _Boom()
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                   repo="o/r", root="/x", base="base")
        tid = s.list_tasks(status="open")[0]["id"]
        s.set_task_status(tid, "blocked", result="escalated")
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                   repo="o/r", root="/x", base="base")
        assert s.list_tasks(status="open") == []             # no fresh open task
        assert len(s.list_tasks(status="blocked")) == 1


def test_resolved_failure_task_does_not_block_a_fresh_one(tmp_path, monkeypatch):
    """Once the operator marks the failure task done, a NEW graduation failure files
    a fresh task (dedup is scoped to open+blocked, not the whole history)."""
    with _store(tmp_path) as s:
        _gate(monkeypatch, on=True)
        g = _Boom()
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                   repo="o/r", root="/x", base="base")
        tid = s.list_tasks(status="open")[0]["id"]
        s.set_task_status(tid, "done", result="escalated + resolved")
        orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                   repo="o/r", root="/x", base="base")
        assert len(s.list_tasks(status="open")) == 1         # a fresh one


# -- factory_memory.record_graduation_failure (unit) ------------------------
def test_record_graduation_failure_shapes_and_dedup(tmp_path):
    with _store(tmp_path) as s:
        r1 = factory_memory.record_graduation_failure(s, error="boom-A")
        assert r1["deduped"] is False and r1["task_id"] is not None
        assert r1["learning"] is not None
        # a distinct error -> the OPEN task dedups (only one open failure at a time)
        r2 = factory_memory.record_graduation_failure(s, error="boom-B")
        assert r2["deduped"] is True and r2["task_id"] is None
        assert len(s.list_tasks(status="open")) == 1
        # the learning is always recorded so recurrence is counted (hits / new rows)
        assert s.learnings_for_role("factory", limit=50)


def test_record_graduation_failure_empty_error_is_safe(tmp_path):
    with _store(tmp_path) as s:
        r = factory_memory.record_graduation_failure(s, error="")
        assert r["task_id"] is not None
        t = s.list_tasks(status="open")[0]
        assert "(no error text)" in t["detail"]


def test_record_graduation_failure_distinct_refs_do_not_collide(tmp_path):
    """Edge-specific dedup (lag-alarm hardening): dedup is scoped to the caller's `ref`,
    so an open base-edge lag task cannot swallow the publication edge's escalation —
    while a repeat on the SAME ref still dedupes."""
    with _store(tmp_path) as s:
        r1 = factory_memory.record_graduation_failure(s, error="A", ref="graduation:lag-base")
        r2 = factory_memory.record_graduation_failure(s, error="B", ref="graduation:lag-publication")
        assert r1["deduped"] is False and r2["deduped"] is False
        assert sorted(t["source_ref"] for t in s.list_tasks(status="open")) == \
            ["graduation:lag-base", "graduation:lag-publication"]
        r3 = factory_memory.record_graduation_failure(s, error="A2", ref="graduation:lag-base")
        assert r3["deduped"] is True and len(s.list_tasks(status="open")) == 2


# -- cmd_rebaseline: the periodic full re-baseline (Piece 5) -----------------
def _fake_adapter(revert_sha="revert-sha"):
    import types
    return types.SimpleNamespace(entry=lambda: ("/champ", "/champ/clive.py"),
                                 revert_commit=lambda repo, sha: revert_sha)


def _scores(working=0.8, held=0.5):
    return {"working": working, "held_out": held, "held_out_measured": True,
            "safety_flag": False, "n_working": 1, "n_held_out": 1}


def test_rebaseline_first_run_stores_baseline_no_regression(tmp_path, monkeypatch):
    import json
    with _store(tmp_path) as s:
        monkeypatch.setattr(s, "list_scenarios", lambda **k: [{"id": "w1", "partition": "working"}])
        monkeypatch.setattr(orch.config, "load_config", lambda: {"grade": {}})
        res = orch.cmd_rebaseline(s, full_scores_fn=lambda store, **k: _scores(),
                                  adapter=_fake_adapter(), champ_root="/champ")
        assert res["regression"]["regressed"] is False and res["reverted"] is None
        assert json.loads(s.get_setting("grade.baseline"))["working"] == 0.8   # baseline stored


def test_rebaseline_regression_without_autorevert_reports_only(tmp_path, monkeypatch):
    import json
    with _store(tmp_path) as s:
        s.set_setting("grade.baseline", json.dumps(_scores(working=0.9)))       # prior higher
        monkeypatch.setattr(s, "list_scenarios", lambda **k: [{"id": "w1", "partition": "working"}])
        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"grade": {"rebaseline_autorevert": False}})
        res = orch.cmd_rebaseline(s, full_scores_fn=lambda store, **k: _scores(working=0.6),
                                  adapter=_fake_adapter(), champ_root="/champ")
        assert res["regression"]["regressed"] is True and res["reverted"] is None  # OFF → no revert


def test_rebaseline_regression_with_autorevert_reverts_champion(tmp_path, monkeypatch):
    import json
    with _store(tmp_path) as s:
        s.set_setting("grade.baseline", json.dumps(_scores(working=0.9)))
        monkeypatch.setattr(s, "list_scenarios", lambda **k: [{"id": "w1", "partition": "working"}])
        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"grade": {"rebaseline_autorevert": True}})
        res = orch.cmd_rebaseline(s, full_scores_fn=lambda store, **k: _scores(working=0.6),
                                  adapter=_fake_adapter(revert_sha="rv1"), champ_root="/champ",
                                  head_sha_fn=lambda root: "headsha")
        assert res["regression"]["regressed"] is True and res["reverted"] == "rv1"  # ON → reverted


def test_rebaseline_dry_run_stores_nothing_and_never_reverts(tmp_path, monkeypatch):
    import json
    with _store(tmp_path) as s:
        s.set_setting("grade.baseline", json.dumps(_scores(working=0.9)))
        monkeypatch.setattr(s, "list_scenarios", lambda **k: [{"id": "w1", "partition": "working"}])
        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"grade": {"rebaseline_autorevert": True}})
        res = orch.cmd_rebaseline(s, dry_run=True, adapter=_fake_adapter(), champ_root="/champ",
                                  full_scores_fn=lambda store, **k: _scores(working=0.6),
                                  head_sha_fn=lambda root: "headsha")
        assert res["reverted"] is None                                          # dry-run: no revert
        assert json.loads(s.get_setting("grade.baseline"))["working"] == 0.9    # unchanged
