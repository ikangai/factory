"""The agora collaboration reader (reporting/collab.py): reads the bus (.groupchat/chat.db)
into the who's-working / who-@mentions-whom view the dashboard shows. Hermetic — a temp bus."""
import sqlite3

from factory.reporting import collab


def _make_bus(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, ts TEXT, sender TEXT, "
                "session_id TEXT, kind TEXT, body TEXT, mentions TEXT)")
    con.execute("CREATE TABLE agents (session_id TEXT PRIMARY KEY, handle TEXT, cwd TEXT, "
                "pid INTEGER, status TEXT, first_seen TEXT, last_seen TEXT, last_read_id INT, "
                "squad TEXT, focus TEXT, model TEXT)")
    con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", [
        (1, "2026-06-26T17:02:00Z", "bohr", "s1", "chat", "shift 11 plan", "[]"),
        (2, "2026-06-26T17:03:00Z", "bohr", "s1", "chat", "@lovelace decision for you", '["lovelace"]'),
        (3, "2026-06-26T17:05:00Z", "system", "", "system", "ada joined the room", "[]")])
    con.execute("INSERT INTO agents VALUES ('s1','bohr','/x',99999999,'done','t','t',0,"
                "'factory-conductor','planning the shift','opus')")
    con.commit()
    con.close()


def test_agora_state_reads_messages_agents_and_edges(tmp_path, monkeypatch):
    db = tmp_path / ".groupchat" / "chat.db"
    _make_bus(db)
    monkeypatch.setattr(collab, "agora_db_path", lambda: str(db))
    st = collab.agora_state()
    assert st["active"] and st["total"] == 2 and st["mentions"] == 1   # 2 chat msgs, 1 @mention
    assert st["senders"] == 1                                          # only bohr posted chat
    assert st["edges"] == [{"from": "bohr", "to": "lovelace", "n": 1}]  # who collaborates with whom
    assert [a["handle"] for a in st["agents"]] == ["bohr"]
    assert st["agents"][0]["squad"] == "factory-conductor" and st["agents"][0]["live"] is False
    assert st["messages"][0]["sender"] == "system"                    # newest-first
    assert any(m["to"] == ["lovelace"] for m in st["messages"])       # the @mention is carried


def test_agora_state_graceful_without_bus(monkeypatch):
    monkeypatch.setattr(collab, "agora_db_path", lambda: None)
    st = collab.agora_state()
    assert st == {"active": False, "total": 0, "mentions": 0, "senders": 0,
                  "agents": [], "messages": [], "edges": []}
