#!/usr/bin/env python3
"""scripts/drill3_rehearse.py — acceptance drill 3, executable, on a throwaway deployment.

Drill 3 (`docs/plans/2026-08-06-production-hardening-roadmap.md` Part 5): *change the
branch/candidate after approval — execution rejected.* Two boundaries, which fail
differently and are drilled separately here:

  factory half  `reporting/approvals.py: execute_approval` — the consent gate. In force
                whether or not the broker is armed, and the ONLY one in force when
                `autonomy.publication_broker: false`. The card pins range + commit count +
                BOTH endpoint shas; approving re-derives under the repo lock and refuses
                ('preview-stale') when reality moved.
  broker half   `orchestrator/broker.py: verify_envelope` — armed only. The allowlist
                authorizes the destination, live `ls-remote` the base, the operator's pin
                store the CONTENT, the operator's spent ledger kills replays.

WHY A THROWAWAY DEPLOYMENT. The obvious rehearsal — push a commit to the real remote's
base branch to trip the base-moved check, then publish for real to see the happy path —
tests a guard by performing the act the guard exists to prevent, on production. This
repo's standing rule is never to probe a boundary with something that acts on success
(the rule exists because an early boundary probe cleared the live killswitch; drill 4's
refusal steps needed the same correction). Every check drilled here is a property of the
code plus the operator's own allowlist/pins/ledger — none of it needs the real remote:

  - the "remote" is a local bare repo in `--dir`; `remote_url`/`bare_path` come from a
    throwaway allowlist, never from an envelope, so no step can reach GitHub even if a
    guard failed outright;
  - the drill's allowlist entry sets `allow_issue_ops: false` AND every envelope carries
    zero issue actions AND no commit subject references an issue, so `gh` never runs
    (the factory half additionally routes git through a runner that refuses `gh` outright);
  - the real store, the real `~/.factory-broker*`, and the real `STOP` are never read or
    written: everything resolves through `FACTORY_BROKER_*` and an explicit db path.

What it therefore does NOT prove, and what the "Verification checklist" in
`docs/runbooks/publication-broker.md` is for: that a real deployment actually split the
credential (factory user cannot push, operator can), that `~/.factory-broker/` is
unreadable from the factory account, and that both sides resolve the same spool. Those are
deployment properties; this is a code-and-policy drill.

    python3 scripts/drill3_rehearse.py --dir /tmp/drill3 [--half broker|factory|both]

Exits non-zero if any step's outcome differs from what the authority line requires.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import subprocess
import sys
import types

FACTORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(FACTORY_ROOT))

from factory.common.store import Blackboard          # noqa: E402
from factory.reporting import approvals, issue_sync  # noqa: E402
from factory.reporting import envelope as env_mod    # noqa: E402

CLI = os.path.join(FACTORY_ROOT, "bin", "factory")
REPO_SLUG = "drill-local/nonexistent-drill3"   # deliberately not a real GitHub repo


class Drill:
    """Accumulates step outcomes so one failing guard doesn't hide the rest."""

    def __init__(self):
        self.rows = []

    def check(self, what, expected, got, evidence=""):
        ok = expected == got
        self.rows.append({"what": what, "expected": expected, "got": got, "ok": ok,
                          "evidence": evidence})
        print(f"[{'PASS' if ok else 'FAIL'}] {len(self.rows)}. {what}\n"
              f"        expected={expected!r} got={got!r}\n        {evidence}")

    @property
    def failed(self):
        return [r for r in self.rows if not r["ok"]]


def git(*args, cwd=None, check=True):
    p = subprocess.run(["git", "-c", "user.email=drill@local", "-c", "user.name=drill",
                        *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"git {args} failed:\n{p.stdout}\n{p.stderr}")
    return p


# =============================================================== broker half
def _broker_env(d):
    env = dict(os.environ)
    env.update({"FACTORY_BROKER_SPOOL": os.path.join(d, "spool"),
                "FACTORY_BROKER_BARE": os.path.join(d, "publish.git"),
                "FACTORY_BROKER_ALLOWLIST": os.path.join(d, "allowlist.yaml"),
                "FACTORY_BROKER_OPERATOR_DIR": os.path.join(d, "operator")})
    return env


def _status_of(out, nonce):
    """`[broker] <nonce8>: <status>` from the CLI's own stdout. The interactive prompt has
    no trailing newline, so the marker can land mid-line."""
    marker = f"[broker] {nonce[:8]}:"
    for line in out.splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip().split(" ")[0]
    return "(no line)"


def broker_half(d, drill):
    origin = os.path.join(d, "origin.git")      # stands in for the real remote
    bare = os.path.join(d, "publish.git")       # the factory-side local bare spool repo
    work = os.path.join(d, "work")
    spool = os.path.join(d, "spool")
    outbox, receipts = os.path.join(spool, "outbox"), os.path.join(spool, "receipts")
    opdir = os.path.join(d, "operator")
    allowlist = os.path.join(d, "allowlist.yaml")
    env = _broker_env(d)

    def broker(*args, stdin_text=None):
        return subprocess.run([CLI, "broker", *args], capture_output=True, text=True,
                              env=env, input=stdin_text,
                              stdin=None if stdin_text is not None else subprocess.DEVNULL)

    def origin_head():
        out = git("ls-remote", origin, "refs/heads/main").stdout.strip()
        return out.split()[0] if out else ""

    def receipt(nonce):
        return env_mod.read_receipt(receipts, nonce) or {}

    def spent(nonce):
        path = os.path.join(opdir, "spent")
        return os.path.isfile(path) and nonce in open(path, encoding="utf-8").read()

    def pins():
        path = os.path.join(opdir, "pins")
        return set(json.load(open(path, encoding="utf-8"))) if os.path.isfile(path) else set()

    def commit(branch, start, path, text, msg, push_as):
        git("checkout", "-q", "-B", branch, start, cwd=work)
        with open(os.path.join(work, path), "w", encoding="utf-8") as fh:
            fh.write(text)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", msg, cwd=work)
        sha = git("rev-parse", "HEAD", cwd=work).stdout.strip()
        git("push", "-q", bare, f"{sha}:refs/heads/{push_as}", cwd=work)
        git("checkout", "-q", "main", cwd=work)
        return sha

    def put(*, base_sha, tip_sha, base_branch="main", repo_slug=REPO_SLUG, ttl_hours=24.0,
            filename_nonce=None, tamper=None, extra=None):
        """Write `<nonce>.json` + sidecar the way the factory would. `extra` is merged
        BEFORE hashing (a factory that recomputes the hash — the real threat model);
        `tamper` mutates the JSON AFTER (the hand-edit case)."""
        e = env_mod.build_envelope(action="graduate", repo_slug=repo_slug,
                                   base_branch=base_branch, base_sha=base_sha,
                                   tip_sha=tip_sha, range_=f"origin/{base_branch}..factory/auto",
                                   n_commits=1, approval_id=1, policy_hash="drill3",
                                   issue_actions=[], ttl_hours=ttl_hours)
        if extra:
            e.update(extra)
        json_path, hash_path = env_mod.write_envelope(e, outbox)
        if tamper:
            t = dict(e)
            tamper(t)
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(t, fh, sort_keys=True, indent=2)
        if filename_nonce:
            os.replace(json_path, os.path.join(outbox, f"{filename_nonce}.json"))
            os.replace(hash_path, os.path.join(outbox, f"{filename_nonce}.json.sha256"))
            return filename_nonce
        return e["nonce"]

    # -- the throwaway deployment
    os.makedirs(outbox)
    os.makedirs(receipts)
    os.makedirs(opdir, mode=0o700)
    git("init", "-q", "--bare", "-b", "main", origin)
    git("init", "-q", "--bare", "-b", "main", bare)
    git("init", "-q", "-b", "main", work)
    with open(os.path.join(work, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("base\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "base commit", cwd=work)
    git("push", "-q", origin, "main", cwd=work)
    b0 = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    git("push", "-q", bare, f"{b0}:refs/heads/main", cwd=work)
    approved = commit("cand", b0, "feature.py",
                      "def approved():\n    return 'the reviewed change'\n",
                      "the approved candidate", "cand")
    with open(allowlist, "w", encoding="utf-8") as fh:
        fh.write("publications:\n"
                 f"  - repo_slug: {REPO_SLUG}\n"
                 "    base_branch: main\n"
                 f"    remote_url: {origin}\n"
                 f"    bare_path: {bare}\n"
                 "    require_pin: true\n"
                 "    allow_issue_ops: false\n")
    os.chmod(allowlist, 0o600)
    print(f"\n--- broker half ({d}) ---\nbase {b0[:9]}  approved candidate {approved[:9]}\n")

    # 1. the base moved under a correctly-pinned envelope
    n1 = put(base_sha=b0, tip_sha=approved)
    broker("pin", approved, "--note", "drill3: reviewed")
    intruder = commit("intruder", b0, "hotfix.txt", "someone else's commit\n",
                      "an intruding commit on the base", "intruder")
    git("push", "-q", origin, f"{intruder}:refs/heads/main", cwd=work)
    out = broker("run-once", "--unattended").stdout
    drill.check("base moved after approval (correct pin, live base advanced)",
                "rejected", _status_of(out, n1),
                f"{receipt(n1).get('detail','')!r}; the intruding commit is intact: "
                f"{origin_head() == intruder}")

    # 2. the envelope hand-edited after it was written
    cand2 = commit("cand2", intruder, "feature2.py", "def approved2():\n    return 2\n",
                   "candidate 2", "cand2")
    n2 = put(base_sha=intruder, tip_sha=cand2, tamper=lambda e: e.update({"tip_sha": b0}))
    before = origin_head()
    out = broker("run-once", "--unattended").stdout
    drill.check("candidate swapped by hand-editing the envelope (sidecar untouched)",
                "rejected", _status_of(out, n2),
                f"{receipt(n2).get('detail','')!r}; remote unchanged: {before == origin_head()}")

    # 3. the BRANCH swapped — and the envelope even supplies its own destination fields
    n3 = put(base_sha=intruder, tip_sha=cand2, base_branch="release",
             extra={"remote_url": origin, "bare_path": bare})
    before = origin_head()
    out = broker("run-once", "--unattended").stdout
    no_release = not git("ls-remote", origin, "refs/heads/release").stdout.strip()
    drill.check("branch swapped after approval (envelope supplies its own remote_url/bare_path)",
                "rejected", _status_of(out, n3),
                f"{receipt(n3).get('detail','')!r}; no refs/heads/release created: "
                f"{no_release}; remote unchanged: {before == origin_head()}")

    # 4. the REPOSITORY swapped
    n4 = put(base_sha=intruder, tip_sha=cand2, repo_slug="ikangai/clive")
    before = origin_head()
    out = broker("run-once", "--unattended").stdout
    drill.check("repo_slug swapped to a repository the operator never allowlisted",
                "rejected", _status_of(out, n4),
                f"{receipt(n4).get('detail','')!r}; remote unchanged: {before == origin_head()}")

    # 5. the CONTENT swapped for something structurally perfect but unreviewed (CRITICAL-1):
    #    correct live base, fresh nonce, its own recomputed hash, a genuine fast-forward.
    #    Destination + liveness checks alone would have published this.
    unapproved = commit("evil", intruder, "feature2.py",
                        "def approved2():\n    return 'exfiltrate everything'\n",
                        "the candidate nobody approved", "evil")
    n5 = put(base_sha=intruder, tip_sha=unapproved)
    before = origin_head()
    out = broker("run-once", "--unattended").stdout
    drill.check("unreviewed candidate, structurally perfect, unpinned (unattended)",
                "pending", _status_of(out, n5),
                f"remote unchanged: {before == origin_head()}; still queued: "
                f"{n5 in env_mod.list_outbox(outbox)}; no receipt: "
                f"{env_mod.read_receipt(receipts, n5) is None}; unspent: {not spent(n5)}")

    # 6. a pinned tip that would REWRITE the base rather than extend it
    forced = commit("divergent", b0, "rewrite.txt", "history rewritten\n",
                    "a tip that is not a descendant of the live base", "divergent")
    broker("pin", forced, "--note", "drill3: pinned, but still not a fast-forward")
    n6 = put(base_sha=intruder, tip_sha=forced)
    before = origin_head()
    out = broker("run-once", "--unattended").stdout
    drill.check("pinned tip that would rewrite the base (not a fast-forward)",
                "rejected", _status_of(out, n6),
                f"{receipt(n6).get('detail','')!r}; remote unchanged: {before == origin_head()}")

    # 7. filename nonce desynchronized from the envelope's own nonce (replay-guard bypass)
    n7 = put(base_sha=intruder, tip_sha=cand2, filename_nonce="a" * 32)
    out = broker("run-once", "--unattended").stdout
    drill.check("filename nonce desynchronized from the envelope's own nonce",
                "rejected", _status_of(out, n7), f"{receipt(n7).get('detail','')!r}")

    # 8. expiry
    n8 = put(base_sha=intruder, tip_sha=cand2, ttl_hours=-1.0)
    out = broker("run-once", "--unattended").stdout
    drill.check("envelope outlived its TTL before the broker reached it",
                "expired", _status_of(out, n8), f"{receipt(n8).get('detail','')!r}")

    # 9. an INTERACTIVE run where no human can answer (no tty). Nobody-was-there must not
    #    be recorded as "the operator declined" — that spent the nonce and wrote an audit
    #    line no operator ever made (drill 3's own finding, fixed 2026-08-16).
    out = broker("run-once").stdout                       # stdin is /dev/null
    drill.check("interactive run with no tty — the deferred envelope survives it",
                "pending", _status_of(out, n5),
                f"still queued: {n5 in env_mod.list_outbox(outbox)}; unspent: "
                f"{not spent(n5)}; no receipt: "
                f"{env_mod.read_receipt(receipts, n5) is None}; nothing pinned by it: "
                f"{unapproved not in pins()}")

    # 10. the positive control — nothing changed since approval, the operator reads the
    #     operator-DERIVED diff and confirms. NOTE the queue order: an interactive run walks
    #     the WHOLE outbox oldest first and prompts per envelope, so step 5's deferred
    #     envelope is prompt #1. Answering it blind is how a real operator publishes the
    #     wrong thing; the answers below are per-prompt: decline the stale one, confirm the
    #     fresh one.
    n10 = put(base_sha=intruder, tip_sha=cand2,
              extra={"n_commits": 99, "range": "a-lie..another-lie"})
    queue = env_mod.list_outbox(outbox)
    out = broker("run-once", stdin_text="n\ny\n").stdout
    derived = "candidate 2" in out and "feature2.py" in out
    drill.check("positive control — unchanged candidate, interactive confirm",
                "pushed", _status_of(out, n10),
                f"queue order {[n[:8] for n in queue]} (the deferred one first); remote -> "
                f"{origin_head()[:9]} == the approved tip: {origin_head() == cand2}; the "
                f"diff shown was derived from the bare repo, not the envelope: {derived}; "
                f"its 99-commit claim was labeled unverified: {'NOT trusted' in out}")

    # 11/12. replay — and replay with the factory-writable receipt copy deleted
    processed = os.path.join(opdir, "processed")
    for i, drop_receipt in enumerate((False, True)):
        if drop_receipt:
            os.remove(os.path.join(receipts, f"{n10}.receipt.json"))
        for suffix in (".json", ".json.sha256"):
            shutil.copy(os.path.join(processed, f"{n10}{suffix}"),
                        os.path.join(outbox, f"{n10}{suffix}"))
        before = origin_head()
        out = broker("run-once", "--unattended").stdout
        drill.check("the same envelope re-dropped into the outbox"
                    + (" with its receipt deleted" if drop_receipt else " (replay)"),
                    "rejected", _status_of(out, n10),
                    f"{receipt(n10).get('detail','')!r}; remote unchanged: "
                    f"{before == origin_head()}")

    # 13. the decline from step 10's first prompt is terminal
    drill.check("the envelope declined at the interactive prompt is terminal",
                "rejected", receipt(n5).get("status", "(none)"),
                f"{receipt(n5).get('detail','')!r}; spent forever: {spent(n5)}; the "
                f"unreviewed tip was never pinned: {unapproved not in pins()}; it never "
                f"reached the remote: {origin_head() != unapproved}")


# =============================================================== factory half
def factory_half(d, drill):
    torigin = os.path.join(d, "target-origin.git")
    troot = os.path.join(d, "target")
    base, auto = "basebr", "factory/auto"
    gh_attempts = []

    def safe_runner(argv, **kwargs):
        """git for real; `gh` never executes — no drill step may reach GitHub."""
        if argv and argv[0] == "gh":
            gh_attempts.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "blocked by the drill harness")
        return subprocess.run(argv, **kwargs)

    def origin_base():
        out = git("ls-remote", torigin, f"refs/heads/{base}").stdout.strip()
        return out.split()[0] if out else ""

    def commit_on(branch, path, text, msg):
        git("checkout", "-q", branch, cwd=troot)
        with open(os.path.join(troot, path), "w", encoding="utf-8") as fh:
            fh.write(text)
        git("add", "-A", cwd=troot)
        git("commit", "-q", "-m", msg, cwd=troot)
        sha = git("rev-parse", "HEAD", cwd=troot).stdout.strip()
        git("checkout", "-q", base, cwd=troot)
        return sha

    def fake_config(*, publication_broker=False):
        """Config resolution monkeypatched in-process — the tests' own idiom
        (tests/test_approvals_broker.py). The alternative, a real adapter pointed at a
        scratch clone, would drill the adapter rather than the consent gate."""
        approvals.config.target_repo_slug = lambda: REPO_SLUG
        approvals.config.get_adapter = lambda: types.SimpleNamespace(
            entry=lambda: (troot, troot), run_tests=lambda root, **k: (True, "ok"))
        approvals.config.target_config = lambda: {"base_branch": base, "release_branch": "rel"}
        approvals.config.load_config = lambda: {"autonomy": {
            "graduation_retest": False, "publication_broker": publication_broker,
            "broker_spool_root": os.path.join(d, "factory-spool")}}

    graduate = functools.partial(issue_sync.graduate_and_push, runner=safe_runner)
    git("init", "-q", "--bare", "-b", base, torigin)
    git("init", "-q", "-b", base, troot)
    git("remote", "add", "origin", torigin, cwd=troot)
    with open(os.path.join(troot, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("target\n")
    git("add", "-A", cwd=troot)
    git("commit", "-q", "-m", "target base", cwd=troot)
    git("push", "-q", "origin", base, cwd=troot)
    git("branch", auto, cwd=troot)

    store = Blackboard(os.path.join(d, "drill.db"))
    store.init_db()
    fake_config()
    print(f"\n--- factory half ({d}) ---\n")

    def preview():
        return issue_sync.graduate_and_push(root=troot, base=base, repo=REPO_SLUG,
                                            store=store, test_fn=None, dry_run=True,
                                            runner=safe_runner)

    def audit(aid):
        return [r.get("action") for r in store.recent_operator_actions(limit=50)
                if r.get("item_ref") == f"approval-{aid}"]

    try:
        # 1. an extra commit on the candidate after the card was pinned
        commit_on(auto, "work.py", "def one():\n    return 1\n", "candidate work")
        aid = approvals.propose_graduation(store, preview=preview())
        card = dict(store.get_approval(aid)["payload"])
        moved = commit_on(auto, "work.py", "def one():\n    return 'swapped'\n",
                          "an extra commit nobody approved")
        before = origin_base()
        res = approvals.execute_approval(store, aid, graduate_fn=graduate)
        row = store.get_approval(aid)
        drill.check("candidate advanced after approval (an extra commit on factory/auto)",
                    "preview-stale", res.get("error", ""),
                    f"card pinned {card['tip_sha'][:9]}/{card['n_commits']}, reality "
                    f"{moved[:9]}/{res.get('fresh', {}).get('n_commits')}; remote unchanged: "
                    f"{before == origin_base()}; row -> {row['status']!r}, payload refreshed "
                    f"to {row['payload']['tip_sha'][:9]}; audit={audit(aid)}")

        # 2. an AMEND — the same commit count, different content (the count-only blind spot)
        aid2 = approvals.propose_graduation(store, preview=preview())
        card2 = dict(store.get_approval(aid2)["payload"])
        git("checkout", "-q", auto, cwd=troot)
        with open(os.path.join(troot, "work.py"), "w", encoding="utf-8") as fh:
            fh.write("def one():\n    return 'amended in place'\n")
        git("add", "-A", cwd=troot)
        git("commit", "-q", "--amend", "--no-edit", cwd=troot)
        amended = git("rev-parse", "HEAD", cwd=troot).stdout.strip()
        git("checkout", "-q", base, cwd=troot)
        before = origin_base()
        res2 = approvals.execute_approval(store, aid2, graduate_fn=graduate)
        drill.check("candidate AMENDED after approval — same count, different content",
                    "preview-stale", res2.get("error", ""),
                    f"count {card2['n_commits']} -> "
                    f"{res2.get('fresh', {}).get('n_commits')} (unchanged), tip "
                    f"{card2['tip_sha'][:9]} -> {amended[:9]}; remote unchanged: "
                    f"{before == origin_base()}; row -> "
                    f"{store.get_approval(aid2)['status']!r}")

        # 3. the BASE moved under the card (an upstream push)
        aid3 = approvals.propose_graduation(store, preview=preview())
        card3 = dict(store.get_approval(aid3)["payload"])
        other = os.path.join(d, "someone-else")
        git("clone", "-q", torigin, other)
        with open(os.path.join(other, "hotfix.txt"), "w", encoding="utf-8") as fh:
            fh.write("an upstream commit the card never saw\n")
        git("add", "-A", cwd=other)
        git("commit", "-q", "-m", "upstream hotfix", cwd=other)
        git("push", "-q", "origin", base, cwd=other)
        upstream = origin_base()
        res3 = approvals.execute_approval(store, aid3, graduate_fn=graduate)
        drill.check("the base moved after approval (an upstream push)",
                    "preview-stale", res3.get("error", ""),
                    f"card pinned base {card3['base_sha'][:9]}, live base "
                    f"{upstream[:9]}; the upstream commit is intact: "
                    f"{origin_base() == upstream}; row -> "
                    f"{store.get_approval(aid3)['status']!r}")

        # 4. broker ARMED: the refusal must land BEFORE the prepare step — no envelope, no
        #    push to the local bare, nothing for an operator to be asked about later
        fake_config(publication_broker=True)
        git("fetch", "-q", "origin", base, cwd=troot)
        git("checkout", "-q", base, cwd=troot)
        git("reset", "-q", "--hard", f"origin/{base}", cwd=troot)
        git("branch", "-f", auto, base, cwd=troot)
        commit_on(auto, "armed.py", "def armed():\n    return 'reviewed'\n",
                  "the candidate approved while the broker is armed")
        aid4 = approvals.propose_graduation(store, preview=preview())
        card4 = dict(store.get_approval(aid4)["payload"])
        prepared = []
        swapped = commit_on(auto, "armed.py", "def armed():\n    return 'swapped'\n",
                            "swapped while the broker was armed")
        before = origin_base()
        res4 = approvals.execute_approval(
            store, aid4, graduate_fn=graduate,
            prepare_graduate_fn=lambda **kw: prepared.append(kw) or {"action": "prepared"})
        drill.check("broker ARMED, candidate swapped — refused before any envelope exists",
                    "preview-stale", res4.get("error", ""),
                    f"prepare never called: {not prepared}; no outbox created: "
                    f"{not os.path.isdir(os.path.join(d, 'factory-spool', 'outbox'))}; card "
                    f"{card4['tip_sha'][:9]} vs reality {swapped[:9]}; remote unchanged: "
                    f"{before == origin_base()}")

        # 5. the positive control — nothing changed, the push lands
        fake_config(publication_broker=False)
        aid5 = approvals.propose_graduation(store, preview=preview())
        card5 = dict(store.get_approval(aid5)["payload"])
        before = origin_base()
        res5 = approvals.execute_approval(store, aid5, graduate_fn=graduate)
        drill.check("positive control — nothing changed since approval, the push lands",
                    "synced", (res5.get("result") or {}).get("action", res5.get("error", "")),
                    f"remote {before[:9]} -> {origin_base()[:9]} == the approved tip: "
                    f"{origin_base() == card5['tip_sha']}; row -> "
                    f"{store.get_approval(aid5)['status']!r}; audit={audit(aid5)}")

        drill.check("no step invoked gh", [], gh_attempts,
                    "the harness's runner refuses `gh` outright; no commit subject carries "
                    "an issue reference, so the sync plan is empty regardless")
    finally:
        store.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", required=True,
                    help="throwaway directory for the stand-in deployment (recreated)")
    ap.add_argument("--half", choices=["broker", "factory", "both"], default="both")
    args = ap.parse_args()
    d = os.path.abspath(args.dir)
    if os.path.exists(d) and not os.path.isdir(d):
        raise SystemExit(f"--dir must be a directory: {d}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)

    drill = Drill()
    for half, fn in (("factory", factory_half), ("broker", broker_half)):
        if args.half in (half, "both"):
            sub = os.path.join(d, f"{half}-half")
            os.makedirs(sub, exist_ok=True)
            fn(sub, drill)

    print("\n" + "=" * 78)
    print(f"drill 3: {len(drill.rows) - len(drill.failed)}/{len(drill.rows)} steps behaved "
          f"as the authority line requires")
    for r in drill.failed:
        print(f"  FAIL: {r['what']} — expected {r['expected']!r}, got {r['got']!r}")
    with open(os.path.join(d, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(drill.rows, fh, indent=2, default=str)
    print(f"evidence: {os.path.join(d, 'results.json')}")
    return 1 if drill.failed else 0


if __name__ == "__main__":
    sys.exit(main())
