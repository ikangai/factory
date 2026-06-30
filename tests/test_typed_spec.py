"""Typed spec column on tasks (GSD integration #2, design:
docs/plans/2026-06-27-gsd-spec-driven-integration.md): the spec is a first-class, persisted,
structured field on tasks — not just folded text — so it survives across shifts."""
import sqlite3

from factory.common.store import Blackboard
from factory.reporting import scope_check


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- store: spec column ------------------------------------------------------
def test_add_task_persists_and_returns_spec(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("task-1", "x", source="human", spec={"target_surface": "a.py", "acceptance": "t"})
        assert s.get_task("task-1")["spec"] == {"target_surface": "a.py", "acceptance": "t"}


def test_add_task_default_spec_is_empty_dict(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("task-1", "x", source="human")
        assert s.get_task("task-1")["spec"] == {}


def test_set_task_spec_updates(tmp_path):
    with _store(tmp_path) as s:
        s.add_task("task-1", "x", source="human")
        s.set_task_spec("task-1", {"target_surface": "b.py"})
        assert s.get_task("task-1")["spec"]["target_surface"] == "b.py"


def test_list_and_in_flight_carry_spec(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "x", source="human", spec={"target_surface": "a.py"})
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        assert s.list_tasks()[0]["spec"]["target_surface"] == "a.py"
        assert s.tasks_in_flight(sh)[0]["spec"]["target_surface"] == "a.py"


def test_migration_adds_spec_json_to_a_preexisting_tasks_table(tmp_path):
    db = str(tmp_path / "old.db")
    c = sqlite3.connect(db)
    c.executescript(                                  # an OLD tasks table without spec_json
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "detail TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, "
        "source_ref TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open', "
        "result TEXT NOT NULL DEFAULT '', shift_id INTEGER, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL);")
    c.commit()
    c.close()
    s = Blackboard(db)
    s.init_db()                                       # must migrate the column in, not crash
    s.add_task("task-1", "x", source="human", spec={"target_surface": "a.py"})
    assert s.get_task("task-1")["spec"] == {"target_surface": "a.py"}
    s.close()


# -- scope_check.prefilter persists the spec it computes ----------------------
def test_prefilter_pass_persists_spec_to_the_task(tmp_path):
    with _store(tmp_path) as s:
        sh = s.start_shift(token_budget=1000)
        s.add_task("task-1", "do x", source="human")
        s.set_task_status("task-1", "in_progress", shift_id=sh)
        judge = lambda t: {"decision": "pass",
                           "spec": {"target_surface": "llm.py", "acceptance": "retry test"}}
        scope_check.prefilter(s, [{"id": "task-1", "title": "do x", "detail": ""}],
                              shift_id=sh, judge=judge)
        assert s.get_task("task-1")["spec"]["target_surface"] == "llm.py"   # durable, not just in-memory
