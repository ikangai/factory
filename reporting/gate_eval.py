"""Golden-case eval loop for the factory's LLM gates (`factory eval-gates` —
roadmap Task 2.1, P12: eval the improvement layer itself).

`tests/test_scope_check.py` monkeypatches every judge, so an edit to
roles/scope_check/prompt.md is regression-tested by NOTHING — and the live scope
judge has never once rejected or split in production (zero `scope-%` results in
the live DB despite auto_decompose firing). The FIRST hypothesis these goldens
probe is therefore a judge miscalibrated toward `pass`: the fixtures are majority
realistic expected-pass briefs plus hand-authored adversarial cases (over-bundled
multi-surface briefs, clive's frozen execution/ + runtime.py surface, vague
unfalsifiables). Hand-authored on purpose — harvesting negatives from history is
impossible (none exist). Expected verdicts are SETS ({"expected":["reject",
"split"]}) to absorb LLM nondeterminism.

The runner replays each fixture through the LIVE `scope_judge` and grades through
`normalize_verdict` — the same fail-open normalization production applies — so the
eval measures exactly what a claimed task would experience. Per-case outcomes
persist in `gate_eval_results` (the smallest honest mechanism: a tiny append-only
table, same pattern as task_evidence — learnings are prose and can't carry
machine-comparable per-case state); a case flipping ok→fail vs its previous run is
a regression in the gate itself and records a factory learning.

Spend discipline: `killswitch.is_halted()` at entry (STOP vetoes even "read-only"
eval spend), every case's judge spend ledgered under role='gate_eval' (attributed
to the running shift when invoked in one — folding into the loop token brake —
and with a NULL shift_id on standalone CLI runs, which add_budget allows), and the
fixture count is capped at MAX_CASES. Operator-triggered ONLY — never wired into
any loop. (Decompose/reviewer fixtures + the weekly launchd agent are follow-ups.)
"""
from __future__ import annotations

import json
import os
from typing import Callable, Optional

MAX_CASES = 20                       # hard spend ceiling: one judge call per case


def fixtures_path(gate: str = "scope") -> str:
    from ..common import paths
    return os.path.join(paths.FACTORY_ROOT, "scenarios", "gates", f"{gate}.jsonl")


def load_fixtures(path: Optional[str] = None) -> list[dict]:
    """Parse a gate's golden fixtures (JSONL: {id, title, detail?, expected:[verdicts]}).
    Malformed cases raise ValueError — a broken golden file must fail LOUDLY, not
    silently shrink the eval into a fake green. Count capped at MAX_CASES."""
    from .scope_check import DECISIONS
    path = path or fixtures_path()
    cases: list[dict] = []
    seen: set = set()
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            cid = (case.get("id") or "").strip() if isinstance(case, dict) else ""
            title = (case.get("title") or "").strip() if isinstance(case, dict) else ""
            expected = case.get("expected") if isinstance(case, dict) else None
            if not (cid and title and isinstance(expected, list) and expected
                    and set(expected) <= set(DECISIONS)):
                raise ValueError(
                    f"{path}:{n}: golden needs id, title and expected ⊆ {DECISIONS} — got "
                    f"{line[:120]!r}")
            if cid in seen:
                raise ValueError(f"{path}:{n}: duplicate golden id {cid!r}")
            seen.add(cid)
            cases.append(case)
            if len(cases) >= MAX_CASES:            # spend ceiling, not an error
                break
    return cases


def run_gate_eval(store, *, judge: Optional[Callable] = None,
                  shift_id: Optional[int] = None, path: Optional[str] = None,
                  gate: str = "scope") -> dict:
    """Replay the goldens through the (LIVE by default) judge; print the per-case +
    aggregate scorecard; persist per-case outcomes; record a factory learning for
    every ok→fail flip. Returns the report dict (`ok_all` drives the CLI exit code).
    Store writes (outcomes, ledger, learnings) happen on the caller's MAIN thread."""
    from ..common import killswitch
    from . import factory_memory, scope_check

    if killswitch.is_halted():                     # STOP vetoes even eval spend
        print("[gate-eval] STOP is engaged — refusing to spend on the eval (0 cases run)")
        return {"gate": gate, "halted": True, "ok_all": False, "total": 0, "ok": 0,
                "failed": 0, "pass_bias": 0, "flips": [], "cases": []}

    cases = load_fixtures(path or fixtures_path(gate))
    if judge is None:                              # the LIVE judge — the point of the eval
        judge = scope_check.scope_judge
    prev = {r["case_id"]: bool(r["ok"]) for r in store.latest_gate_eval_results(gate)}

    results: list[dict] = []
    flips: list[str] = []
    ok_n = bias = 0
    for case in cases:
        task = {"id": case["id"], "title": case["title"],
                "detail": case.get("detail", "")}
        try:
            raw = judge(task)
        except Exception:                          # noqa: BLE001 — mirror prefilter: a judge
            raw = {}                               # crash normalizes to pass, never aborts
        scope_check._ledger_judge_spend(store, raw, "gate_eval",
                                        f"gate eval: {case['id']}", shift_id)
        v = scope_check.normalize_verdict(raw)
        expected = list(case["expected"])
        ok = v["decision"] in expected
        ok_n += ok
        if "pass" not in expected and v["decision"] == "pass":
            bias += 1                              # an adversarial case waved through
        flipped = prev.get(case["id"]) is True and not ok
        if flipped:
            flips.append(case["id"])
            factory_memory.record_learning(
                store, "factory",
                f"gate-eval regression: {gate} golden '{case['id']}' flipped ok→fail — "
                f"the judge returned '{v['decision']}', expected one of {sorted(expected)}; "
                "re-check the last prompt/judge change before trusting the gate",
                scope="gate_eval", shift_id=shift_id)
        store.add_gate_eval_result(gate, case["id"], ok, v["decision"])
        results.append({"id": case["id"], "expected": expected,
                        "verdict": v["decision"], "ok": ok, "flipped": flipped})
        print(f"[gate-eval] {'ok  ' if ok else 'FAIL'} {case['id']}: got "
              f"'{v['decision']}' (expected {'|'.join(expected)})"
              + (" — FLIPPED ok→fail" if flipped else ""))

    print(f"[gate-eval] {gate}: {ok_n}/{len(cases)} ok, {len(cases) - ok_n} failed, "
          f"{len(flips)} flipped ok→fail")
    if bias:
        print(f"[gate-eval] pass-bias probe: {bias} adversarial case(s) waved through as "
              f"'pass' — the {gate} judge may be miscalibrated toward pass")
    return {"gate": gate, "halted": False, "ok_all": ok_n == len(cases),
            "total": len(cases), "ok": ok_n, "failed": len(cases) - ok_n,
            "pass_bias": bias, "flips": flips, "cases": results}
