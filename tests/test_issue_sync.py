"""Auto issue-sync (design: docs/plans/2026-06-27-factory-auto-issue-sync-design.md).

Pure-logic + injected-runner tests — no real git/gh/network. The whole point of
the design is that `runner` is injectable, so every test drives the code with a
fake git/gh and asserts on the calls it would have made.
"""
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


def test_graduate_after_shift_runs_when_real_and_shipped(tmp_path):
    with _store(tmp_path) as s:
        g = _Recorder()
        res = orch._graduate_after_shift(s, real=True, shipped=2, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "synced"
        assert g.calls[0]["repo"] == "o/r" and g.calls[0]["base"] == "base"
        assert g.calls[0]["root"] == "/x" and g.calls[0]["store"] is s


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


def test_graduate_after_shift_swallows_errors_to_protect_the_loop(tmp_path):
    with _store(tmp_path) as s:
        g = _Recorder(raises=True)
        res = orch._graduate_after_shift(s, real=True, shipped=1, graduate_fn=g,
                                         repo="o/r", root="/x", base="base")
        assert res["action"] == "error"        # swallowed, did not propagate


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
