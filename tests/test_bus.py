"""common/bus.py: the factory's ONLY programmatic gateway to its vendored coordination bus
(design: docs/plans/2026-07-08-factory-owned-bus-human-queue-design.md §1). Hermetic — every
test drives the REAL vendored CLI (vendor/agora/chat.py) against a per-test tmp bus dir, the
same idiom as tests/test_vendored_bus.py (AGORA_DIR override, AGORA_SOLO_GRACE=0)."""
import re

from factory.common import bus
from factory.common import paths


def _seed_escalation(tmp_path, sender="worker1", session="sess-1",
                      text="@human need a decision on X please"):
    """Seed a REAL @human escalation. Discovery (see report): `chat questions` only
    surfaces a message whose row has a session_id AND whose session has a registered
    agent row (vendor/agora/chat.py:all_open_escalations) — a bare `--from <handle>` with
    no `--session` never opens an escalation, because the CLI has no identity to register
    without one. `common.bus.send()` doesn't expose --session (workers always run with a
    real Claude session), so tests that need a real escalation seed via the internal
    `_run` helper directly, exactly the way a real worker's `chat.py send --session ...`
    would."""
    r = bus._run(["send", "--session", session, "--from", sender, text], bus_dir=str(tmp_path))
    assert r.returncode == 0, r.stderr
    return r


def test_send_then_recent_sees_it(tmp_path):
    assert bus.send("hello vendored bus", frm="tester", bus_dir=str(tmp_path)) is True
    msgs = bus.recent(bus_dir=str(tmp_path))
    assert len(msgs) == 1
    assert msgs[0]["sender"] == "tester"
    assert msgs[0]["text"] == "hello vendored bus"
    assert isinstance(msgs[0]["id"], int)
    assert re.match(r"^\d{2}:\d{2}$", msgs[0]["ts"])


def test_recent_respects_limit_and_order(tmp_path):
    for i in range(3):
        assert bus.send(f"msg {i}", frm="tester", bus_dir=str(tmp_path)) is True
    msgs = bus.recent(n=2, bus_dir=str(tmp_path))
    # `log --limit N` returns the N most recent, oldest-first — the last one is the latest send.
    assert [m["text"] for m in msgs] == ["msg 1", "msg 2"]


def test_human_escalation_appears_in_open_questions(tmp_path):
    _seed_escalation(tmp_path)
    qs = bus.open_questions(bus_dir=str(tmp_path))
    assert len(qs) == 1
    assert qs[0]["sender"] == "worker1"
    assert "need a decision on X" in qs[0]["text"]
    assert isinstance(qs[0]["id"], int)


def test_plain_send_without_human_does_not_escalate(tmp_path):
    assert bus.send("just chatting, nothing urgent", frm="tester", bus_dir=str(tmp_path)) is True
    assert bus.open_questions(bus_dir=str(tmp_path)) == []


def test_answer_clears_open_question(tmp_path):
    _seed_escalation(tmp_path)
    qs = bus.open_questions(bus_dir=str(tmp_path))
    assert len(qs) == 1
    mid = qs[0]["id"]
    assert bus.answer(mid, "go ahead with X", bus_dir=str(tmp_path)) is True
    assert bus.open_questions(bus_dir=str(tmp_path)) == []
    # the answer itself lands on the bus, addressed back to the asker
    msgs = bus.recent(bus_dir=str(tmp_path))
    assert any("go ahead with X" in m["text"] for m in msgs)


def test_who_lists_registered_agent(tmp_path):
    _seed_escalation(tmp_path)  # registers worker1 via --session
    assert bus.who(bus_dir=str(tmp_path)) == ["worker1"]


def test_who_empty_room(tmp_path):
    assert bus.who(bus_dir=str(tmp_path)) == []


def test_bus_db_path_prefers_explicit_bus_dir(tmp_path):
    bus._run(["send", "--from", "tester", "hi"], bus_dir=str(tmp_path))
    assert bus.bus_db_path(bus_dir=str(tmp_path)) == str(tmp_path / "chat.db")


def test_bus_db_path_env_override(tmp_path, monkeypatch):
    bus._run(["send", "--from", "tester", "hi"], bus_dir=str(tmp_path))
    monkeypatch.setenv("AGORA_DIR", str(tmp_path))
    assert bus.bus_db_path() == str(tmp_path / "chat.db")


def test_bus_db_path_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("AGORA_DIR", raising=False)
    monkeypatch.delenv("GROUPCHAT_DIR", raising=False)
    monkeypatch.setattr(paths, "FACTORY_ROOT", str(tmp_path))
    assert bus.bus_db_path() is None


# --------------------------------------------------------------------------------------- #
# Failure paths: the bus is a coordination nicety, never a build dependency. Every public
# function must degrade to its neutral value (never raise) whether the CLI itself fails
# cleanly (bad bus_dir) or the subprocess call never happens at all (a raising runner).
# --------------------------------------------------------------------------------------- #

def test_public_functions_never_raise_on_unwritable_bus_dir(tmp_path):
    bad = tmp_path / "locked"
    bad.mkdir()
    bad.chmod(0o400)   # read-only: chat.py's sqlite connect() cannot create/open chat.db
    try:
        assert bus.send("hi", bus_dir=str(bad)) is False
        assert bus.answer(1, "hi", bus_dir=str(bad)) is False
        assert bus.open_questions(bus_dir=str(bad)) == []
        assert bus.who(bus_dir=str(bad)) == []
        assert bus.recent(bus_dir=str(bad)) == []
    finally:
        bad.chmod(0o700)  # restore so pytest's tmp_path cleanup can remove it


def test_public_functions_never_raise_when_runner_raises(tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("subprocess exploded")

    assert bus.send("hi", bus_dir=str(tmp_path), runner=boom) is False
    assert bus.answer(1, "hi", bus_dir=str(tmp_path), runner=boom) is False
    assert bus.open_questions(bus_dir=str(tmp_path), runner=boom) == []
    assert bus.who(bus_dir=str(tmp_path), runner=boom) == []
    assert bus.recent(bus_dir=str(tmp_path), runner=boom) == []
