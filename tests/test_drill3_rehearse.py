"""scripts/drill3_rehearse.py — the executable form of acceptance drill 3
(docs/plans/2026-08-06-production-hardening-roadmap.md Part 5; procedure + execution record
in docs/runbooks/publication-broker.md).

Runs the REAL harness end-to-end (~4s: real local git repos, the real `bin/factory broker`
CLI, the real `execute_approval` consent gate) so the drill can't rot into a script nobody
runs. `tests/test_broker.py` and `tests/test_approvals_broker.py` are the unit-level proof
of the same guards; this asserts the operator-facing rehearsal still passes as a whole, and
that it still covers the boundaries the roadmap sentence names — a drill that quietly loses
a step would otherwise keep reporting green.
"""
import json
import subprocess
import sys

from factory.common import paths

SCRIPT = paths.factory("scripts", "drill3_rehearse.py")

# The substitution each step drills, keyed by a fragment of its own label. Losing any of
# these means the drill no longer covers "change the branch/candidate after approval".
REQUIRED = [
    "candidate advanced after approval",       # factory half: extra commit
    "AMENDED after approval",                  # factory half: same count, different content
    "the base moved after approval",           # factory half: upstream push
    "broker ARMED, candidate swapped",         # factory half: refused before preparing
    "positive control — nothing changed",      # factory half: the push still lands
    "base moved after approval (correct pin",  # broker half: live ls-remote re-verification
    "hand-editing the envelope",               # broker half: content hash
    "branch swapped after approval",           # broker half: destination authority
    "repo_slug swapped",                       # broker half: destination authority
    "unreviewed candidate, structurally perfect",   # broker half: the pin gate (CRITICAL-1)
    "not a fast-forward",                      # broker half: never a force push
    "nonce desynchronized",                    # broker half: replay-guard identity
    "outlived its TTL",                        # broker half: expiry
    "no tty",                                  # broker half: nobody-there is not a decline
    "interactive confirm",                     # broker half: the operator-derived diff
    "re-dropped into the outbox",              # broker half: replay
    "receipt deleted",                         # broker half: the ledger is the authority
    "declined at the interactive prompt",      # broker half: a decline is terminal
]


def test_drill3_rehearsal_passes_end_to_end(tmp_path):
    d = tmp_path / "drill3"
    p = subprocess.run([sys.executable, SCRIPT, "--dir", str(d)],
                       capture_output=True, text=True, timeout=600)
    rows = json.loads((d / "results.json").read_text())
    failed = [r for r in rows if not r["ok"]]
    assert not failed, f"drill steps failed: {failed}\n{p.stdout[-4000:]}"
    assert p.returncode == 0, p.stdout[-4000:] + p.stderr[-2000:]

    labels = " | ".join(r["what"] for r in rows)
    missing = [frag for frag in REQUIRED if frag not in labels]
    assert not missing, f"the drill no longer covers: {missing}"


def test_drill3_rehearsal_never_touches_the_real_deployment(tmp_path):
    """Every path the harness uses must resolve inside --dir: the drill exists to be
    rehearsed on a live operator machine, so a stray default (the real spool, the real
    ~/.factory-broker, the real store) would rehearse against production."""
    d = tmp_path / "drill3"
    subprocess.run([sys.executable, SCRIPT, "--dir", str(d), "--half", "broker"],
                   capture_output=True, text=True, timeout=600, check=True)
    for name in ("origin.git", "publish.git", "spool/outbox", "spool/receipts",
                 "operator/pins", "operator/spent", "operator/processed", "allowlist.yaml"):
        assert (d / "broker-half" / name).exists(), f"missing under --dir: {name}"

    source = open(SCRIPT, encoding="utf-8").read()
    # The allowlist entry — the only thing that authorizes a destination — must point at
    # the drill's own bare repo, and issue ops must be off in it.
    assert "allow_issue_ops: false" in source
    assert "expanduser" not in source          # no ~/.factory-broker*, no ~/anything
    assert "ikangai/clive" in source           # ...used only as a REJECTED destination
    entry = (d / "broker-half" / "allowlist.yaml").read_text()
    assert str(d) in entry and "github.com" not in entry
