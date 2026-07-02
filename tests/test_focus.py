"""MISSION.md read/write helpers (research/focus.py). write_mission is the durable steer
seam shared by the board mission editor and the --mission CLI path — it must round-trip
through read_mission and leave the rest of the file byte-identical."""
from factory.research import focus


def test_write_mission_round_trips_through_read(tmp_path):
    p = str(tmp_path / "MISSION.md")
    focus.write_mission(p, "make clive reliable under recovery")
    assert focus.read_mission(p) == "make clive reliable under recovery"


def test_write_mission_replaces_body_and_preserves_other_sections(tmp_path):
    p = tmp_path / "MISSION.md"
    p.write_text("## Mission\nold statement\n\n## Research focus\ntmux agents\n\n"
                 "## Material from the human\n- https://example.com/paper\n")
    focus.write_mission(str(p), "new statement")
    out = p.read_text()
    assert focus.read_mission(str(p)) == "new statement"
    assert "old statement" not in out
    assert "## Research focus\ntmux agents" in out                  # untouched
    assert "https://example.com/paper" in out                       # material preserved


def test_write_mission_appends_section_when_absent(tmp_path):
    p = tmp_path / "MISSION.md"
    p.write_text("# Title\n\n## Research focus\nx\n")
    focus.write_mission(str(p), "steer here")
    assert focus.read_mission(str(p)) == "steer here"
    assert "## Research focus\nx" in p.read_text()                   # existing section kept


def test_write_mission_creates_file_when_missing(tmp_path):
    p = str(tmp_path / "sub" / "MISSION.md")  # parent exists? no — write should still handle a flat path
    flat = str(tmp_path / "MISSION.md")
    focus.write_mission(flat, "fresh mission")
    assert focus.read_mission(flat) == "fresh mission"
