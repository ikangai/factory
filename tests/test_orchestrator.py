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
