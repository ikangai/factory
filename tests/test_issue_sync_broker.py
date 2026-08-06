"""reporting/issue_sync.py — the BROKER-ARMED prepare functions (Component D,
docs/plans/2026-08-06-publication-broker-design.md): `graduate_and_prepare_envelope` /
`promote_and_prepare_envelope` / `_issue_actions_from_sync`. Injected-runner tests — no
real git/gh/network (mirrors tests/test_issue_sync.py's own idiom); real file I/O for the
envelope spool (a tmp_path).

Binding rule 2 (no new credential surface): every git call these functions make is either
a LOCAL ref read (`rev-parse origin/<x>` — never a `fetch`) or a push to a local bare path
— never `git push origin` and never `gh`. `test_prepare_never_fetches_or_calls_gh` proves
this at the argv level, which is the same evidence the design's own acceptance bullet
("test this: env without GH_TOKEN...") asks for.
"""
import os

from factory.common.store import Blackboard
from factory.reporting import envelope, issue_sync


def _store(tmp_path):
    s = Blackboard(str(tmp_path / "f.db"))
    s.init_db()
    return s


def _c(sha, subject, body=""):
    return {"sha": sha, "subject": subject, "body": body}


_US, _RS = "\x1f", "\x1e"


def _log(commits):
    return "".join(
        f"{c['sha']}{_US}{c['subject']}{_US}{c.get('body', '')}{_RS}\n" for c in commits)


class _Run:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class _GitFake:
    """Dispatches injected git calls by argv shape; never calls real git or gh. Tracks the
    worktree path `promote_and_prepare_envelope` creates (via `worktree add`) so a later
    `rev-parse HEAD` scoped to it can answer differently from one scoped to `root`."""
    def __init__(self, *, branch="base", ff_rc=0, diff_rc=1, push_rc=0, init_rc=0,
                worktree_rc=0, merge_no_ff_rc=0, revlist_out="1",
                old="oldsha1", new="newsha1", wt_new="wtsha1", log=""):
        self.calls = []
        self.branch, self.ff_rc, self.diff_rc = branch, ff_rc, diff_rc
        self.push_rc, self.init_rc = push_rc, init_rc
        self.worktree_rc, self.merge_no_ff_rc, self.revlist_out = (
            worktree_rc, merge_no_ff_rc, revlist_out)
        self.old, self.new, self.wt_new = old, new, wt_new
        self.log = log
        self.wt = None

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        a = argv
        if a[0] != "git":
            raise AssertionError(f"unexpected non-git call: {argv!r}")   # NEVER gh
        if a[1] == "init":                              # _ensure_bare_repo (no -C prefix)
            return _Run(self.init_rc, "")
        cwd = a[2]
        sub = a[3] if len(a) > 3 else ""
        if sub == "worktree" and len(a) > 4 and a[4] == "add":
            self.wt = a[6]
            return _Run(self.worktree_rc, "")
        if sub == "worktree":
            return _Run(0, "")
        if sub == "rev-parse" and "--abbrev-ref" in a:
            return _Run(0, self.branch)
        if sub == "rev-parse":
            ref = a[-1]
            if ref == "HEAD":
                return _Run(0, self.wt_new if (self.wt and cwd == self.wt) else self.new)
            # graduate_and_prepare_envelope makes exactly one non-HEAD rev-parse call
            # (`origin/<base>`, -> self.old); promote_and_prepare_envelope makes exactly
            # one too (`origin/<release>`, its ONLY use of rev() — base_ref is never
            # rev-parsed, only diffed/merged) — one call-slot per function, no ambiguity.
            return _Run(0, self.old)
        if sub == "merge":
            return _Run(self.merge_no_ff_rc if "--no-ff" in a else self.ff_rc, "")
        if sub == "diff":
            return _Run(self.diff_rc, "")
        if sub == "log":
            return _Run(0, self.log)
        if sub == "push":
            return _Run(self.push_rc, "")
        if sub == "rev-list":
            return _Run(0, self.revlist_out)
        return _Run(0, "")

    def subcmds(self):
        return [a[3] for a in self.calls if a[0] == "git" and len(a) > 3 and a[1] != "init"]


# -- _issue_actions_from_sync -----------------------------------------------------------
def test_issue_actions_renders_bodies_and_skips_fresh_check(tmp_path):
    with _store(tmp_path) as s:
        acts = issue_sync._issue_actions_from_sync(
            "o/r", [_c("a1", "feat (#40)")], s, runner=_GitFake())
        assert acts == [{"op": "comment", "number": 40, "shas": ["a1"], "body": acts[0]["body"]}]
        assert "a1" in acts[0]["body"]


def test_issue_actions_only_covers_not_yet_synced_commits(tmp_path):
    with _store(tmp_path) as s:
        s.record_issue_sync(40, "a1", "comment", "")
        acts = issue_sync._issue_actions_from_sync(
            "o/r", [_c("a1", "feat (#40)"), _c("b2", "more (#40)")], s, runner=_GitFake())
        assert acts[0]["shas"] == ["b2"]


def test_issue_actions_empty_when_everything_already_synced(tmp_path):
    with _store(tmp_path) as s:
        s.record_issue_sync(40, "a1", "comment", "")
        acts = issue_sync._issue_actions_from_sync(
            "o/r", [_c("a1", "feat (#40)")], s, runner=_GitFake())
        assert acts == []


def test_issue_actions_close_op_for_a_keyword(tmp_path):
    with _store(tmp_path) as s:
        acts = issue_sync._issue_actions_from_sync(
            "o/r", [_c("c1", "feat", body="closes #12")], s, runner=_GitFake())
        assert acts == [{"op": "close", "number": 12, "shas": ["c1"], "body": acts[0]["body"]}]
        assert "Resolved by the autonomous factory" in acts[0]["body"]   # _format_comment's
        assert "c1" in acts[0]["body"]                                  # own rendering (close)


# -- graduate_and_prepare_envelope -------------------------------------------------------
def test_graduate_prepare_happy_path_writes_an_envelope(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", log=_log([_c("c1", "feat", body="closes #12")]))
        spool = str(tmp_path / "spool")
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f, spool_root=spool)
        assert res["action"] == "prepared"
        assert res["base_sha"] == "oldsha1" and res["tip_sha"] == "newsha1"
        env = envelope.read_envelope(
            os.path.join(spool, "outbox", f"{res['nonce']}.json"))
        assert env["action"] == "graduate" and env["repo_slug"] == "o/r"
        assert env["base_sha"] == "oldsha1" and env["tip_sha"] == "newsha1"
        assert env["issue_actions"] == [{"op": "close", "number": 12, "shas": ["c1"],
                                        "body": env["issue_actions"][0]["body"]}]


def test_graduate_prepare_creates_the_local_bare_spool_if_missing(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", log=_log([_c("c1", "feat")]))
        spool = str(tmp_path / "spool")
        issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f, spool_root=spool)
        assert any(a[0] == "git" and a[1] == "init" for a in f.calls)


def test_graduate_prepare_pushes_to_bare_never_to_origin_and_never_calls_gh(tmp_path):
    """Binding rule 2: no new credential surface — this is the exact evidence: every push
    argv targets the local bare path, never a bare 'origin'; the runner also raises on any
    non-git argv, so a stray `gh` call would fail the test outright."""
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", log=_log([_c("c1", "feat (#40)")]))
        spool = str(tmp_path / "spool")
        issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f, spool_root=spool)
        push_calls = [a for a in f.calls if len(a) > 3 and a[3] == "push"]
        assert len(push_calls) == 1
        assert push_calls[0][4] == os.path.join(spool, "clive-publish.git")
        assert "origin" not in push_calls[0]
        assert "fetch" not in f.subcmds()               # no fetch attempted either


def test_graduate_prepare_skips_when_not_on_base(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="other")
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f,
            spool_root=str(tmp_path / "spool"))
        assert res == {"action": "skip", "reason": "not-on-base"}


def test_graduate_prepare_skips_on_non_fastforward_never_forces(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", ff_rc=1)
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f,
            spool_root=str(tmp_path / "spool"))
        assert res["reason"] == "not-fast-forward"
        flat = [tok for call in f.calls for tok in call]
        assert "--force" not in flat and "-f" not in flat


def test_graduate_prepare_skips_a_noop(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", diff_rc=0)
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f,
            spool_root=str(tmp_path / "spool"))
        assert res == {"action": "skip", "reason": "no-op"}
        assert "push" not in f.subcmds()


def test_graduate_prepare_skips_when_retest_fails(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base")
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f,
            test_fn=lambda root: (False, "2 failed"), spool_root=str(tmp_path / "spool"))
        assert res == {"action": "skip", "reason": "tests-failed", "report": "2 failed"}
        assert "push" not in f.subcmds()


def test_graduate_prepare_honors_stop_check(tmp_path):
    with _store(tmp_path) as s:
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=_GitFake(),
            stop_check=lambda: True, spool_root=str(tmp_path / "spool"))
        assert res == {"action": "skip", "reason": "stop"}


def test_graduate_prepare_bare_push_failure(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", push_rc=1)
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f,
            spool_root=str(tmp_path / "spool"))
        assert res["action"] == "skip" and res["reason"] == "bare-push-failed"


def test_graduate_prepare_functions_without_gh_token_env(tmp_path, monkeypatch):
    """Binding rule 2's own acceptance test, verbatim: armed mode prepares an envelope
    with GH_TOKEN entirely absent from the environment."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with _store(tmp_path) as s:
        f = _GitFake(branch="base", log=_log([_c("c1", "feat")]))
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f,
            spool_root=str(tmp_path / "spool"))
        assert res["action"] == "prepared"


def test_graduate_prepare_ttl_and_approval_id_and_policy_hash_flow_into_the_envelope(tmp_path):
    with _store(tmp_path) as s:
        f = _GitFake(branch="base")
        res = issue_sync.graduate_and_prepare_envelope(
            root="/x", base="base", repo="o/r", store=s, runner=f,
            spool_root=str(tmp_path / "spool"), approval_id=42, ttl_hours=2,
            policy_hash="fixed-hash")
        assert res["envelope"]["approval_id"] == 42
        assert res["envelope"]["policy_hash"] == "fixed-hash"
        created = res["envelope"]["created_at"]
        expires = res["envelope"]["expires_at"]
        assert created < expires


# -- promote_and_prepare_envelope --------------------------------------------------------
def test_promote_prepare_happy_path_writes_an_envelope(tmp_path):
    f = _GitFake(revlist_out="3")
    spool = str(tmp_path / "spool")
    res = issue_sync.promote_and_prepare_envelope(
        root="/x", base="base", release="main", repo="o/r", runner=f, spool_root=spool)
    assert res["action"] == "prepared" and res["n_commits"] == 3
    env = envelope.read_envelope(os.path.join(spool, "outbox", f"{res['nonce']}.json"))
    assert env["action"] == "promote" and env["base_branch"] == "main"
    assert env["base_sha"] == "oldsha1" and env["tip_sha"] == "wtsha1"
    assert env["issue_actions"] == []


def test_promote_prepare_pushes_bare_never_origin(tmp_path):
    f = _GitFake(revlist_out="1")
    spool = str(tmp_path / "spool")
    issue_sync.promote_and_prepare_envelope(
        root="/x", base="base", release="main", repo="o/r", runner=f, spool_root=spool)
    push_calls = [a for a in f.calls if len(a) > 3 and a[3] == "push"]
    assert len(push_calls) == 1
    assert push_calls[0][4] == os.path.join(spool, "clive-publish.git")
    assert "fetch" not in f.subcmds()


def test_promote_prepare_skips_nothing_to_promote(tmp_path):
    f = _GitFake(revlist_out="0")
    res = issue_sync.promote_and_prepare_envelope(
        root="/x", base="base", release="main", repo="o/r", runner=f,
        spool_root=str(tmp_path / "spool"))
    assert res == {"action": "skip", "reason": "nothing-to-promote"}


def test_promote_prepare_skips_worktree_failure(tmp_path):
    f = _GitFake(revlist_out="1", worktree_rc=1)
    res = issue_sync.promote_and_prepare_envelope(
        root="/x", base="base", release="main", repo="o/r", runner=f,
        spool_root=str(tmp_path / "spool"))
    assert res == {"action": "skip", "reason": "worktree-failed"}


def test_promote_prepare_skips_merge_conflict(tmp_path):
    f = _GitFake(revlist_out="1", merge_no_ff_rc=1)
    res = issue_sync.promote_and_prepare_envelope(
        root="/x", base="base", release="main", repo="o/r", runner=f,
        spool_root=str(tmp_path / "spool"))
    assert res == {"action": "skip", "reason": "merge-conflict"}
    assert any("--abort" in a for a in f.calls)


def test_promote_prepare_skips_bare_push_failure(tmp_path):
    f = _GitFake(revlist_out="1", push_rc=1)
    res = issue_sync.promote_and_prepare_envelope(
        root="/x", base="base", release="main", repo="o/r", runner=f,
        spool_root=str(tmp_path / "spool"))
    assert res["action"] == "skip" and res["reason"] == "bare-push-failed"


def test_promote_prepare_never_forces(tmp_path):
    """`worktree remove --force` (routine cleanup) is expected — the check is scoped to
    PUSH calls only, so it can't false-positive on that unrelated flag."""
    f = _GitFake(revlist_out="1")
    issue_sync.promote_and_prepare_envelope(
        root="/x", base="base", release="main", repo="o/r", runner=f,
        spool_root=str(tmp_path / "spool"))
    push_calls = [a for a in f.calls if len(a) > 3 and a[3] == "push"]
    assert push_calls and all("--force" not in a and "-f" not in a for a in push_calls)
