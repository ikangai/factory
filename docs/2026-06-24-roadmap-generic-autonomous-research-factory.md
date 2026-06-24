# Roadmap — Generic, Autonomous, Research-Driven Harness Factory

Date: 2026-06-24 · Status: proposed (Martin's mission statement → phased plan)

## Mission (Martin, 2026-06-24)

> The factory autonomously continues developing a target (e.g. clive) with as
> little human intervention as possible. The human points it in a direction by
> stating a **mission statement**; the factory does the rest — including
> **research** (arXiv / HuggingFace papers, etc.) in the context of *agents that
> use a CLI to work and communicate*. It should be **generic**: point it at any
> repo and it does the rest.

## Foundation already in place (do not rebuild)

- **Phase 0**: proposer/judge/reporter/miner roles (`claude -p`), deterministic
  runner grading the real shell end-state, SQLite blackboard, operator board,
  human promotion gate. (4-auditor + 8-angle reviewed.)
- **Phase 1 operated for real**: mine→vet→baseline→propose→round→gate runs
  end-to-end; champion robust on real mined tasks.
- **Safety backbone proven load-bearing** (both caught by the human gate, then fixed):
  - claude-cli **isolation** — every `claude -p` runs `--setting-sources "" --tools "" --strict-mcp-config` (no plugins/hooks/MCP/chat leakage). clive-side half on clive `b1c6355`.
  - **#64** miner miscounted-oracle (Goodhart landmine — champion was right).
  - **#65** scoped-round false-eligibility (`fe392be`: like-for-like comparison).
- **Relocated** to standalone `/Development/factory` (this repo); target set by
  `clive.root` in `config.yaml` — first step toward repo-agnostic.

## Three evolution axes

### A. Genericity — point it at any repo
The factory still bakes in clive shape (champion `system_prompt` describes clive;
`spec_applier` actuates clive env knobs; scenarios are clive-shaped). Generalise via
a **Target Adapter** interface: `run_target(goal, spec) → end-state`, `actuate(open-block) → env/flags`, `discover_capabilities()`, `seed_scenarios()`. clive becomes
*one* adapter. Entry: `factory init <repo> --mission "..."`. `config.clive.*` →
`config.target.*`.

### B. Research integration — literature → proposals
A new **researcher role**: retrieve papers on CLI-driving / tool-use / multi-agent
agents (arXiv, HF), distill concrete techniques, and emit (a) candidate `open`-block
changes grounded in a citation, and (b) new scenarios that exercise a technique.
Decisions: source access (web fetch / API), grounding (every proposal cites its
source), dedup vs already-tried, and keeping the proposer blind to held-out.

### C. Bounded autonomy — mission → self-directed loop
Given a mission statement, the factory runs the loop unattended: derive direction →
mine/generate gaps (incl. research-driven) → propose → round → gate. **Promotion
stays human-gated** until trust metrics (clean Goodhart signals, held-out stability,
oracle-validation) justify relaxing it. Autonomy = unattended *operation*, not
unattended *promotion* — the two grading bugs found this session are why.

## Phasing

- **P2a — Target Adapter** (genericity): extract the clive-specific bits behind an
  adapter; prove a second trivial target. Unblocks "any repo."
- **P2b — Researcher role** (research): paper retrieval → grounded proposals +
  scenarios; wire into the proposer/miner with citation provenance.
- **P2c — Autonomy harness**: mission→continuous loop with budget/Goodhart/held-out
  guardrails; promotion-gate relaxation criteria (explicitly Martin's call).

## Open decisions for the human (steer these)

1. **Research access**: which sources + how (web fetch, arXiv API, HF) — and any cost/rate bounds.
2. **Autonomy bound**: how much the factory may do unattended before a human checkpoint (promotion always gated? a "trust budget"?).
3. **First non-clive target**: a small repo to validate the adapter against.
4. **Hardening cut**: fix the #64 miner-oracle class + the deferred multi-clive issues before widening autonomy, or in parallel.
