"""reporting/human_queue.py: derive_human_queue — the operator's actionable work list
(Task 4, docs/plans/2026-07-08-factory-owned-bus-human-queue.md; design: …-design.md §2).
Hermetic — the `store` fixture (tmp-dir Blackboard, conftest.py) covers approvals/blocked
tasks; escalations are seeded against a REAL tmp bus via the vendored CLI, the same idiom
as tests/test_bus.py's `_seed_escalation` (a bare --from with no --session never escalates
— see that module's docstring)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from factory.common import bus
from factory.reporting import human_queue


def _seed_escalation(tmp_path, sender="worker1", session="sess-1",
                      text="@human need a decision on X please"):
    r = bus._run(["send", "--session", session, "--from", sender, text], bus_dir=str(tmp_path))
    assert r.returncode == 0, r.stderr
    return r


NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


# -- empty world --------------------------------------------------------------------------

def test_empty_world_all_counts_zero(store, tmp_path):
    bus_dir = tmp_path / "empty_bus"
    q = human_queue.derive_human_queue(store, bus_dir=str(bus_dir), now=NOW)
    assert q == {"items": [], "counts": {"escalations": 0, "approvals": 0, "blocked": 0,
                                          "total": 0}}


# -- one of each type: shapes + counts + ORDER --------------------------------------------

def test_one_of_each_type_shapes_counts_and_order(store, tmp_path):
    _seed_escalation(tmp_path)

    approval_id = store.add_pending_approval(
        "graduation", {"range": "base..HEAD", "n_commits": 3})

    store.add_task("task-abc", "fix the widget", source="worker")
    store.set_task_status("task-abc", "blocked", result="no_candidate (tests): boom")

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW)

    assert q["counts"] == {"escalations": 1, "approvals": 1, "blocked": 1, "total": 3}
    types = [it["type"] for it in q["items"]]
    assert types == ["escalation", "approval", "blocked"]   # escalations, approvals, blocked

    esc = q["items"][0]
    assert esc["type"] == "escalation"
    assert set(esc.keys()) == {"type", "id", "ts", "sender", "text"}
    assert esc["sender"] == "worker1"
    assert esc["text"] == "@human need a decision on X please"

    appr = q["items"][1]
    assert appr["type"] == "approval"
    assert set(appr.keys()) == {"type", "approval_id", "kind", "summary", "n_commits",
                                 "age_days", "stale"}
    assert appr["approval_id"] == approval_id
    assert appr["kind"] == "graduation"
    assert isinstance(appr["summary"], str) and appr["summary"]
    assert appr["n_commits"] == 3
    assert isinstance(appr["age_days"], float)
    assert appr["stale"] is False

    blk = q["items"][2]
    assert blk["type"] == "blocked"
    assert set(blk.keys()) == {"type", "task_id", "title", "reason", "age_days",
                                "evidence_head"}
    assert blk["task_id"] == "task-abc"
    assert blk["title"] == "fix the widget"
    assert blk["reason"] == "no_candidate (tests): boom"
    assert blk["evidence_head"] == ""   # no task_evidence row was added


def test_multiple_escalations_ordered_oldest_first(store, tmp_path):
    # Bus semantics (tests/test_bus.py): only the room's LEAD's @human survives as a real
    # escalation — a second sender's @human is rewritten to @<lead> ("funnels through the
    # lead"). So two OPEN escalations at once means the SAME session asking twice without
    # being answered in between, not two different senders.
    _seed_escalation(tmp_path, sender="worker1", session="sess-1", text="@human first, oldest")
    _seed_escalation(tmp_path, sender="worker1", session="sess-1", text="@human second, newest")

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW)
    escalations = [it for it in q["items"] if it["type"] == "escalation"]
    assert len(escalations) == 2
    assert escalations[0]["id"] < escalations[1]["id"]
    assert escalations[0]["text"] == "@human first, oldest"


def test_multiple_approvals_ordered_oldest_first(store, tmp_path):
    first = store.add_pending_approval("graduation", {"n_commits": 1})
    # backdate `first` so it is unambiguously the older row despite id ordering alone
    store.conn.execute("UPDATE pending_approvals SET created_at = ? WHERE id = ?",
                        ((NOW - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), first))
    store.conn.commit()
    second = store.add_pending_approval("publication", {"ahead": 2})

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW)
    approvals = [it for it in q["items"] if it["type"] == "approval"]
    assert [a["approval_id"] for a in approvals] == [first, second]


def test_multiple_blocked_ordered_newest_first(store, tmp_path):
    store.add_task("task-old", "old one", source="worker")
    store.set_task_status("task-old", "blocked", result="boom 1")
    store.add_task("task-new", "new one", source="worker")
    store.set_task_status("task-new", "blocked", result="boom 2")

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW)
    blocked = [it for it in q["items"] if it["type"] == "blocked"]
    assert [b["task_id"] for b in blocked] == ["task-new", "task-old"]


# -- approval aging -------------------------------------------------------------------------

def test_approval_aging_computed_against_injected_now(store, tmp_path):
    approval_id = store.add_pending_approval("graduation", {"n_commits": 1})
    backdated = (NOW - timedelta(days=1, hours=12)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    store.conn.execute("UPDATE pending_approvals SET created_at = ? WHERE id = ?",
                        (backdated, approval_id))
    store.conn.commit()

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW,
                                        stale_after_days=3.0)
    appr = next(it for it in q["items"] if it["type"] == "approval")
    assert 1.4 < appr["age_days"] < 1.6
    assert appr["stale"] is False


def test_approval_stale_flips_past_threshold(store, tmp_path):
    approval_id = store.add_pending_approval("graduation", {"n_commits": 1})
    backdated = (NOW - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    store.conn.execute("UPDATE pending_approvals SET created_at = ? WHERE id = ?",
                        (backdated, approval_id))
    store.conn.commit()

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW,
                                        stale_after_days=3.0)
    appr = next(it for it in q["items"] if it["type"] == "approval")
    assert appr["age_days"] > 3.0
    assert appr["stale"] is True


# -- blocked reason collapsed -----------------------------------------------------------

def test_blocked_reason_whitespace_collapsed(store, tmp_path):
    store.add_task("task-multi", "multiline failure", source="worker")
    store.set_task_status("task-multi", "blocked",
                          result="no_candidate (tests):\n  boom\n\twent   wrong  ")

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW)
    blk = next(it for it in q["items"] if it["type"] == "blocked")
    assert blk["reason"] == "no_candidate (tests): boom went wrong"
    assert "\n" not in blk["reason"] and "\t" not in blk["reason"]


def test_blocked_evidence_head_from_task_evidence_when_present(store, tmp_path):
    store.add_task("task-ev", "has evidence", source="worker")
    store.set_task_status("task-ev", "blocked", result="error (tests): boom")
    store.add_task_evidence("task-ev", tests_report="full pytest output " * 30,
                            reply_head="the worker's own words")

    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW)
    blk = next(it for it in q["items"] if it["type"] == "blocked")
    assert blk["evidence_head"] != ""
    assert len(blk["evidence_head"]) <= 300


# -- defensive: evolving/empty payload ---------------------------------------------------

def test_pending_approval_with_empty_payload_summary_is_still_a_string(store, tmp_path):
    store.add_pending_approval("graduation", {})
    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW)
    appr = next(it for it in q["items"] if it["type"] == "approval")
    assert isinstance(appr["summary"], str) and appr["summary"]
    assert appr["n_commits"] is None


def test_pending_publication_approval_summary_and_n_commits(store, tmp_path):
    store.add_pending_approval("publication", {"ahead": 5, "release": "main"})
    q = human_queue.derive_human_queue(store, bus_dir=str(tmp_path), now=NOW)
    appr = next(it for it in q["items"] if it["type"] == "approval")
    assert appr["kind"] == "publication"
    assert "5" in appr["summary"] and "main" in appr["summary"]
    assert appr["n_commits"] == 5


# -- section degradation -------------------------------------------------------------------

def test_bus_dir_missing_degrades_escalations_only(store, tmp_path, capsys):
    store.add_pending_approval("graduation", {"n_commits": 1})
    store.add_task("task-x", "x", source="worker")
    store.set_task_status("task-x", "blocked", result="boom")

    missing_bus = tmp_path / "does-not-exist"
    q = human_queue.derive_human_queue(store, bus_dir=str(missing_bus), now=NOW)

    assert q["counts"]["escalations"] == 0
    assert q["counts"]["approvals"] == 1
    assert q["counts"]["blocked"] == 1
    assert q["counts"]["total"] == 2
