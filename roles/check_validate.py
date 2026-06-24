"""Deterministic backstop for synthesized acceptance checks (factory bug #64).

A check-synth role (an LLM) writes a deterministic acceptance check from a mined
scenario whose ORACLE was itself an LLM guess — the miner miscounted (claimed 15
spoken words; the truth was 14). When the generated check gates the candidate on
that guessed literal FIRST, with the recompute-from-source guard ordered AFTER
it, a CORRECT candidate writing the true answer is failed by the wrong literal
and the guard can never fire (dead code on the failure path). That is a Goodhart
landmine: it would steer the proposer toward the WRONG answer.

Under the recompute-first contract (roles/check-synth/prompt.md) a check derives
`expected` from the seed artifact and returns it in `evidence["expected"]` on
every path. This validator exercises the generated check against two synthetic
end-states built from THAT recomputed value:

  - correct end-state (candidate output == recomputed expected)  MUST pass
  - perturbed end-state (output != expected)                     MUST fail

A check that rejects its own recomputed-correct answer (the bug) or accepts a
wrong one is rejected — caught here, before the human gate, not by relying on it.
Checks we cannot exercise (shell-based, multi-output, or not exposing
`evidence["expected"]`) pass through as UNVERIFIED — we never falsely reject.
"""
from __future__ import annotations

from typing import Any

from ..checks.check_base import CheckContext, CheckResult


class _UsesShell(Exception):
    """Raised when a check shells out — it can't be exercised deterministically."""


class _DictProvider:
    """A CheckContext provider that serves files from a dict and records reads."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.reads: list[str] = []

    def read_file(self, handle: Any, relpath: str):
        self.reads.append(relpath)
        return self.files.get(relpath)

    def run_in_env(self, handle: Any, cmd: str, timeout: int = 60):
        raise _UsesShell()


def _load_acceptance(code: str):
    ns: dict = {}
    exec(compile(code, "<synth-check>", "exec"), ns)  # noqa: S102 — same code load_acceptance runs later
    fn = ns.get("acceptance")
    if not callable(fn):
        raise ValueError("module defines no acceptance(ctx)")
    return fn


def _ctx(scenario: dict, files: dict[str, str]):
    prov = _DictProvider(files)
    ctx = CheckContext(provider=prov, handle=None, scenario=scenario,
                       goal=scenario.get("goal", ""), workdir="")
    return ctx, prov


def _perturb(value: str) -> str:
    s = str(value).strip()
    try:
        return str(int(s) + 1)
    except ValueError:
        return (s + "_WRONG") if s else "WRONG"


def validate_synth_check(code: str, scenario: dict) -> tuple[bool, str]:
    """Return (ok, reason). ok is False ONLY when the check provably disagrees
    with its own recomputed oracle; unverifiable checks return (True, why)."""
    seed = dict(scenario.get("seed_files") or {})
    try:
        acceptance = _load_acceptance(code)
    except Exception as e:  # noqa: BLE001
        return False, f"check does not load: {e}"

    # 1) Seed-only end-state: read the recomputed oracle + discover the output file.
    ctx, prov = _ctx(scenario, dict(seed))
    try:
        r0 = acceptance(ctx)
    except _UsesShell:
        return True, "unverified: check uses ctx.run (shell); cannot exercise deterministically"
    except Exception as e:  # noqa: BLE001
        return False, f"check crashed on the seed end-state: {e}"
    if not isinstance(r0, CheckResult):
        return False, "acceptance did not return a CheckResult"

    expected = (r0.evidence or {}).get("expected")
    if expected is None:
        return True, ("unverified: check did not expose evidence['expected'] "
                      "(recompute-first contract not followed)")
    out_files = [p for p in prov.reads if p not in seed]
    if len(out_files) != 1:
        return True, "unverified: could not uniquely identify the candidate output file"
    out = out_files[0]

    # 2) The candidate's recomputed-correct answer MUST pass.
    ctx_c, _ = _ctx(scenario, {**seed, out: str(expected)})
    try:
        rc = acceptance(ctx_c)
    except _UsesShell:
        return True, "unverified: check uses ctx.run (shell)"
    if not (isinstance(rc, CheckResult) and rc.passed):
        return False, (f"check FAILS on its own recomputed-correct answer "
                       f"({out}={expected!r}) — it trusts a wrong literal instead of "
                       f"the recompute (the #64 ordering bug)")

    # 3) A perturbed (wrong) answer MUST fail.
    ctx_w, _ = _ctx(scenario, {**seed, out: _perturb(str(expected))})
    try:
        rw = acceptance(ctx_w)
    except _UsesShell:
        return True, "unverified: check uses ctx.run (shell)"
    if isinstance(rw, CheckResult) and rw.passed:
        return False, f"check ACCEPTS a wrong answer ({out}={_perturb(str(expected))!r})"

    return True, f"validated: passes {out}={expected!r}, fails perturbations"
