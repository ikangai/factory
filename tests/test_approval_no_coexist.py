"""F13 (round-2 integration fix, docs/plans/2026-08-06-publication-broker-design.md):
`add_pending_approval`'s supersede-first UPDATE only touches 'pending' rows — a
broker-armed approval can sit 'executing' for a long time (up to
autonomy.envelope_ttl_hours) waiting for the operator's broker. A second proposal filed
in that window used to coexist with the first rather than being refused, and approving
it would prepare a SECOND envelope contesting the SAME base the first one is (or already
has) pushed. `reporting.approvals.propose_graduation`/`propose_publication` now refuse
(return None) while one of that kind is already 'executing'; the orchestrator.py call
sites treat None as a clean skip, never a crash. New file — mirrors
tests/test_approvals.py's own hermetic idiom, that file untouched.
"""
from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch
from factory.reporting import approvals, issue_sync


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- _kind_has_executing / propose_graduation / propose_publication (unit) -----------------
def test_kind_has_executing_false_when_nothing_executing(tmp_path):
    with _store(tmp_path) as s:
        assert approvals._kind_has_executing(s, "graduation") is False


def test_kind_has_executing_true_when_the_same_kind_is_executing(tmp_path):
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        assert s.claim_approval(aid)
        assert approvals._kind_has_executing(s, "graduation") is True
        assert approvals._kind_has_executing(s, "publication") is False   # scoped by kind


def test_propose_graduation_refuses_while_one_is_executing(tmp_path):
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"n_commits": 1, "range": "a..b"})
        assert s.claim_approval(aid)
        got = approvals.propose_graduation(s, preview={"range": "c..d", "n_commits": 5})
        assert got is None
        # the executing row is untouched; no NEW pending row was created
        assert s.pending_approvals(status="pending") == []
        assert s.get_approval(aid)["status"] == "executing"


def test_propose_graduation_succeeds_once_the_executing_row_resolves(tmp_path):
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        assert s.claim_approval(aid)
        assert approvals.propose_graduation(s, preview={"range": "a..b", "n_commits": 2}) is None
        s.resolve_approval(aid, "approved", note="done")
        got = approvals.propose_graduation(s, preview={"range": "a..b", "n_commits": 2})
        assert isinstance(got, int)


def test_propose_publication_refuses_while_one_is_executing(tmp_path):
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("publication", {"ahead": 3, "release": "main"})
        assert s.claim_approval(aid)
        got = approvals.propose_publication(s, ahead=9, release="main")
        assert got is None
        assert s.pending_approvals(status="pending") == []


def test_propose_publication_succeeds_when_nothing_executing(tmp_path):
    with _store(tmp_path) as s:
        got = approvals.propose_publication(s, ahead=3, release="main")
        assert isinstance(got, int)
        row = s.get_approval(got)
        assert row["payload"] == {"ahead": 3, "release": "main"}


# -- orchestrator.py call sites treat None as a clean skip, never a crash ------------------
def test_cmd_graduate_skips_cleanly_when_a_graduation_is_already_executing(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        aid = s.add_pending_approval(
            "graduation", {"range": "origin/basebr..factory/auto", "n_commits": 1})
        assert s.claim_approval(aid)

        monkeypatch.setattr(orch.config, "target_repo_slug", lambda: "o/r")
        monkeypatch.setattr(orch.config, "target_config", lambda: {"base_branch": "basebr"})

        class _Ad:
            def entry(self):
                return ("/troot", "/troot/clive.py")

        monkeypatch.setattr(orch.config, "get_adapter", lambda: _Ad())
        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"autonomy": {"push_approval": True}})

        def fake_grad(**kw):
            return {"action": "dry_run", "range": "origin/basebr..factory/auto",
                   "n_commits": 4, "base_sha": "b0", "tip_sha": "t0", "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        res = orch.cmd_graduate(s)
        assert res == {"action": "skip", "reason": "already-executing"}
        assert s.pending_approvals(status="pending") == []   # no second row filed


def test_graduate_after_shift_skips_cleanly_when_a_graduation_is_already_executing(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 1})
        assert s.claim_approval(aid)
        monkeypatch.setattr(orch.config, "load_config",
                            lambda: {"autonomy": {"push_approval": True,
                                                  "publication_broker": False}})

        def graduate_fn(**kw):
            return {"action": "dry_run", "range": "a..b", "n_commits": 3,
                   "base_sha": "b0", "tip_sha": "t0", "synced": []}

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=graduate_fn,
                                         repo="o/r", root="/x", base="base")
        assert res == {"action": "skip", "reason": "already-executing"}
