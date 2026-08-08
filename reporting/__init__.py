"""Reporting + presentation layer for the harness factory.

`generate_executive_summary` (the one symbol this package exports) turns the
blackboard + filesystem state of an (autonomous) run into a short, plain-language
presentation for the human at the 09:00 update: DISCOVERIES, DECISIONS, PROPOSED
NEXT STEPS. It never promotes, and falls back to a deterministic templated summary
if the LLM call fails — so it can never crash a loop.

SCOPE OF THE READ-ONLY CONTRACT (corrected 2026-08-08, Phase 2 canonicality review —
this docstring previously claimed "It NEVER writes to the store" for the whole
package, which is false and was the broadest untested false claim in the tree):

- READ-ONLY (enforced by `tests/test_canonicality.py`): `summary.py`, `diary.py`,
  `blog.py` — the three presentation modules. They write no workflow state; they do
  ledger their own token spend via `store.add_budget`, which is telemetry every role
  emits, not a workflow decision.
- WORKFLOW WRITERS, legitimately: `approvals.py`, `issue_sync.py`, `factory_memory.py`,
  `scope_check.py`, `human_queue.py`, `gate_eval.py`. These implement decisions
  (approvals, issue-sync ledgering, learnings, scope verdicts) and write the store by
  design.

The canonical division the factory actually keeps is in
`docs/runbooks/crash-recovery.md` ("Canonical state"): SQLite owns workflow and
decisions, git owns artifacts, GitHub owns published state, the bus is notification-only.
"""
from .summary import generate_executive_summary

__all__ = ["generate_executive_summary"]
