"""Component B (design: docs/plans/2026-08-08-crash-consistency-design.md) — the thin,
fail-soft intent-row wrapping around the merge/auto-revert (orchestrator/code_round.py),
the revert's Factory-Revert trailer (adapters/base.py), the unarmed graduation push and
armed graduation prepare (reporting/issue_sync.py), and the armed-ingestion write-order
swap (reporting/approvals.py).

Hermetic: the `store` fixture (conftest.py) is a tmp-dir, schema-initialized Blackboard;
git-touching tests use REAL local repos under tmp_path (never the network).
"""
from __future__ import annotations

import os
import subprocess

import pytest

from factory.common import store as store_mod
from factory.reporting import approvals, envelope, issue_sync
from factory.orchestrator import code_round


# -- fakes mirroring tests/test_code_round.py's own FakeAdapter --------------

class FakeAdapter:
    def __init__(self, *, tests_passed=True, merge_raises=False, revert_raises=False,
                 cand_tip="CANDTIP"):
        self._tests_passed = tests_passed
        self._merge_raises = merge_raises
        self._revert_raises = revert_raises
        self._cand_tip = cand_tip
        self.calls = []

    def frozen_paths(self):
        return []

    def run_tests(self, repo, **k):
        self.calls.append("run_tests")
        return (self._tests_passed, "report")

    def merge_branch(self, repo, branch, message=None, **k):
        self.calls.append(("merge", branch))
        if self._merge_raises:
            raise RuntimeError("merge conflict (aborted)")
        return "MERGESHA"

    def revert_commit(self, repo, sha):
        self.calls.append(("revert", sha))
        if self._revert_raises:
            raise RuntimeError("revert boom")
        return "REVERTSHA"

    def current_commit(self, repo):
        return self._cand_tip


def _grade(*values):
    it = iter(values)
    return lambda repo: next(it)


def g(working, held_out=0.7):
    return {"working": working, "held_out": held_out, "held_out_measured": True,
            "divergence_alarm": False, "safety_flag": False}


CHAMP = {"working": 0.8, "held_out": 0.7}
CLEAN_DIFF = ("diff --git a/src/clive/feature.py b/src/clive/feature.py\n"
              "--- a/src/clive/feature.py\n+++ b/src/clive/feature.py\n")


def _run(ad, grade_fn, store, *, task_id="task-1", diff=CLEAN_DIFF):
    return code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo="/cand", branch="cand", diff_text=diff,
        champion_scores=CHAMP, grade_fn=grade_fn, label="cand",
        task_id=task_id, db_path=store.db_path)


# -- happy path: begin/complete tracked ---------------------------------------

def test_successful_merge_records_an_applied_operation(store):
    ad = FakeAdapter()
    res = _run(ad, _grade(g(0.85), g(0.85)), store)
    assert res["action"] == "merged" and res["merge_sha"] == "MERGESHA"

    op = store.get_operation_by_key("merge:task-1:CANDTIP")
    assert op is not None
    assert op["status"] == "applied"
    assert op["receipt"] == "MERGESHA"
    assert op["kind"] == "merge"


def test_auto_revert_moves_the_same_row_to_reconciled(store):
    ad = FakeAdapter()
    res = _run(ad, _grade(g(0.85), g(0.6)), store)   # re-baseline reveals a regression
    assert res["action"] == "auto_reverted"

    op = store.get_operation_by_key("merge:task-1:CANDTIP")
    assert op is not None
    assert op["status"] == "reconciled"
    assert op["receipt"] == "MERGESHA"          # untouched — set by complete_operation
    assert "REVERTSHA" in op["detail"]


def test_merge_failure_marks_the_operation_failed(store):
    ad = FakeAdapter(merge_raises=True)
    res = _run(ad, _grade(g(0.85)), store)
    assert res["action"] == "discarded" and res["stage"] == "merge"

    op = store.get_operation_by_key("merge:task-1:CANDTIP")
    assert op is not None
    assert op["status"] == "failed"


def test_revert_failure_marks_the_operation_failed(store):
    ad = FakeAdapter(revert_raises=True)
    res = _run(ad, _grade(g(0.85), g(0.6)), store)
    assert res["action"] == "revert_failed"

    op = store.get_operation_by_key("merge:task-1:CANDTIP")
    assert op["status"] == "failed"


# -- idempotency: a pre-existing applied/reconciled row skips the merge ------

def test_already_applied_operation_skips_the_merge_entirely(store):
    store.begin_operation("merge", "merge:task-1:CANDTIP", tip_sha="CANDTIP",
                          payload={"task_id": "task-1"})
    op_id = store.get_operation_by_key("merge:task-1:CANDTIP")["id"]
    store.complete_operation(op_id, receipt="ALREADY-MERGED-SHA")

    ad = FakeAdapter()
    res = _run(ad, _grade(g(0.85), g(0.85)), store)
    assert res["action"] == "merged"
    assert res["merge_sha"] == "ALREADY-MERGED-SHA"
    assert res.get("idempotent_skip") is True
    assert ("merge", "cand") not in ad.calls        # never actually re-merged


# -- fail-soft: a raising store must never break the merge path --------------

def test_raising_begin_operation_does_not_block_the_merge(store, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("store is on fire")
    monkeypatch.setattr(store_mod.Blackboard, "begin_operation", boom)

    ad = FakeAdapter()
    res = _run(ad, _grade(g(0.85), g(0.85)), store)
    assert res["action"] == "merged" and res["merge_sha"] == "MERGESHA"
    assert ("merge", "cand") in ad.calls


def test_raising_complete_operation_does_not_block_the_merge(store, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("store is on fire")
    monkeypatch.setattr(store_mod.Blackboard, "complete_operation", boom)

    ad = FakeAdapter()
    res = _run(ad, _grade(g(0.85), g(0.85)), store)
    assert res["action"] == "merged" and res["merge_sha"] == "MERGESHA"


def test_raising_set_operation_status_does_not_block_auto_revert(store, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("store is on fire")
    monkeypatch.setattr(store_mod.Blackboard, "set_operation_status", boom)

    ad = FakeAdapter()
    res = _run(ad, _grade(g(0.85), g(0.6)), store)
    assert res["action"] == "auto_reverted"
    assert res["revert_sha"] == "REVERTSHA"


def test_no_db_path_or_task_id_is_a_pure_no_op(store):
    """Every existing caller/test that doesn't thread task_id/db_path must see zero
    behavior change: no operations row is ever created."""
    ad = FakeAdapter()
    res = code_round.run_code_round(
        adapter=ad, main_repo="/main", cand_repo="/cand", branch="cand",
        diff_text=CLEAN_DIFF, champion_scores=CHAMP,
        grade_fn=_grade(g(0.85), g(0.85)), label="cand")
    assert res["action"] == "merged"
    assert store.operations() == []


# -- adapters/base.py: the Factory-Revert trailer -----------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def _new_repo(path):
    import os
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return path


def test_revert_commit_carries_a_factory_revert_trailer(tmp_path):
    from factory.adapters.base import TargetAdapter

    class ConcreteAdapter(TargetAdapter):
        def actuate(self, *a, **k): raise NotImplementedError
        def run(self, *a, **k): raise NotImplementedError
        def parse_session_dirs(self, *a, **k): raise NotImplementedError
        def scrub_env(self, *a, **k): raise NotImplementedError
        def panel_env(self, *a, **k): raise NotImplementedError
        def entry(self, *a, **k): raise NotImplementedError
        def interpreter(self, *a, **k): raise NotImplementedError

    repo = _new_repo(str(tmp_path / "r"))
    (tmp_path / "r" / "a.txt").write_text("good\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "root")
    (tmp_path / "r" / "a.txt").write_text("BAD\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "regression")

    adapter = ConcreteAdapter()
    bad_sha = adapter.current_commit(repo)
    adapter.revert_commit(repo, bad_sha)

    log = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%B"],
                         capture_output=True, text=True, check=True).stdout
    assert f"Factory-Revert: {bad_sha}" in log
    # git's own default revert body survives too — still human-legible.
    assert "This reverts commit" in log


def test_reconciler_can_grep_the_factory_revert_trailer(tmp_path):
    """The whole point of the trailer: git alone (no store) can distinguish "merged then
    reverted" from "never merged" via --grep."""
    from factory.adapters.base import TargetAdapter

    class ConcreteAdapter(TargetAdapter):
        def actuate(self, *a, **k): raise NotImplementedError
        def run(self, *a, **k): raise NotImplementedError
        def parse_session_dirs(self, *a, **k): raise NotImplementedError
        def scrub_env(self, *a, **k): raise NotImplementedError
        def panel_env(self, *a, **k): raise NotImplementedError
        def entry(self, *a, **k): raise NotImplementedError
        def interpreter(self, *a, **k): raise NotImplementedError

    root = _new_repo(str(tmp_path / "r"))
    (tmp_path / "r" / "a.txt").write_text("1\n")
    _git(root, "add", "."); _git(root, "commit", "-qm", "root")
    _git(root, "checkout", "-qb", "feature")
    (tmp_path / "r" / "b.txt").write_text("2\n")
    _git(root, "add", "."); _git(root, "commit", "-qm", "feat")
    _git(root, "checkout", "-q", "-")

    adapter = ConcreteAdapter()
    merge_sha = adapter.merge_branch(root, "feature",
                                     message="factory: cand\n\nFactory-Task: task-1")
    adapter.revert_commit(root, merge_sha)

    landed = subprocess.run(
        ["git", "-C", root, "log", "--grep=Factory-Task: task-1", "--format=%H"],
        capture_output=True, text=True, check=True).stdout.split()
    assert landed == [merge_sha]
    reverted = subprocess.run(
        ["git", "-C", root, "log", f"--grep=Factory-Revert: {merge_sha}", "--format=%H"],
        capture_output=True, text=True, check=True).stdout.split()
    assert len(reverted) == 1   # the merge WAS reverted — findable via git alone


# -- reporting/issue_sync.py: graduate_and_push (unarmed) intent row ---------

_US, _RS = "\x1f", "\x1e"


def _log(commits):
    return "".join(
        f"{c['sha']}{_US}{c['subject']}{_US}{c.get('body', '')}{_RS}\n" for c in commits)


class _Run:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _GradPushFake:
    """Minimal argv-dispatching git+gh fake for graduate_and_push (mirrors
    tests/test_issue_sync.py's own _GitFake)."""
    def __init__(self, *, branch="base", old="oldsha", new="newsha", log=""):
        self.calls = []
        self.branch, self.old, self.new, self.log = branch, old, new, log

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        a = argv
        if a[0] == "git":
            sub = a[3] if len(a) > 3 else ""
            if sub == "fetch":
                return _Run(0, "")
            if sub == "rev-parse" and "--abbrev-ref" in a:
                return _Run(0, self.branch)
            if sub == "rev-parse":
                return _Run(0, self.new if a[-1] == "HEAD" else self.old)
            if sub == "merge":
                return _Run(0, "")
            if sub == "push":
                return _Run(0, "")
            if sub == "diff":
                return _Run(1, "")   # has a real (non-whitespace) change
            if sub == "log":
                return _Run(0, self.log)
        if a[0] == "gh":
            return _Run(0, "https://gh/x")
        return _Run(0, "")


def test_graduate_and_push_records_an_applied_operation_on_success(store):
    f = _GradPushFake(branch="base", log=_log([{"sha": "c1", "subject": "feat"}]))
    res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                       store=store, runner=f)
    assert res["action"] == "synced"

    op = store.get_operation_by_key(f"grad:o/r:{f.old}:{f.new}")
    assert op is not None
    assert op["kind"] == "graduate_push"
    assert op["status"] == "applied"
    assert op["receipt"] == f.new
    assert op["base_sha"] == f.old and op["tip_sha"] == f.new


def test_graduate_and_push_marks_the_operation_failed_on_push_failure(store):
    class _PushFails(_GradPushFake):
        def __call__(self, argv, **kw):
            r = super().__call__(argv, **kw)
            if argv[0] == "git" and len(argv) > 3 and argv[3] == "push":
                return _Run(1, "")
            return r

    f = _PushFails(branch="base", log=_log([{"sha": "c1", "subject": "feat"}]))
    res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                       store=store, runner=f)
    assert res["reason"] == "push-failed"

    op = store.get_operation_by_key(f"grad:o/r:{f.old}:{f.new}")
    assert op is not None
    assert op["status"] == "failed"


def test_graduate_and_push_with_a_raising_store_still_pushes(store, monkeypatch):
    """Fail-soft: a store hiccup must never block the graduation push."""
    def boom(self, *a, **k):
        raise RuntimeError("store is on fire")
    monkeypatch.setattr(store_mod.Blackboard, "begin_operation", boom)

    f = _GradPushFake(branch="base", log=_log([{"sha": "c1", "subject": "feat"}]))
    res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                       store=store, runner=f)
    assert res["action"] == "synced"
    assert any(a[0] == "git" and len(a) > 3 and a[3] == "push" for a in f.calls)


# -- reporting/issue_sync.py: graduate_and_prepare_envelope (armed) ----------

class _GradPrepareFake:
    def __init__(self, *, branch="base", old="oldsha", new="newsha", log=""):
        self.calls = []
        self.branch, self.old, self.new, self.log = branch, old, new, log

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        a = argv
        assert a[0] == "git"                     # never gh — no credential leaves the factory
        sub = a[3] if len(a) > 3 else ""
        if sub == "fetch":
            return _Run(0, "")
        if sub == "rev-parse" and "--abbrev-ref" in a:
            return _Run(0, self.branch)
        if sub == "rev-parse":
            return _Run(0, self.new if a[-1] == "HEAD" else self.old)
        if sub == "merge":
            return _Run(0, "")
        if sub == "diff":
            return _Run(1, "")
        if sub == "log":
            return _Run(0, self.log)
        if sub == "push":
            return _Run(0, "")
        if a[1] == "init":
            return _Run(0, "")
        return _Run(0, "")


def test_graduate_prepare_stamps_the_nonce_before_the_envelope_hits_disk(store, tmp_path):
    """The orphan-envelope-window fix: on_nonce must fire BEFORE write_envelope creates
    the file on disk — proven by checking the file does not exist yet inside the
    callback."""
    spool = str(tmp_path / "spool")
    seen = {}

    def on_nonce(nonce):
        seen["nonce"] = nonce
        outbox = os.path.join(spool, "outbox")
        seen["file_exists_yet"] = os.path.isdir(outbox) and any(
            f.startswith(nonce) for f in os.listdir(outbox))

    f = _GradPrepareFake(branch="base", log=_log([{"sha": "c1", "subject": "feat"}]))
    res = issue_sync.graduate_and_prepare_envelope(
        root="/x", base="base", repo="o/r", store=store, runner=f,
        spool_root=spool, on_nonce=on_nonce)

    assert res["action"] == "prepared"
    assert seen["nonce"] == res["nonce"]
    assert seen["file_exists_yet"] is False       # stamped BEFORE the write
    # ...and the envelope really is on disk by the time we return
    assert os.path.isfile(os.path.join(spool, "outbox", f"{res['nonce']}.json"))


def test_graduate_prepare_records_an_applied_operation_keyed_on_the_nonce(store, tmp_path):
    spool = str(tmp_path / "spool")
    f = _GradPrepareFake(branch="base", log=_log([{"sha": "c1", "subject": "feat"}]))
    res = issue_sync.graduate_and_prepare_envelope(
        root="/x", base="base", repo="o/r", store=store, runner=f, spool_root=spool)

    op = store.get_operation_by_key(f"gradprep:{res['nonce']}")
    assert op is not None
    assert op["kind"] == "graduate_prepare"
    assert op["status"] == "applied"
    assert op["receipt"] == res["nonce"]


def test_graduate_prepare_a_raising_on_nonce_does_not_block_the_write(store, tmp_path):
    spool = str(tmp_path / "spool")

    def boom(nonce):
        raise RuntimeError("payload update is on fire")

    f = _GradPrepareFake(branch="base", log=_log([{"sha": "c1", "subject": "feat"}]))
    res = issue_sync.graduate_and_prepare_envelope(
        root="/x", base="base", repo="o/r", store=store, runner=f,
        spool_root=spool, on_nonce=boom)
    assert res["action"] == "prepared"
    assert os.path.isfile(os.path.join(spool, "outbox", f"{res['nonce']}.json"))


# -- reporting/approvals.py: execute_approval stamps the nonce via on_nonce --

def test_execute_approval_stamps_broker_nonce_before_prepare_fn_writes(tmp_path, monkeypatch):
    """Integration: execute_approval's `_stamp_nonce` closure is what `on_nonce` calls
    when the REAL graduate_and_prepare_envelope is used (not a test double) — verified
    end-to-end via a real prepare_graduate_fn that calls on_nonce itself."""
    import types
    from factory.common.store import Blackboard

    monkeypatch.setattr(approvals.config, "target_repo_slug", lambda: "o/r")
    monkeypatch.setattr(approvals.config, "get_adapter",
                        lambda: types.SimpleNamespace(entry=lambda: ("/troot", "/troot/x")))
    monkeypatch.setattr(approvals.config, "target_config",
                        lambda: {"base_branch": "base", "release_branch": "main"})
    monkeypatch.setattr(approvals.config, "load_config",
                        lambda: {"autonomy": {"publication_broker": True}})

    def graduate_fn(**kw):
        return {"action": "dry_run", "range": "a..b", "n_commits": 1,
               "base_sha": "b0", "tip_sha": "t0", "synced": []}

    def prepare_fn(*, on_nonce=None, **kw):
        if on_nonce:
            on_nonce("nonce-xyz")
        return {"action": "prepared", "nonce": "nonce-xyz"}

    with Blackboard(str(tmp_path / "f.db")) as s:
        s.init_db()
        aid = s.add_pending_approval("graduation", {"range": "a..b", "n_commits": 1,
                                                    "base_sha": "b0", "tip_sha": "t0",
                                                    "synced_preview": []})
        res = approvals.execute_approval(s, aid, graduate_fn=graduate_fn,
                                         prepare_graduate_fn=prepare_fn)
        assert res["ok"] is True
        row = s.get_approval(aid)
        assert row["payload"]["broker_nonce"] == "nonce-xyz"
        assert row["status"] == "executing"


def test_execute_approval_stamps_broker_nonce_on_the_PUBLICATION_path_too(tmp_path, monkeypatch):
    """F6 — Component B wrapped graduation only. The publication branch passed neither
    `store` nor `on_nonce`, so `kind='publication'` kept the exact window the design
    flagged as "NEW — not in the roadmap": the envelope reaches the outbox with no
    `broker_nonce` on the row, the broker pushes it, and `ingest_broker_receipts` (which
    matches only on `payload.broker_nonce`) can never claim the receipt — the row ages out
    to 'stale' while the promotion actually SUCCEEDED."""
    import types
    from factory.common.store import Blackboard

    monkeypatch.setattr(approvals.config, "target_repo_slug", lambda: "o/r")
    monkeypatch.setattr(approvals.config, "get_adapter",
                        lambda: types.SimpleNamespace(entry=lambda: ("/troot", "/troot/x")))
    monkeypatch.setattr(approvals.config, "target_config",
                        lambda: {"base_branch": "base", "release_branch": "main"})
    monkeypatch.setattr(approvals.config, "load_config",
                        lambda: {"autonomy": {"publication_broker": True}})

    seen = {}

    def lag_fn(**kw):
        return {"ahead": 2, "release": "main"}

    def prepare_promote(*, on_nonce=None, store=None, **kw):
        seen["got_on_nonce"] = on_nonce is not None
        seen["got_store"] = store is not None
        if on_nonce:
            on_nonce("pub-nonce-1")          # the REAL prepare fn calls this before writing
        return {"action": "prepared", "nonce": "pub-nonce-1"}

    with Blackboard(str(tmp_path / "f.db")) as s:
        s.init_db()
        aid = s.add_pending_approval("publication", {"ahead": 2, "release": "main"})
        res = approvals.execute_approval(s, aid, lag_fn=lag_fn,
                                         prepare_promote_fn=prepare_promote)
        assert res["ok"] is True
        assert seen == {"got_on_nonce": True, "got_store": True}
        row = s.get_approval(aid)
        assert row["payload"]["broker_nonce"] == "pub-nonce-1", (
            "a publication envelope would be unmatchable to its receipt")
        assert row["status"] == "executing"


def test_promote_prepare_opens_an_intent_row_and_stamps_before_writing(store, tmp_path):
    """F6, the producer half: the publication prepare path must open its own intent row
    and stamp the nonce BEFORE the envelope file exists, exactly as the graduation path
    does — otherwise a crash between write and stamp is unrecoverable by either
    mechanism. Uses the injected `runner` (no real repo needed): git is only asked for
    shas and exit codes here, and the ORDER of our own side effects is the subject."""
    class _R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_git(argv, **kw):
        if "rev-parse" in argv:
            return _R(0, "a" * 40)
        if "rev-list" in argv:
            return _R(0, "3")
        return _R(0, "")

    order = []
    real_write = issue_sync.envelope_mod.write_envelope

    def spy_write(env, outbox):
        order.append("write")
        return real_write(env, outbox)

    issue_sync.envelope_mod.write_envelope = spy_write
    try:
        res = issue_sync.promote_and_prepare_envelope(
            root=str(tmp_path / "root"), base="base", release="main", repo="o/r",
            runner=fake_git, spool_root=str(tmp_path / "spool"), approval_id=7,
            store=store, on_nonce=lambda _n: order.append("stamp"))
    finally:
        issue_sync.envelope_mod.write_envelope = real_write

    assert res["action"] == "prepared"
    assert order == ["stamp", "write"], "the nonce must be durable before the envelope is"
    op = store.get_operation_by_key(f"gradprep:{res['nonce']}")
    assert op is not None and op["status"] == "applied"


# -- reporting/approvals.py: armed-ingestion write-order swap ----------------

def test_ingest_records_synced_issues_before_resolving_the_approval(tmp_path, monkeypatch):
    """The ordering fix itself: a crash between the two writes must leave the row
    'executing' (re-ingestible) with the ledger ALREADY advanced — never the reverse
    (which had no re-ingestion path once resolved)."""
    from factory.common.store import Blackboard

    monkeypatch.setattr(approvals, "_broker_spool_root", lambda: None)

    with Blackboard(str(tmp_path / "f.db")) as s:
        s.init_db()
        aid = s.add_pending_approval("graduation", {"n_commits": 1})
        assert s.claim_approval(aid)
        s.update_approval_payload(aid, {"n_commits": 1, "broker_nonce": "nonceABC"})

        receipts_dir = str(tmp_path / "spool" / "receipts")
        done_dir = str(tmp_path / "spool" / "receipts" / "done")
        issue_results = [{"ok": True, "number": 40, "op": "comment", "shas": ["c1"]}]
        envelope.write_receipt(nonce="nonceABC", status="pushed",
                               receipts_dir=receipts_dir, receipt_sha="cafef00d",
                               issue_results=issue_results)

        # Simulate a crash INSIDE resolve_approval — after _record_synced_issues (called
        # first, per the fix) has already committed.
        original_resolve = s.resolve_approval

        def crash_after_ledger(*a, **k):
            raise RuntimeError("crash mid-resolve")
        monkeypatch.setattr(s, "resolve_approval", crash_after_ledger)

        with pytest.raises(RuntimeError):
            approvals.ingest_broker_receipts(s, receipts_dir=receipts_dir, done_dir=done_dir)

        # The ledger already advanced (recoverable direction) even though the approval
        # row never resolved.
        assert s.issue_sync_seen(40, "c1") is True
        assert s.get_approval(aid)["status"] == "executing"

        # Restore the real resolve_approval and re-run: idempotent re-ingestion still
        # resolves the row correctly (record_issue_sync is itself INSERT OR REPLACE).
        monkeypatch.setattr(s, "resolve_approval", original_resolve)
        envelope.write_receipt(nonce="nonceABC", status="pushed",
                               receipts_dir=receipts_dir, receipt_sha="cafef00d",
                               issue_results=issue_results)
        approvals.ingest_broker_receipts(s, receipts_dir=receipts_dir, done_dir=done_dir)
        assert s.get_approval(aid)["status"] == "approved"
