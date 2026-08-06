"""orchestrator/broker.py — Component C of the publication broker
(docs/plans/2026-08-06-publication-broker-design.md). Runs as the OPERATOR.

Hermetic end-to-end: real `git` against real local repos (file-path "remotes" — no
network), so ls-remote/merge-base/cat-file exercise real git semantics; `gh` is the only
faked seam (this machine has no real GitHub). This is the drill-3 acceptance matrix from
the design's own Acceptance section, made executable: base-moved reject, tamper reject,
replayed-nonce reject, expiry reject, happy path.
"""
from datetime import datetime, timedelta, timezone
import subprocess

from factory.orchestrator import broker
from factory.reporting import envelope


def _git(args, cwd, check=True):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30)
    if check:
        assert r.returncode == 0, f"git -C {cwd} {args} failed: {r.stderr}"
    return r.stdout.strip()


def _init_repo(path, branch="base"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True,
                   capture_output=True)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _commit(path, name, msg):
    (path / name).write_text(msg, encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", msg], path)
    return _git(["rev-parse", "HEAD"], path)


class _Runner:
    """Real git via subprocess; `gh` calls are faked (canned rc, recorded for asserts)."""
    def __init__(self, gh_rc=0, gh_out="ok"):
        self.calls = []
        self.gh_rc, self.gh_out = gh_rc, gh_out

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, self.gh_rc, stdout=self.gh_out, stderr="")
        return subprocess.run(argv, **kw)

    def gh_subcmds(self):
        return [a[2] for a in self.calls if a[0] == "gh" and len(a) > 2]


def _rig(tmp_path):
    """A remote 'GitHub' bare repo + a local bare 'spool' repo already carrying a
    candidate tip one commit ahead of the remote's base — the exact state
    graduate_and_prepare_envelope leaves behind. Returns
    (remote_bare, bare_path, work, base_sha, tip_sha, entry)."""
    remote_bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "base", str(remote_bare)],
                   check=True, capture_output=True)

    work = tmp_path / "work"
    _init_repo(work, branch="base")
    _git(["remote", "add", "origin", str(remote_bare)], work)
    base_sha = _commit(work, "f.txt", "first")
    _git(["push", "-q", "origin", "base"], work)

    bare_path = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare_path)], check=True,
                   capture_output=True)
    tip_sha = _commit(work, "f.txt", "second")
    _git(["push", "-q", str(bare_path), "HEAD:refs/heads/base"], work)

    entry = {"repo_slug": "o/r", "remote_url": str(remote_bare), "base_branch": "base",
             "bare_path": str(bare_path), "allow_issue_ops": True}
    return remote_bare, bare_path, work, base_sha, tip_sha, entry


def _env_for(rig, **over):
    _, _, _, base_sha, tip_sha, entry = rig
    kw = dict(action="graduate", repo_slug=entry["repo_slug"], base_branch=entry["base_branch"],
             base_sha=base_sha, tip_sha=tip_sha, range_=f"{base_sha}..{tip_sha}", n_commits=1,
             approval_id=1, policy_hash="ph",
             issue_actions=[{"op": "close", "number": 12, "body": "closes #12"}])
    kw.update(over)
    return envelope.build_envelope(**kw)


# -- load_allowlist / find_entry -----------------------------------------------------------
def test_load_allowlist_missing_file_is_empty(tmp_path):
    assert broker.load_allowlist(str(tmp_path / "nope.yaml")) == []


def test_load_allowlist_malformed_yaml_is_empty(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("publications: [this is not: valid: yaml:", encoding="utf-8")
    assert broker.load_allowlist(str(p)) == []


def test_load_allowlist_parses_publications(tmp_path):
    p = tmp_path / "allow.yaml"
    p.write_text(
        "publications:\n"
        "  - repo_slug: o/r\n"
        "    remote_url: git@github.com:o/r.git\n"
        "    base_branch: base\n"
        "    bare_path: /tmp/bare.git\n"
        "    allow_issue_ops: true\n", encoding="utf-8")
    rows = broker.load_allowlist(str(p))
    assert len(rows) == 1 and rows[0]["repo_slug"] == "o/r"


def test_find_entry_matches_repo_and_branch():
    allow = [{"repo_slug": "o/r", "base_branch": "base"},
            {"repo_slug": "o/r", "base_branch": "main"}]
    got = broker.find_entry(allow, {"repo_slug": "o/r", "base_branch": "main"})
    assert got == allow[1]


def test_find_entry_none_when_no_match():
    assert broker.find_entry([{"repo_slug": "a/b", "base_branch": "x"}],
                             {"repo_slug": "o/r", "base_branch": "base"}) is None


# -- verify_envelope: drill-3 matrix ---------------------------------------------------------
def test_verify_happy_path(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig)
    receipts_dir = tmp_path / "receipts"
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = broker.verify_envelope(env, hash_path=hash_path, receipts_dir=str(receipts_dir),
                               allowlist=[entry], runner=_Runner())
    assert v == {"ok": True, "entry": entry}


def test_verify_rejects_base_moved_since_approval(tmp_path):
    """Drill 3's exact case: the operator (or anyone) pushed something else to the real
    remote's base branch after the envelope was prepared — the pinned base_sha is stale."""
    rig = _rig(tmp_path)
    remote_bare, _, work, _, _, entry = rig
    env = _env_for(rig)
    # someone else moves the remote base branch
    other = tmp_path / "other"
    _init_repo(other, branch="base")
    _git(["remote", "add", "origin", str(remote_bare)], other)
    _git(["fetch", "-q", "origin", "base"], other)
    _git(["checkout", "-q", "base"], other)
    (other / "g.txt").write_text("intruding change", encoding="utf-8")
    _git(["add", "-A"], other)
    _git(["commit", "-q", "-m", "moved"], other)
    _git(["push", "-q", "origin", "base"], other)

    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = broker.verify_envelope(env, hash_path=hash_path, receipts_dir=str(tmp_path / "receipts"),
                               allowlist=[entry], runner=_Runner())
    assert v["ok"] is False and v["status"] == "rejected"
    assert "base moved" in v["reason"]


def test_verify_rejects_tampered_envelope(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    json_path, hash_path = envelope.write_envelope(env, str(outbox))
    tampered = dict(env, n_commits=999)     # the file on disk no longer matches its hash
    import json
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(tampered, fh)
    reread = envelope.read_envelope(json_path)
    v = broker.verify_envelope(reread, hash_path=hash_path, receipts_dir=str(tmp_path / "receipts"),
                               allowlist=[entry], runner=_Runner())
    assert v["ok"] is False and v["status"] == "rejected"
    assert "hash" in v["reason"]


def test_verify_rejects_a_spent_nonce_replay(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig)
    receipts_dir = tmp_path / "receipts"
    envelope.write_receipt(nonce=env["nonce"], status="pushed", receipts_dir=str(receipts_dir))
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = broker.verify_envelope(env, hash_path=hash_path, receipts_dir=str(receipts_dir),
                               allowlist=[entry], runner=_Runner())
    assert v["ok"] is False and v["status"] == "rejected"
    assert "replay" in v["reason"]


def test_verify_rejects_an_expired_envelope(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    past = datetime.now(timezone.utc) - timedelta(hours=48)
    env = _env_for(rig, now=past, ttl_hours=1)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = broker.verify_envelope(env, hash_path=hash_path, receipts_dir=str(tmp_path / "receipts"),
                               allowlist=[entry], runner=_Runner())
    assert v["ok"] is False and v["status"] == "expired"


def test_verify_rejects_no_allowlist_entry(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = broker.verify_envelope(env, hash_path=hash_path, receipts_dir=str(tmp_path / "receipts"),
                               allowlist=[], runner=_Runner())
    assert v["ok"] is False and "allowlist" in v["reason"]


def test_verify_rejects_unreadable_envelope(tmp_path):
    v = broker.verify_envelope(None, hash_path=str(tmp_path / "x.sha256"),
                               receipts_dir=str(tmp_path), allowlist=[], runner=_Runner())
    assert v["ok"] is False and "unreadable" in v["reason"]


def test_verify_rejects_unknown_schema_version(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig)
    env["schema_version"] = 999
    outbox = tmp_path / "outbox"
    # write with the tampered schema so the hash matches what verify will read back
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = broker.verify_envelope(env, hash_path=hash_path, receipts_dir=str(tmp_path / "receipts"),
                               allowlist=[entry], runner=_Runner())
    assert v["ok"] is False and "schema_version" in v["reason"]


def test_verify_rejects_when_tip_sha_absent_from_bare(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig, tip_sha="f" * 40)   # never pushed into the bare repo
    _, _, _, _, _, entry = rig
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = broker.verify_envelope(env, hash_path=hash_path, receipts_dir=str(tmp_path / "receipts"),
                               allowlist=[entry], runner=_Runner())
    assert v["ok"] is False and "not an object" in v["reason"]


def test_verify_never_a_force_push_non_ancestor_rejected(tmp_path):
    """tip_sha that is NOT a descendant of base_sha (e.g. a rewritten/rebased history) must
    reject — the fast-forward-only guard."""
    rig = _rig(tmp_path)
    remote_bare, bare_path, work, base_sha, tip_sha, entry = rig
    # an UNRELATED commit, pushed into the bare repo under a different ref so cat-file
    # finds the object but it shares no ancestry with base_sha
    orphan = tmp_path / "orphan"
    _init_repo(orphan, branch="orphan")
    orphan_sha = _commit(orphan, "o.txt", "unrelated history")
    _git(["push", "-q", str(bare_path), "HEAD:refs/heads/orphan-holder"], orphan)
    env = _env_for(rig, tip_sha=orphan_sha)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = broker.verify_envelope(env, hash_path=hash_path, receipts_dir=str(tmp_path / "receipts"),
                               allowlist=[entry], runner=_Runner())
    assert v["ok"] is False and "fast-forward" in v["reason"]


# -- execute_envelope -----------------------------------------------------------------------
def test_execute_pushes_and_runs_issue_actions(tmp_path):
    rig = _rig(tmp_path)
    remote_bare, _, _, base_sha, tip_sha, entry = rig
    env = _env_for(rig)
    r = _Runner()
    out = broker.execute_envelope(env, entry, runner=r)
    assert out["ok"] is True and out["sha"] == tip_sha
    live = _git(["ls-remote", str(remote_bare), "refs/heads/base"], tmp_path)
    assert live.split()[0] == tip_sha                 # the real remote now has the new tip
    assert r.gh_subcmds() == ["comment", "close"]      # close op = comment then close
    assert out["issue_results"] == [{"number": 12, "op": "close", "ok": True}]


def test_execute_respects_allow_issue_ops_false(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    entry = dict(entry, allow_issue_ops=False)
    env = _env_for(rig)
    r = _Runner()
    out = broker.execute_envelope(env, entry, runner=r)
    assert out["ok"] is True                            # push still happens
    assert r.gh_subcmds() == []                          # but no gh call at all
    assert out["issue_results"][0]["ok"] is False


def test_execute_one_gh_failure_does_not_abort_the_others(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig, issue_actions=[{"op": "comment", "number": 1, "body": "a"},
                                       {"op": "comment", "number": 2, "body": "b"}])
    r = _Runner(gh_rc=1, gh_out="boom")
    out = broker.execute_envelope(env, entry, runner=r)
    assert out["ok"] is True
    assert [ir["ok"] for ir in out["issue_results"]] == [False, False]
    assert len(out["issue_results"]) == 2                # both attempted


def test_execute_push_failure_reports_and_skips_issues(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    entry = dict(entry, remote_url=str(tmp_path / "does-not-exist.git"))
    env = _env_for(rig)
    r = _Runner()
    out = broker.execute_envelope(env, entry, runner=r)
    assert out["ok"] is False and "push failed" in out["detail"]
    assert r.gh_subcmds() == []


# -- run_once ---------------------------------------------------------------------------------
def test_run_once_happy_path_writes_a_pushed_receipt_and_archives_the_outbox(tmp_path):
    rig = _rig(tmp_path)
    remote_bare, _, _, base_sha, tip_sha, entry = rig
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    receipts = tmp_path / "receipts"
    envelope.write_envelope(env, str(outbox))
    allow_path = tmp_path / "allow.yaml"
    allow_path.write_text(
        f"publications:\n  - repo_slug: {entry['repo_slug']}\n"
        f"    remote_url: {entry['remote_url']}\n    base_branch: {entry['base_branch']}\n"
        f"    bare_path: {entry['bare_path']}\n    allow_issue_ops: true\n", encoding="utf-8")
    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=str(allow_path), runner=_Runner())
    assert results == [{"nonce": env["nonce"], "status": "pushed"}]
    receipt = envelope.read_receipt(str(receipts), env["nonce"])
    assert receipt["status"] == "pushed" and receipt["receipt_sha"] == tip_sha
    assert envelope.list_outbox(str(outbox)) == []       # archived out of the live outbox
    assert (outbox / "done" / f"{env['nonce']}.json").exists()


def test_run_once_rejected_envelope_writes_a_rejected_receipt(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig)     # no allowlist at all -> reject
    outbox = tmp_path / "outbox"
    receipts = tmp_path / "receipts"
    envelope.write_envelope(env, str(outbox))
    allow_path = tmp_path / "empty.yaml"
    allow_path.write_text("publications: []\n", encoding="utf-8")
    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=str(allow_path), runner=_Runner())
    assert results == [{"nonce": env["nonce"], "status": "rejected"}]
    receipt = envelope.read_receipt(str(receipts), env["nonce"])
    assert receipt["status"] == "rejected" and "allowlist" in receipt["detail"]


def test_run_once_processes_multiple_envelopes(tmp_path):
    rig1 = _rig(tmp_path / "r1")
    env1 = _env_for(rig1, nonce="nonce-1")
    outbox = tmp_path / "outbox"
    receipts = tmp_path / "receipts"
    envelope.write_envelope(env1, str(outbox))
    allow_path = tmp_path / "allow.yaml"
    entry1 = rig1[5]
    allow_path.write_text(
        f"publications:\n  - repo_slug: {entry1['repo_slug']}\n"
        f"    remote_url: {entry1['remote_url']}\n    base_branch: {entry1['base_branch']}\n"
        f"    bare_path: {entry1['bare_path']}\n    allow_issue_ops: true\n", encoding="utf-8")
    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=str(allow_path), runner=_Runner())
    assert len(results) == 1 and results[0]["status"] == "pushed"


# -- watch ------------------------------------------------------------------------------------
def test_watch_bounded_iterations_calls_run_once_each_time(tmp_path):
    outbox, receipts, allow_path = tmp_path / "outbox", tmp_path / "receipts", tmp_path / "allow.yaml"
    allow_path.write_text("publications: []\n", encoding="utf-8")
    slept = []
    n = broker.watch(outbox_dir=str(outbox), receipts_dir=str(receipts),
                     allowlist_path=str(allow_path), max_iters=3,
                     sleep_fn=lambda s: slept.append(s), poll_s=0.01)
    assert n == 3
    assert slept == [0.01, 0.01]              # sleeps BETWEEN iterations, not after the last


# -- status -----------------------------------------------------------------------------------
def test_status_reports_pending_and_receipts(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig)
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    envelope.write_envelope(env, str(outbox))
    envelope.write_receipt(nonce="other", status="pushed", receipts_dir=str(receipts))
    st = broker.status(outbox_dir=str(outbox), receipts_dir=str(receipts))
    assert st == {"pending": [env["nonce"]], "receipts": ["other"]}
