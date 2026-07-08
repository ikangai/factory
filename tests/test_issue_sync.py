"""Auto issue-sync (design: docs/plans/2026-06-27-factory-auto-issue-sync-design.md).

Pure-logic + injected-runner tests — no real git/gh/network. The whole point of
the design is that `runner` is injectable, so every test drives the code with a
fake git/gh and asserts on the calls it would have made.
"""
import os

import pytest

from factory.common.store import Blackboard
from factory.orchestrator import orchestrator as orch
from factory.reporting import issue_sync


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


# -- parse_issue_refs --------------------------------------------------------
def test_bare_hash_is_a_mention_not_a_close():
    refs = issue_sync.parse_issue_refs("feat(x): do a thing (#41)")
    assert refs["mentions"] == {41}
    assert refs["closes"] == set()


def test_gh_prefixed_ref_is_recognized():
    refs = issue_sync.parse_issue_refs("feat: tool memos (gh#41 slice 1/2)")
    assert refs["mentions"] == {41}
    assert refs["closes"] == set()


def test_close_keywords_mark_a_close():
    for kw in ("closes", "close", "closed", "fixes", "fix", "fixed",
               "resolves", "resolve", "resolved"):
        refs = issue_sync.parse_issue_refs(f"feat: thing\n\n{kw} #7")
        assert refs["closes"] == {7}, kw
        assert refs["mentions"] == set(), kw


def test_close_with_gh_prefix():
    refs = issue_sync.parse_issue_refs("fixes gh#12")
    assert refs["closes"] == {12}


def test_a_closed_number_is_not_also_a_mention():
    refs = issue_sync.parse_issue_refs("fixes #5 and also touches #5")
    assert refs["closes"] == {5}
    assert refs["mentions"] == set()


def test_multiple_distinct_refs():
    refs = issue_sync.parse_issue_refs("feat: x (#40)\n\ncloses #41")
    assert refs["closes"] == {41}
    assert refs["mentions"] == {40}


def test_no_refs():
    refs = issue_sync.parse_issue_refs("chore: tidy up, no issue here")
    assert refs == {"closes": set(), "mentions": set()}


def test_keyword_does_not_match_inside_a_word():
    # 'prefix' must not trigger a close via its 'fix' tail; #9 stays a bare mention.
    refs = issue_sync.parse_issue_refs("refactor: prefix #9 handling")
    assert refs["closes"] == set()
    assert refs["mentions"] == {9}


# -- plan_sync ---------------------------------------------------------------
def _c(sha, subject, body=""):
    return {"sha": sha, "subject": subject, "body": body}


def test_plan_groups_commits_by_issue():
    plan = issue_sync.plan_sync([
        _c("a1", "feat: x (#40)"),
        _c("b2", "test: y (#40)"),
    ])
    assert set(plan) == {40}
    assert [c["sha"] for c in plan[40]["commits"]] == ["a1", "b2"]
    assert plan[40]["action"] == "comment"


def test_plan_close_wins_over_comment_regardless_of_order():
    plan = issue_sync.plan_sync([
        _c("a1", "feat: partial (#41)"),
        _c("b2", "feat: finish it", body="closes #41"),
    ])
    assert plan[41]["action"] == "close"
    assert len(plan[41]["commits"]) == 2


def test_plan_separates_distinct_issues():
    plan = issue_sync.plan_sync([_c("a1", "feat: x (#40)", body="closes #41")])
    assert plan[40]["action"] == "comment"
    assert plan[41]["action"] == "close"


def test_plan_empty_for_no_refs():
    assert issue_sync.plan_sync([_c("a1", "chore: tidy")]) == {}


def test_factory_task_trailer_text_never_closes_an_issue():
    """63035a2 review (Critical 2): Factory-Task trailers carry task-TITLE text (free/
    LLM-authored) — provenance, not intent. A title phrased 'closes #41' must NOT close
    a real issue on graduation."""
    plan = issue_sync.plan_sync(
        [_c("a1", "factory: factory/cand-ab12cd34",
            body="Factory-Task: task-abc: closes #41 memory leak")])
    assert plan == {}                       # no close, no comment — the trailer is inert


def test_non_trailer_body_line_still_closes():
    """Control: a worker's OWN commit-body close keyword (not a trailer line) still syncs."""
    plan = issue_sync.plan_sync(
        [_c("a1", "factory: factory/cand-ab12cd34",
            body="Factory-Task: task-abc: closes #41 memory leak\ncloses #41")])
    assert plan[41]["action"] == "close"


# -- store idempotency -------------------------------------------------------
def test_issue_sync_seen_roundtrip(tmp_path):
    with _store(tmp_path) as s:
        assert s.issue_sync_seen(41, "deadbeef") is False
        s.record_issue_sync(41, "deadbeef", "comment", "http://x/1")
        assert s.issue_sync_seen(41, "deadbeef") is True
        assert s.issue_sync_seen(41, "other") is False     # same issue, new commit
        assert s.issue_sync_seen(40, "deadbeef") is False   # same commit, other issue


def test_record_issue_sync_is_idempotent(tmp_path):
    with _store(tmp_path) as s:
        s.record_issue_sync(7, "abc", "close", "u1")
        s.record_issue_sync(7, "abc", "close", "u1")        # re-record must not raise
        assert s.issue_sync_seen(7, "abc") is True


# -- sync_issues (injected gh runner) ----------------------------------------
class _Run:
    def __init__(self, returncode=0, stdout="https://gh/x"):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _FakeRunner:
    """Records argv of every call; returns a canned CompletedProcess-like result."""
    def __init__(self, returncode=0, stdout="https://gh/x"):
        self.calls = []
        self._rc = returncode
        self._stdout = stdout

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return _Run(self._rc, self._stdout)

    def subcmds(self):
        # the gh subcommand of each call, e.g. 'comment' / 'close'
        return [a[2] for a in self.calls if len(a) > 2 and a[0] == "gh"]


def test_sync_comments_on_a_mention_without_closing(tmp_path):
    with _store(tmp_path) as s:
        r = _FakeRunner(stdout="https://gh/40#c1")
        res = issue_sync.sync_issues("o/r", [_c("a1", "feat: x (#40)")],
                                     store=s, runner=r)
        assert res[0]["issue"] == 40 and res[0]["action"] == "comment"
        assert r.subcmds() == ["comment"]                 # commented, did NOT close
        assert s.issue_sync_seen(40, "a1") is True


def test_sync_closes_on_a_keyword(tmp_path):
    with _store(tmp_path) as s:
        r = _FakeRunner()
        res = issue_sync.sync_issues("o/r", [_c("b2", "done", body="closes #41")],
                                     store=s, runner=r)
        assert res[0]["action"] == "close"
        assert r.subcmds() == ["comment", "close"]        # comment first, then close
        close_call = [a for a in r.calls if a[2] == "close"][0]
        assert "41" in close_call
        assert s.issue_sync_seen(41, "b2") is True


def test_sync_is_idempotent_second_run_skips(tmp_path):
    with _store(tmp_path) as s:
        commits = [_c("a1", "feat: x (#40)")]
        issue_sync.sync_issues("o/r", commits, store=s, runner=_FakeRunner())
        r2 = _FakeRunner()
        res = issue_sync.sync_issues("o/r", commits, store=s, runner=r2)
        assert r2.calls == []                              # nothing re-posted
        assert res[0]["action"] == "skip"


def test_sync_followup_comments_only_about_new_commits(tmp_path):
    with _store(tmp_path) as s:
        issue_sync.sync_issues("o/r", [_c("a1", "feat (#40)")],
                               store=s, runner=_FakeRunner())
        r2 = _FakeRunner()
        issue_sync.sync_issues(
            "o/r", [_c("a1", "feat (#40)"), _c("b2", "more work (#40)")],
            store=s, runner=r2)
        body = r2.calls[0][r2.calls[0].index("--body") + 1]
        assert "b2" in body and "more work" in body       # only the new commit
        assert "a1" not in body


def test_sync_dry_run_posts_and_records_nothing(tmp_path):
    with _store(tmp_path) as s:
        r = _FakeRunner()
        res = issue_sync.sync_issues("o/r", [_c("b2", "done", body="closes #41")],
                                     store=s, runner=r, dry_run=True)
        assert r.calls == []                               # posted nothing
        assert s.issue_sync_seen(41, "b2") is False        # recorded nothing
        assert res[0]["action"] == "close"                 # but reports the plan


def test_sync_gh_failure_is_not_recorded_so_a_retry_reattempts(tmp_path):
    with _store(tmp_path) as s:
        res = issue_sync.sync_issues("o/r", [_c("a1", "feat (#40)")],
                                     store=s, runner=_FakeRunner(returncode=1))
        assert res[0]["action"] == "error"
        assert s.issue_sync_seen(40, "a1") is False        # not recorded → retryable


# -- graduate_and_push (injected git+gh runner) ------------------------------
_US, _RS = "\x1f", "\x1e"


def _log(commits):
    """Render commits in the wire format commits_in_range parses (git log --format)."""
    return "".join(
        f"{c['sha']}{_US}{c['subject']}{_US}{c.get('body', '')}{_RS}\n" for c in commits)


class _GitFake:
    """Dispatches injected git/gh calls by argv. Records every call for assertions."""
    def __init__(self, *, branch="base", ff_rc=0, push_rc=0, diff_rc=1, fetch_rc=0,
                 old="old", new="new", log=""):
        self.calls = []
        self.branch, self.ff_rc, self.push_rc = branch, ff_rc, push_rc
        self.diff_rc = diff_rc          # `git diff --quiet` exit: 1 = has changes (default), 0 = none
        self.fetch_rc = fetch_rc        # `git fetch` exit: 0 = success (default)
        self.old, self.new, self.log = old, new, log

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        a = argv
        if a[0] == "git":
            sub = a[3] if len(a) > 3 else ""
            if sub == "fetch":
                return _Run(self.fetch_rc, "")
            if sub == "rev-parse" and "--abbrev-ref" in a:
                return _Run(0, self.branch)
            if sub == "rev-parse":
                return _Run(0, self.new if a[-1] == "HEAD" else self.old)
            if sub == "merge":
                return _Run(self.ff_rc, "")
            if sub == "push":
                return _Run(self.push_rc, "")
            if sub == "diff":
                return _Run(self.diff_rc, "")
            if sub == "log":
                return _Run(0, self.log)
        if a[0] == "gh":
            return _Run(0, "https://gh/x")
        return _Run(0, "")

    def git_subcmds(self):
        return [a[3] for a in self.calls if a[0] == "git" and len(a) > 3]


def test_commits_in_range_parses_multiline_bodies():
    body = "line1\nline2\ncloses #5"
    f = _GitFake(log=_log([{"sha": "abc", "subject": "feat: x", "body": body}]))
    commits = issue_sync.commits_in_range("/x", "a..b", runner=f)
    assert commits == [{"sha": "abc", "subject": "feat: x", "body": body}]


def test_graduate_happy_path_ff_push_then_sync(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", log=_log([{"sha": "c1", "subject": "feat (#40)"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f)
        assert res["action"] == "synced"
        assert "merge" in f.git_subcmds() and "push" in f.git_subcmds()
        assert any(a[0] == "gh" and a[2] == "comment" for a in f.calls)
        assert s.issue_sync_seen(40, "c1") is True


def test_graduate_skips_when_not_on_base(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="some-feature")
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f)
        assert res == {"action": "skip", "reason": "not-on-base"}
        assert "push" not in f.git_subcmds()               # never touched the remote


def test_graduate_skips_on_non_fastforward_and_never_forces(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", ff_rc=1)
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f)
        assert res["reason"] == "not-fast-forward"
        assert "push" not in f.git_subcmds()
        flat = [tok for call in f.calls for tok in call]
        assert "--force" not in flat and "-f" not in flat  # NEVER force


def test_graduate_skips_sync_when_push_fails(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", push_rc=1)
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f)
        assert res["reason"] == "push-failed"
        assert not any(a[0] == "gh" for a in f.calls)       # no issue sync after a failed push


def test_graduate_skips_a_whitespace_only_noop_before_pushing(tmp_path):
    """Theme 4: the merge gate only rejects a fully-EMPTY diff, so a whitespace-only 'fix' could
    reach production and keyword-close a real issue. graduate_and_push must refuse to push (and
    to sync issues for) a change that is empty once whitespace is ignored."""
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", diff_rc=0,              # `git diff -w --quiet` → no real change
                     log=_log([{"sha": "c1", "subject": "style (#40)"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f)
        assert res == {"action": "skip", "reason": "no-op"}
        assert "push" not in f.git_subcmds()                # never pushed a no-op to production
        assert not any(a[0] == "gh" for a in f.calls)       # never auto-closed an issue for nothing


def test_graduate_reblocks_push_when_the_integrated_tip_fails_retest(tmp_path):
    """Prod-push quality gate: the per-task merges each tested their own change, but not the
    INTEGRATED tip. A red re-test of the tip skips the push (fail-closed) — nothing reaches prod."""
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", log=_log([{"sha": "c1", "subject": "feat (#40)"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r", store=s,
                                           runner=f, test_fn=lambda root: (False, "2 failed"))
        assert res["action"] == "skip" and res["reason"] == "tests-failed"
        assert res.get("report") == "2 failed"
        assert "push" not in f.git_subcmds()                # never pushed a red tip to prod
        assert not any(a[0] == "gh" for a in f.calls)       # no issue sync after a red gate


def test_graduate_pushes_when_the_tip_passes_retest(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", log=_log([{"sha": "c1", "subject": "feat (#40)"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r", store=s,
                                           runner=f, test_fn=lambda root: (True, "ok"))
        assert res["action"] == "synced" and "push" in f.git_subcmds()


def test_graduate_does_not_retest_a_no_op(tmp_path):
    """The cheap no-op guard runs BEFORE the (expensive) re-test — a no-op never spends a run."""
    with _store(tmp_path) as s:
        ran = {"n": 0}

        def retest(root):
            ran["n"] += 1
            return (True, "")

        f = _GitFake(branch="base", diff_rc=0, log=_log([{"sha": "c1", "subject": "style"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r", store=s,
                                           runner=f, test_fn=retest)
        assert res["reason"] == "no-op" and ran["n"] == 0


def test_graduate_skips_on_stop(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base")
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f, stop_check=lambda: True)
        assert res == {"action": "skip", "reason": "stop"}
        assert f.calls == []                                # STOP halts before any git


def test_graduate_dry_run_mutates_nothing(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base",
                     log=_log([{"sha": "c1", "subject": "done", "body": "closes #41"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f, dry_run=True)
        assert res["action"] == "dry_run"
        assert "merge" not in f.git_subcmds() and "push" not in f.git_subcmds()
        assert not any(a[0] == "gh" for a in f.calls)
        assert s.issue_sync_seen(41, "c1") is False


# -- fetch-before-read (blindspot fix 2026-07-07: stale-ref reads masked a week of drift) --
def test_graduate_fetches_remote_base_before_reading_it(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", log=_log([{"sha": "c1", "subject": "feat (#40)"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f)
        assert res["action"] == "synced"
        fetch_call = ["git", "-C", "/x", "fetch", "origin", "base"]
        remote_rev_parse_call = ["git", "-C", "/x", "rev-parse", "origin/base"]
        assert fetch_call in f.calls and remote_rev_parse_call in f.calls
        assert f.calls.index(fetch_call) < f.calls.index(remote_rev_parse_call)


def test_graduate_skips_on_fetch_failure(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", fetch_rc=1,
                     log=_log([{"sha": "c1", "subject": "feat (#40)"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f)
        assert res == {"action": "skip", "reason": "fetch-failed"}
        assert "merge" not in f.git_subcmds() and "push" not in f.git_subcmds()
        assert not any(a[0] == "gh" for a in f.calls)       # never touched the remote or issues


def test_dry_run_pins_endpoint_shas(tmp_path):
    """Fix 2 (final whole-branch review): the dry-run preview must resolve BOTH endpoint SHAs
    (origin/<base> tip + <auto_branch> tip) so the approval card pins the actual commits, not
    just a count against a constant symbolic range string."""
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", old="deadbee", new="cafef00",
                     log=_log([{"sha": "c1", "subject": "done", "body": "closes #41"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f, dry_run=True)
        assert res["action"] == "dry_run"
        # origin/base and factory/auto both rev-parse through the fake to `old`
        assert res["base_sha"] == "deadbee" and res["tip_sha"] == "deadbee"
        assert ["git", "-C", "/x", "rev-parse", "factory/auto"] in f.calls


def test_dry_run_survives_fetch_failure(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", fetch_rc=1,
                     log=_log([{"sha": "c1", "subject": "done", "body": "closes #41"}]))
        res = issue_sync.graduate_and_push(root="/x", base="base", repo="o/r",
                                           store=s, runner=f, dry_run=True)
        assert res["action"] == "dry_run"
        assert res["fetch_failed"] is True


# -- loop wiring: _graduate_after_shift --------------------------------------
class _Recorder:
    def __init__(self, result=None, raises=False):
        self.calls = []
        self.result = result or {"action": "synced", "n_commits": 1, "synced": []}
        self.raises = raises

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise RuntimeError("boom")
        return self.result


def _gate_off(monkeypatch):
    """autonomy.push_approval: false — reproduces the pre-2026-07-08 behavior (a real
    push, not a proposal) so these tests keep exercising _graduate_after_shift's ORIGINAL
    contract without depending on the live config.yaml default (which is ON)."""
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": False}})


def test_graduate_after_shift_runs_when_real_and_shipped(tmp_path, monkeypatch):
    _gate_off(monkeypatch)
    with _store(tmp_path) as s:
        g = _Recorder()
        res = orch._graduate_after_shift(s, real=True, shipped=2, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "synced"
        assert g.calls[0]["repo"] == "o/r" and g.calls[0]["base"] == "base"
        assert g.calls[0]["root"] == "/x" and g.calls[0]["store"] is s
        assert "dry_run" not in g.calls[0]                     # the REAL call, not a preview


def test_graduate_after_shift_skips_when_not_real(tmp_path):
    with _store(tmp_path) as s:
        g = _Recorder()
        res = orch._graduate_after_shift(s, real=False, shipped=2, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "skip"
        assert g.calls == []


def test_graduate_after_shift_skips_when_nothing_shipped(tmp_path):
    with _store(tmp_path) as s:
        g = _Recorder()
        res = orch._graduate_after_shift(s, real=True, shipped=0, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "skip"
        assert g.calls == []


def test_graduate_after_shift_skips_without_a_repo(tmp_path):
    with _store(tmp_path) as s:
        g = _Recorder()
        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="", root="/x", base="base")
        assert res["action"] == "skip" and res["reason"] == "no-repo"
        assert g.calls == []


def test_graduate_after_shift_swallows_errors_to_protect_the_loop(tmp_path, monkeypatch):
    _gate_off(monkeypatch)
    with _store(tmp_path) as s:
        g = _Recorder(raises=True)
        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "error"        # swallowed, did not propagate


def test_graduate_after_shift_gate_off_skips_benignly_when_lock_busy(tmp_path, monkeypatch):
    """Cross-process push lock (quality review fix 3): another pusher (an Approve click,
    `factory graduate`) holds the repo lock → the gate-off real call is skipped
    'lock-busy' and treated as BENIGN (the push IS happening — elsewhere), never escalated
    through the failure seam."""
    from factory.common import filelock
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": False, "failure_tasks": True}})
    monkeypatch.setattr(filelock, "DEFAULT_TIMEOUT_S", 0.05)
    with _store(tmp_path) as s:
        g = _Recorder()
        with filelock.repo_lock("/x"):                     # the "other pusher"
            res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                             repo="o/r", root="/x", base="base")
        assert res == {"action": "skip", "reason": "lock-busy"}
        assert g.calls == []                               # never pushed under contention
        assert s.list_tasks(status="open") == []           # benign — no failure task


def test_graduate_after_shift_gate_off_real_call_runs_under_the_lock(tmp_path, monkeypatch):
    """The gate-off push actually HOLDS the repo lock while graduate_fn runs — a second
    acquisition from inside the call must see it busy."""
    from factory.common import filelock
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": False}})
    monkeypatch.setattr(filelock, "DEFAULT_TIMEOUT_S", 0.05)
    with _store(tmp_path) as s:
        seen = {}

        def g(**kw):
            try:
                with filelock.repo_lock("/x"):
                    seen["held"] = False                   # acquired → the caller did NOT hold it
            except filelock.LockBusyError:
                seen["held"] = True
            return {"action": "synced", "n_commits": 1, "synced": []}

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "synced"
        assert seen["held"] is True


# -- push_approval gate: proposes instead of pushing when ON -----------------
def _gate_on(monkeypatch):
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": True}})


def test_graduate_after_shift_proposes_instead_of_pushing_when_gate_on(tmp_path, monkeypatch):
    """The correctness-critical assertion: with the gate ON, the injected graduate_fn is
    called ONLY with dry_run=True — never for a real push — and the row lands in
    pending_approvals rather than anything reaching origin."""
    _gate_on(monkeypatch)
    with _store(tmp_path) as s:
        g = _Recorder(result={"action": "dry_run", "range": "a..b", "n_commits": 3,
                              "synced": [{"issue": 9, "action": "comment", "commits": ["c1"]}]})
        res = orch._graduate_after_shift(s, real=True, shipped=2, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "proposed"
        assert res["n_commits"] == 3
        assert isinstance(res["approval_id"], int)
        # exactly one call, and it was a preview — never a real push
        assert len(g.calls) == 1
        assert g.calls[0]["dry_run"] is True
        assert g.calls[0]["repo"] == "o/r" and g.calls[0]["base"] == "base"
        rows = s.pending_approvals()
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "graduation" and row["status"] == "pending"
        assert row["payload"]["range"] == "a..b" and row["payload"]["n_commits"] == 3
        assert row["payload"]["synced_preview"][0]["issue"] == 9


def test_graduate_after_shift_gate_on_preview_runs_under_the_lock(tmp_path, monkeypatch):
    """Fix 4a (final whole-branch review): the gate-ON dry-run preview FETCHES, and
    execute_approval holds the repo lock across its own re-derivation + push — so a shift-end
    preview must take the SAME lock or it could interleave a fetch with an in-flight Approve.
    A second acquisition from inside the preview must see the lock held."""
    from factory.common import filelock
    _gate_on(monkeypatch)
    monkeypatch.setattr(filelock, "DEFAULT_TIMEOUT_S", 0.05)
    with _store(tmp_path) as s:
        seen = {}

        def g(**kw):
            try:
                with filelock.repo_lock("/x"):
                    seen["held"] = False
            except filelock.LockBusyError:
                seen["held"] = True
            return {"action": "dry_run", "range": "a..b", "n_commits": 1,
                    "base_sha": "b0", "tip_sha": "t0", "synced": []}

        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "proposed"
        assert seen["held"] is True


def test_graduate_after_shift_gate_on_preview_skips_benignly_when_lock_busy(tmp_path, monkeypatch):
    """Another pusher holds the repo lock during a gate-ON shift end → the preview is a benign
    lock-busy skip (not worth queueing behind a push+retest), never escalated, no card filed."""
    from factory.common import filelock
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": True, "failure_tasks": True}})
    monkeypatch.setattr(filelock, "DEFAULT_TIMEOUT_S", 0.05)
    with _store(tmp_path) as s:
        g = _Recorder(result={"action": "dry_run", "n_commits": 1, "synced": []})
        with filelock.repo_lock("/x"):
            res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                             repo="o/r", root="/x", base="base")
        assert res == {"action": "skip", "reason": "lock-busy"}
        assert g.calls == []                               # never previewed under contention
        assert s.pending_approvals() == []
        assert s.list_tasks(status="open") == []           # benign — no failure task


def test_graduate_after_shift_gate_on_zero_commits_is_quiet_no_row(tmp_path, monkeypatch):
    """A dry-run preview with nothing to graduate must not spam an empty approval card."""
    _gate_on(monkeypatch)
    with _store(tmp_path) as s:
        g = _Recorder(result={"action": "dry_run", "range": "a..a", "n_commits": 0, "synced": []})
        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res == {"action": "skip", "reason": "no-op"}
        assert s.pending_approvals() == []


def test_graduate_after_shift_gate_on_abnormal_preview_falls_through_to_escalation(tmp_path, monkeypatch):
    """When the injected graduate_fn ignores dry_run and returns a real skip/action instead
    of 'dry_run', that's treated as an abnormal result and routed through the SAME
    abnormal-skip escalation a real push's skip gets — not silently dropped."""
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": True, "failure_tasks": True}})
    with _store(tmp_path) as s:
        g = _Recorder(result={"action": "skip", "reason": "push-failed"})
        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res == {"action": "skip", "reason": "push-failed"}
        tasks = s.list_tasks(status="open")
        assert len(tasks) == 1
        assert "graduate skipped: push-failed" in tasks[0]["detail"]
        assert s.pending_approvals() == []


# -- Fix 3b: suppress re-proposing a graduation the operator rejected unchanged ----
def test_graduate_after_shift_gate_on_suppresses_identical_rejection(tmp_path, monkeypatch, capsys):
    """Fix 3b (final whole-branch review): a graduation the operator already REJECTED and
    that is UNCHANGED (same count + endpoint SHAs) must not be re-filed next shift — it would
    just nag with the identical card. Depends on Fix 2's pinned SHAs."""
    _gate_on(monkeypatch)
    with _store(tmp_path) as s:
        rej = s.add_pending_approval("graduation",
            {"range": "origin/base..factory/auto", "n_commits": 3, "base_sha": "b0", "tip_sha": "t0"})
        s.resolve_approval(rej, "rejected", note="not yet")
        g = _Recorder(result={"action": "dry_run", "range": "origin/base..factory/auto",
                              "n_commits": 3, "base_sha": "b0", "tip_sha": "t0", "synced": []})
        res = orch._graduate_after_shift(s, real=True, shipped=2, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "skip" and res["reason"] == "rejected-unchanged"
        assert s.pending_approvals() == []                 # NO new pending card
        assert "graduation unchanged since operator rejection" in capsys.readouterr().out


def test_graduate_after_shift_gate_on_reproposes_after_a_new_commit(tmp_path, monkeypatch):
    """A DIFFERENT preview after a rejection (a new commit → the tip moved) proposes normally
    — suppression is scoped to the EXACT rejected endpoints."""
    _gate_on(monkeypatch)
    with _store(tmp_path) as s:
        rej = s.add_pending_approval("graduation",
            {"range": "origin/base..factory/auto", "n_commits": 3, "base_sha": "b0", "tip_sha": "t0"})
        s.resolve_approval(rej, "rejected", note="not yet")
        g = _Recorder(result={"action": "dry_run", "range": "origin/base..factory/auto",
                              "n_commits": 4, "base_sha": "b0", "tip_sha": "t9", "synced": []})
        res = orch._graduate_after_shift(s, real=True, shipped=2, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "proposed"
        assert len(s.pending_approvals()) == 1


# -- cmd_graduate (CLI glue) -------------------------------------------------
def test_cmd_graduate_resolves_config_and_calls_graduate(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        captured = {}

        def fake_grad(**kw):
            captured.update(kw)
            return {"action": "synced", "n_commits": 3, "range": "a..b", "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        monkeypatch.setattr(orch.config, "target_repo_slug", lambda: "o/r")
        monkeypatch.setattr(orch.config, "target_config", lambda: {"base_branch": "basebr"})

        class _Ad:
            def entry(self):
                return ("/troot", "/troot/clive.py")

        monkeypatch.setattr(orch.config, "get_adapter", lambda: _Ad())
        res = orch.cmd_graduate(s, dry_run=True)
        assert res["action"] == "synced"
        assert captured["repo"] == "o/r" and captured["base"] == "basebr"
        assert captured["root"] == "/troot" and captured["dry_run"] is True


def test_cmd_graduate_skips_without_a_repo(tmp_path, monkeypatch):
    with _store(tmp_path) as s:
        monkeypatch.setattr(orch.config, "target_repo_slug", lambda: "")
        assert orch.cmd_graduate(s) is None


class _CmdAd:
    def entry(self):
        return ("/troot", "/troot/clive.py")

    def run_tests(self, cwd, **k):
        return (True, "ok")


def _cmd_graduate_config(monkeypatch, *, push_approval):
    monkeypatch.setattr(orch.config, "target_repo_slug", lambda: "o/r")
    monkeypatch.setattr(orch.config, "target_config", lambda: {"base_branch": "basebr"})
    monkeypatch.setattr(orch.config, "get_adapter", lambda: _CmdAd())
    monkeypatch.setattr(orch.config, "load_config",
                        lambda: {"autonomy": {"push_approval": push_approval}})


def test_cmd_graduate_gate_on_files_approval_and_does_not_push(tmp_path, monkeypatch, capsys):
    """Fix 1 (final whole-branch review): `factory graduate` is reachable by the autonomous
    conductor (Bash + ./bin/factory), so an UNGATED real push let an LLM push to production
    with zero approval. With autonomy.push_approval ON (config default) and no --dry-run,
    cmd_graduate must NOT push — it files a pending approval from the dry-run preview and
    returns the proposed shape (mirrors _graduate_after_shift's gate-ON return)."""
    with _store(tmp_path) as s:
        calls = []

        def fake_grad(**kw):
            calls.append(kw)
            return {"action": "dry_run", "range": "origin/basebr..factory/auto", "n_commits": 4,
                    "base_sha": "b0", "tip_sha": "t0", "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        _cmd_graduate_config(monkeypatch, push_approval=True)
        res = orch.cmd_graduate(s)                              # NO --dry-run
        assert res["action"] == "proposed" and res["n_commits"] == 4
        assert isinstance(res["approval_id"], int)
        assert all(c["dry_run"] is True for c in calls)        # never a real push
        rows = s.pending_approvals()
        assert len(rows) == 1 and rows[0]["kind"] == "graduation"
        assert rows[0]["payload"]["tip_sha"] == "t0"
        out = capsys.readouterr().out
        assert "graduation proposed" in out and "autonomy.push_approval" in out


def test_cmd_graduate_gate_on_zero_commits_files_nothing(tmp_path, monkeypatch):
    """Gate ON but nothing to graduate: no empty approval card, no push."""
    with _store(tmp_path) as s:
        def fake_grad(**kw):
            return {"action": "dry_run", "range": "x..x", "n_commits": 0,
                    "base_sha": "b0", "tip_sha": "b0", "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        _cmd_graduate_config(monkeypatch, push_approval=True)
        res = orch.cmd_graduate(s)
        assert res == {"action": "skip", "reason": "no-op"}
        assert s.pending_approvals() == []


def test_cmd_graduate_gate_off_pushes_for_real(tmp_path, monkeypatch):
    """Gate OFF (config-file override): byte-identical to the pre-fix behavior — a real push,
    no approval filed."""
    with _store(tmp_path) as s:
        calls = []

        def fake_grad(**kw):
            calls.append(kw)
            return {"action": "synced", "n_commits": 2, "range": "a..b", "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        _cmd_graduate_config(monkeypatch, push_approval=False)
        res = orch.cmd_graduate(s)                              # NO --dry-run, gate OFF
        assert res["action"] == "synced"
        assert calls[0].get("dry_run") is False                # a real push
        assert s.pending_approvals() == []


def test_cmd_graduate_dry_run_never_proposes_even_with_gate_on(tmp_path, monkeypatch):
    """An explicit --dry-run stays a pure preview under EITHER gate setting — it never files
    an approval (byte-identical to today's dry-run)."""
    with _store(tmp_path) as s:
        def fake_grad(**kw):
            return {"action": "dry_run", "range": "a..b", "n_commits": 3,
                    "base_sha": "b0", "tip_sha": "t0", "synced": []}

        monkeypatch.setattr(issue_sync, "graduate_and_push", fake_grad)
        _cmd_graduate_config(monkeypatch, push_approval=True)
        res = orch.cmd_graduate(s, dry_run=True)
        assert res["action"] == "dry_run"
        assert s.pending_approvals() == []


# -- graduation_lag (blindspot fix 2026-07-07: passive unpushed-commit counter) --
def test_graduation_lag_counts_unpushed_commits():
    """graduation_lag measures how far the champion has drifted ahead of the last push."""
    def fake_runner(argv, **kw):
        # Expect: ["git", "-C", "/r", "rev-list", "--count", "origin/base..factory/auto"]
        assert argv == ["git", "-C", "/r", "rev-list", "--count", "origin/base..factory/auto"]
        return _Run(returncode=0, stdout="105\n")

    result = issue_sync.graduation_lag(root="/r", base="base", runner=fake_runner)
    assert result == {"ahead": 105}


def test_graduation_lag_missing_ref_is_quiet():
    """graduation_lag returns None+error when a ref is missing, never raises."""
    class _RunWithError:
        def __init__(self):
            self.returncode = 128
            self.stdout = ""
            self.stderr = "fatal: unknown revision origin/base\n"

    def fake_runner(argv, **kw):
        return _RunWithError()

    result = issue_sync.graduation_lag(root="/r", base="base", runner=fake_runner)
    assert result["ahead"] is None
    assert "unknown revision" in result["error"]


# -- promote_to_release (Task 5, 2026-07-08): base→release merge-push, mechanized ---------
class _PromoteFake:
    """Dispatches promote_to_release's git calls by subcommand; records every call (in
    order, full argv) for assertions. Each step's return code is independently scriptable
    so every skip reason is reachable."""
    def __init__(self, *, fetch_rc=0, count="3", worktree_add_rc=0, merge_rc=0, push_rc=0,
                head="newsha"):
        self.calls = []
        self.fetch_rc = fetch_rc
        self.count = count
        self.worktree_add_rc = worktree_add_rc
        self.merge_rc = merge_rc
        self.push_rc = push_rc
        self.head = head

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        a = argv
        sub = a[3] if len(a) > 3 else ""
        if sub == "fetch":
            return _Run(self.fetch_rc, "")
        if sub == "rev-list":
            return _Run(0, self.count)
        if sub == "worktree" and len(a) > 4 and a[4] == "add":
            return _Run(self.worktree_add_rc, "")
        if sub == "worktree":                      # remove / prune — always best-effort ok
            return _Run(0, "")
        if sub == "merge" and "--abort" in a:
            return _Run(0, "")
        if sub == "merge":
            return _Run(self.merge_rc, "")
        if sub == "push":
            return _Run(self.push_rc, "")
        if sub == "rev-parse":
            return _Run(0, self.head)
        return _Run(0, "")

    def git_subcmds(self):
        return [c[3] for c in self.calls if c[0] == "git" and len(c) > 3]


def test_promote_to_release_happy_path(tmp_path):
    f = _PromoteFake(count="3", head="newsha")
    res = issue_sync.promote_to_release(root="/x", base="base", release="main", runner=f)
    assert res == {"action": "promoted", "sha": "newsha", "n_commits": 3}
    assert f.git_subcmds() == ["fetch", "fetch", "rev-list", "worktree", "merge", "push",
                               "rev-parse", "worktree", "worktree"]
    add_call = f.calls[3]
    assert add_call[2] == "/x"                       # worktree add itself runs -C root
    assert add_call[4] == "add" and add_call[5] == "--detach"
    wt = add_call[6]
    assert add_call[7] == "origin/main"
    # merge/push/rev-parse HEAD all run INSIDE the detached temp worktree — never on root
    assert f.calls[4][2] == wt and f.calls[5][2] == wt and f.calls[6][2] == wt
    assert f.calls[4][3:] == ["merge", "--no-ff", "origin/base", "-m",
                              "Merge base into main: factory promotion (approved via human queue)"]
    assert f.calls[5][3:] == ["push", "origin", "HEAD:main"]
    assert f.calls[6][3:] == ["rev-parse", "HEAD"]
    # cleanup (finally) runs with -C root, not the (now-removed) worktree
    assert f.calls[7][2] == "/x" and f.calls[7][3:] == ["worktree", "remove", "--force", wt]
    assert f.calls[8][3:] == ["worktree", "prune"]
    assert not os.path.exists(wt)                    # the real mkdtemp dir is cleaned up


def test_promote_to_release_push_never_forces(tmp_path):
    f = _PromoteFake()
    issue_sync.promote_to_release(root="/x", base="base", release="main", runner=f)
    push_calls = [c for c in f.calls if c[0] == "git" and c[3] == "push"]
    assert len(push_calls) == 1
    assert "--force" not in push_calls[0] and "-f" not in push_calls[0]


def test_promote_to_release_skips_on_fetch_failure(tmp_path):
    f = _PromoteFake(fetch_rc=1)
    res = issue_sync.promote_to_release(root="/x", base="base", release="main", runner=f)
    assert res == {"action": "skip", "reason": "fetch-failed"}
    assert "worktree" not in f.git_subcmds() and "merge" not in f.git_subcmds()
    assert "push" not in f.git_subcmds()


def test_promote_to_release_skips_when_nothing_to_promote(tmp_path):
    f = _PromoteFake(count="0")
    res = issue_sync.promote_to_release(root="/x", base="base", release="main", runner=f)
    assert res == {"action": "skip", "reason": "nothing-to-promote"}
    assert "worktree" not in f.git_subcmds()


def test_promote_to_release_skips_on_unparsable_count(tmp_path):
    """A rev-list failure/garbage count is folded into the same fail-closed skip as a
    genuine zero — never treated as 'infinite commits, promote anyway'."""
    f = _PromoteFake(count="not-a-number")
    res = issue_sync.promote_to_release(root="/x", base="base", release="main", runner=f)
    assert res == {"action": "skip", "reason": "nothing-to-promote"}
    assert "worktree" not in f.git_subcmds()


def test_promote_to_release_skips_on_worktree_add_failure(tmp_path):
    f = _PromoteFake(worktree_add_rc=1)
    res = issue_sync.promote_to_release(root="/x", base="base", release="main", runner=f)
    assert res == {"action": "skip", "reason": "worktree-failed"}
    assert "merge" not in f.git_subcmds() and "push" not in f.git_subcmds()
    # cleanup still attempted (finally) even though the add itself failed
    assert any(c[3:5] == ["worktree", "remove"] for c in f.calls if c[0] == "git")


def test_promote_to_release_aborts_and_skips_on_merge_conflict(tmp_path):
    f = _PromoteFake(merge_rc=1)
    res = issue_sync.promote_to_release(root="/x", base="base", release="main", runner=f)
    assert res == {"action": "skip", "reason": "merge-conflict"}
    merges = [c for c in f.calls if c[0] == "git" and c[3] == "merge"]
    assert len(merges) == 2 and merges[1][4] == "--abort"      # attempted, then aborted
    assert "push" not in f.git_subcmds()
    assert any(c[3:5] == ["worktree", "remove"] for c in f.calls if c[0] == "git")


def test_promote_to_release_skips_on_push_failure(tmp_path):
    f = _PromoteFake(push_rc=1)
    res = issue_sync.promote_to_release(root="/x", base="base", release="main", runner=f)
    assert res == {"action": "skip", "reason": "push-failed"}
    assert any(c[3:5] == ["worktree", "remove"] for c in f.calls if c[0] == "git")


def test_promote_to_release_prunes_after_the_dir_is_gone(tmp_path):
    """Cleanup ordering (quality review, minor 4): rmtree BEFORE the final prune, so that
    even when `worktree remove --force` fails, the directory is already gone by prune time
    and prune can drop the dangling .git/worktrees metadata (pruning first would leave it
    behind forever)."""
    observed = {}

    class _F(_PromoteFake):
        def __call__(self, argv, **kw):
            if list(argv[3:5]) == ["worktree", "prune"]:
                wt = next(c[6] for c in self.calls
                          if c[3:6] == ["worktree", "add", "--detach"])
                observed["exists_at_prune"] = os.path.exists(wt)
            return super().__call__(argv, **kw)

    res = issue_sync.promote_to_release(root="/x", base="base", release="main", runner=_F())
    assert res["action"] == "promoted"
    assert observed["exists_at_prune"] is False            # rmtree already ran
