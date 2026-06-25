"""#1: staged research briefs reach the Proposer as GROUNDED direction.

The researcher distils cited technique briefs into RESEARCH_STAGING_DIR, but until
now propose() never read them — the 'research → proposal' arrow was broken (the
briefs only padded the human summary). These tests pin that:

  * propose() injects GROUNDED briefs into the proposer's context slice;
  * it EXCLUDES ungrounded briefs (provenance_warning — citation not among the
    fetched sources) so the proposer is fed only verifiable direction;
  * a brief-cited proposal records its citation on the candidate (provenance).

Hermetic — no LLM, no network: we monkeypatch claude_p to CAPTURE the assembled
prompt (briefs are added to the context BEFORE the call) and return a canned patch.
"""
import os

import yaml

from factory.common.store import Blackboard
from factory.roles import common


def _write_brief(d: str, bid: str, **fields) -> None:
    with open(os.path.join(d, f"{bid}.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"id": bid, "status": "staged", **fields}, fh, sort_keys=False)


def test_propose_injects_grounded_briefs_and_excludes_ungrounded(tmp_path, monkeypatch):
    staging = tmp_path / "rstaging"
    staging.mkdir()
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(staging))
    _write_brief(str(staging), "rb-good",
                 arxiv_id="2606.24820v1", title="SHERLOC",
                 applies_to="observation_policy",
                 suggested_change="GROUNDED_re_read_screen_before_acting",
                 rationale="grounded technique")
    _write_brief(str(staging), "rb-bad",
                 arxiv_id="9999.99999", title="Hallucinated",
                 applies_to="system_prompt",
                 suggested_change="UNGROUNDED_DO_NOT_SHOW",
                 rationale="bad cite",
                 provenance_warning="citation not among fetched papers")

    captured: dict = {}

    def fake_claude_p(prompt, **k):
        captured["prompt"] = prompt
        return "{}", 0, 0.0  # parses to {} → propose returns None (no candidate written)

    monkeypatch.setattr(common, "claude_p", fake_claude_p)

    with Blackboard(str(tmp_path / "f.db")) as store:
        store.init_db()  # no champion row → propose() falls back to paths.CHAMPION_YAML
        out = common.propose(store)

    assert out is None
    prompt = captured["prompt"]
    assert "research_briefs" in prompt                       # the context slice exists
    assert "GROUNDED_re_read_screen_before_acting" in prompt  # grounded brief reached proposer
    assert "UNGROUNDED_DO_NOT_SHOW" not in prompt             # provenance_warning brief excluded


def test_staged_briefs_skips_malformed_yaml_and_keeps_valid(tmp_path, monkeypatch):
    """One corrupt brief (e.g. an operator hand-edit during vetting) must NOT discard
    every valid brief for the round — per-file skip, not all-or-nothing."""
    staging = tmp_path / "rs"
    staging.mkdir()
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(staging))
    _write_brief(str(staging), "rb-ok", applies_to="skills",
                 suggested_change="VALID_BRIEF_KEPT", arxiv_id="1")
    (staging / "rb-bad.yaml").write_text("{ this: is: broken: [unclosed",
                                         encoding="utf-8")

    changes = [b["suggested_change"] for b in common._staged_research_briefs()]
    assert "VALID_BRIEF_KEPT" in changes   # the good brief survives the corrupt one


def test_staged_briefs_survives_deleted_file_race(tmp_path, monkeypatch):
    """A staging file deleted between glob and mtime read must skip only itself,
    not collapse the whole result to []."""
    import os as _os

    staging = tmp_path / "rs"
    staging.mkdir()
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(staging))
    _write_brief(str(staging), "rb-ok", applies_to="skills",
                 suggested_change="SURVIVOR", arxiv_id="1")
    _write_brief(str(staging), "rb-gone", applies_to="skills",
                 suggested_change="RACED_AWAY", arxiv_id="2")

    real_getmtime = _os.path.getmtime

    def flaky(p):
        if str(p).endswith("rb-gone.yaml"):
            raise FileNotFoundError(p)   # simulate the delete race
        return real_getmtime(p)

    monkeypatch.setattr(_os.path, "getmtime", flaky)

    changes = [b["suggested_change"] for b in common._staged_research_briefs()]
    assert "SURVIVOR" in changes   # the racing delete only drops its own file


def test_brief_cited_proposal_records_provenance(tmp_path, monkeypatch):
    staging = tmp_path / "rstaging"
    staging.mkdir()
    monkeypatch.setattr("factory.common.paths.RESEARCH_STAGING_DIR", str(staging))
    cands = tmp_path / "cands"
    cands.mkdir()
    monkeypatch.setattr("factory.common.paths.CANDIDATES_DIR", str(cands))

    reply = ('```json\n{"open_key": "system_prompt", "new_value": "NEW_SYSTEM_PROMPT", '
             '"summary": "tighten observation", "cite": "arxiv:2606.24820v1"}\n```')
    monkeypatch.setattr(common, "claude_p", lambda prompt, **k: (reply, 5, 0.0))

    with Blackboard(str(tmp_path / "f.db")) as store:
        store.init_db()
        cid = common.propose(store)
        assert cid is not None, "a valid one-key change should produce a candidate"
        cand = store.get_candidate(cid)

    assert "2606.24820v1" in cand["change_summary"]  # citation recorded for provenance
