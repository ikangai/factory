"""The self-harness loop — the bounded harness engineer (design: docs/plans/2026-08-05-
self-harness-loop-design.md, Components C/D). A one-shot, FRONTIER-tier `claude -p` call
that reads reporting/weakness.py's deterministic failure clusters and proposes BOUNDED
harness edits — knob settings, role-prompt patches, learnings corrections — each citing
the evidence that motivated it. `validate_proposals` (never the harness engineer's own
claim) decides whether a proposal is even STORED as 'proposed'. APPLYING one is always a
separate, operator-gated act (`apply_proposal` / `factory harness apply <id>`) — this
module never writes a setting/prompt/learning on its own initiative.

Mirrors orchestrator/org.py's plan_org shape exactly: killswitch FIRST, deferred claude_p
import (so tests monkeypatch it), frontier model, ledger spend regardless of outcome,
parse, validate, persist.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from ..common import config, harness_surface, killswitch

MAX_PROPOSALS = 5           # at most this many proposals per batch
MIN_NEW_EVIDENCE = 10       # the evidence-freshness gate: a free no-op below this many
                            # NEW failure rows since the last proposal batch
VALID_KINDS = ("setting", "prompt", "learning_corrective")

# The design's authority line, VERBATIM (design doc § "The authority line") — asserted
# literally in tests, exactly as org.py's own _bounds_text contract.
AUTHORITY_LINE = (
    "The harness engineer PROPOSES; it never applies. Its proposals may touch only the "
    "declared editable surface: SETTINGS_SPEC knobs, role prompt files, and learnings "
    "rows. Brakes, budgets, gates, verifiers, the killswitch, the bus, the store schema, "
    "and this manifest itself are FROZEN — a proposal naming a frozen surface is "
    "rejected wholesale. Every proposal cites the evidence rows that motivated it. "
    "Application is an operator action. The trigger knob is config-only and outside "
    "SETTINGS_SPEC, so the loop can never widen or re-arm itself."
)


def _bounds_text() -> str:
    """The {BOUNDS} seam — the authority line VERBATIM. Mirrors org._bounds_text's role
    (the text an LLM reads, so wording accuracy is the whole point) but design keeps the
    concrete surface DETAIL in a separate {SURFACE} seam (see `_surface_text`) — here is
    only the authority statement itself."""
    return AUTHORITY_LINE


def _surface_text() -> str:
    """The {SURFACE} seam: the concrete editable-vs-frozen manifest + SANE_BOUNDS ranges
    — what the authority line means IN PRACTICE for a proposal aimed at this batch."""
    editable = sorted(harness_surface.editable_settings_keys())
    bool_keys = sorted(k for k in editable if config.SETTINGS_SPEC.get(k) is bool)
    bounds_kv = ", ".join(
        f"{k} in [{lo},{hi}]" for k, (lo, hi) in sorted(harness_surface.SANE_BOUNDS.items()))
    return (
        f"- Editable `setting` targets: {', '.join(editable)}.\n"
        f"- Numeric bounds for the INT ones: {bounds_kv}.\n"
        f"- The BOOLEAN ones ({', '.join(bool_keys)}) need no bounds — `true`/`false` "
        f"only.\n"
        f"- Editable `prompt` targets: any `roles/<x>/prompt.md` file — proposing one "
        f"NEVER writes a file; the operator applies it by hand after review.\n"
        f"- Editable `learning_corrective` targets: `learning:<id>` naming an EXISTING "
        f"learnings row (see the learnings the {{MEMORY}} section above surfaced, or cite "
        f"one from the mined weaknesses' bad-lore cluster).\n"
        f"- FROZEN surfaces (a `prompt` target touching any of these is rejected "
        f"wholesale): {', '.join(harness_surface.FROZEN_SURFACES)}.\n"
        f"- FROZEN knobs (a `setting` target naming any of these is rejected wholesale, "
        f"even if a future SETTINGS_SPEC edit were to list it): every `autonomy.*` and "
        f"`grade.*` key, and any `*.organizer`/`*.harness_engineer` trigger knob.\n"
        f"- At most {MAX_PROPOSALS} proposals per batch; a duplicate `(kind, target)` "
        f"pair rejects the WHOLE batch (the later one is the violation).")


def _settings_text(store) -> str:
    """The {SETTINGS} seam: every SETTINGS_SPEC key's currently resolved value + source
    (override/config/default), so the harness engineer proposes a CHANGE, not a guess."""
    lines = []
    for key, kind in sorted(config.SETTINGS_SPEC.items()):
        val, source = config.resolve_setting(store, key, None)
        lines.append(f"- {key} = {val!r} ({source}, type={kind.__name__})")
    return "\n".join(lines)


def build_harness_prompt(store, *, mission: Optional[dict], clusters: list[dict],
                         weakness_text: Optional[str] = None) -> str:
    """Fill roles/harness_engineer/prompt.md's seams from the live store + a weakness
    report. `weakness_text` may be passed in (plan_harness renders it once, both to the
    prompt AND for validate_proposals' citation vocabulary) or left None to render it
    here from `clusters`."""
    from ..common.textutil import clean_line
    from ..reporting import factory_memory
    from ..reporting.weakness import render_weakness_table
    from ..roles.common import _load_prompt
    weakness_text = weakness_text if weakness_text is not None else render_weakness_table(clusters)
    mission_text = clean_line((mission or {}).get("statement") or "", cap=1000)
    return (_load_prompt("harness_engineer")
            .replace("{MISSION}", mission_text or "(no active mission)")
            .replace("{WEAKNESS}", weakness_text)
            .replace("{SURFACE}", _surface_text())
            .replace("{SETTINGS}", _settings_text(store))
            .replace("{MEMORY}", factory_memory.memory_card(store, "harness_engineer"))
            .replace("{BOUNDS}", _bounds_text()))


def validate_proposals(props, *, store, clusters: list[dict]) -> tuple[bool, list[str]]:
    """Enforce the authority line IN CODE (never trust the harness engineer's own claim of
    compliance). Returns (ok, reasons) — a batch with ANY violation is rejected WHOLESALE;
    `reasons` names every violation found (not just the first). An empty batch (`[]`) is a
    legitimate "nothing evidence-grounded to propose" answer and validates as ok."""
    reasons: list[str] = []
    if not isinstance(props, list):
        return False, ["reply is not a JSON array"]
    if len(props) > MAX_PROPOSALS:
        reasons.append(f"{len(props)} proposals exceeds MAX_PROPOSALS ({MAX_PROPOSALS})")

    evidence_vocab = {eid for c in clusters for eid in (c.get("evidence_ids") or [])}
    weakness_slugs = {c["id"] for c in clusters}

    seen_targets: set = set()
    for i, p in enumerate(props):
        tag = f"proposal[{i}]"
        if not isinstance(p, dict):
            reasons.append(f"{tag}: not a dict")
            continue

        kind = p.get("kind")
        target = str(p.get("target") or "")
        weakness = p.get("weakness")
        evidence = p.get("evidence")
        change = p.get("change")

        if kind not in VALID_KINDS:
            reasons.append(f"{tag}: kind {kind!r} not in {VALID_KINDS}")
            continue   # nothing further about target/change can be checked meaningfully

        if weakness not in weakness_slugs:
            reasons.append(f"{tag}: weakness {weakness!r} does not name a cluster in the "
                           f"current weakness report")

        if not isinstance(evidence, list) or not evidence or not all(
                isinstance(e, str) and e for e in evidence):
            reasons.append(f"{tag}: evidence must be a non-empty list of non-blank strings")
        else:
            bad = [e for e in evidence if e not in evidence_vocab]
            if bad:
                reasons.append(f"{tag}: evidence id(s) {bad} are not in the weakness "
                               f"report's vocabulary")

        ok_t, reason_t = harness_surface.check_target(kind, target)
        if not ok_t:
            reasons.append(f"{tag}: {reason_t}")

        dedup_key = (kind, target)
        if dedup_key in seen_targets:
            reasons.append(f"{tag}: duplicate (kind, target) {dedup_key} — a later "
                           f"duplicate in the same batch is rejected")
        seen_targets.add(dedup_key)

        if kind == "setting":
            if not isinstance(change, dict) or "value" not in change:
                reasons.append(f"{tag}: setting change must be {{'value': ...}}")
            elif ok_t:
                spec_kind = config.SETTINGS_SPEC.get(target)
                try:
                    casted = config._cast_setting(spec_kind, change["value"])
                except (TypeError, ValueError):
                    reasons.append(f"{tag}: value {change.get('value')!r} does not cast "
                                   f"to {spec_kind}")
                else:
                    bounds = harness_surface.SANE_BOUNDS.get(target)
                    if bounds is not None and not (bounds[0] <= casted <= bounds[1]):
                        reasons.append(f"{tag}: value {casted} is outside SANE_BOUNDS "
                                       f"{bounds} for {target}")
        elif kind == "prompt":
            if not isinstance(change, dict) or not str(change.get("patch") or "").strip():
                reasons.append(f"{tag}: prompt change must include a non-empty 'patch'")
        elif kind == "learning_corrective":
            if not isinstance(change, dict) or change.get("op") not in ("archive", "pin"):
                reasons.append(f"{tag}: learning_corrective change.op must be 'archive' "
                               f"or 'pin'")
            if ok_t:
                lid_str = target.split(":", 1)[1]
                row = store.get_learning(int(lid_str))
                if row is None:
                    reasons.append(f"{tag}: {target} does not exist")
                elif row.get("pinned"):
                    reasons.append(f"{tag}: {target} is pinned by the operator — not a "
                                   f"valid corrective target")

    return (len(reasons) == 0), reasons


def _persist(store, proposals: list, *, status: str, shift_id: Optional[int]) -> list[dict]:
    """Insert every dict entry of `proposals` as its OWN harness_proposals row (audit
    trail either way — a REJECTED batch is stored too, per the design's plan_harness
    contract). Non-dict entries are skipped (nothing sane to store)."""
    out = []
    for p in proposals:
        if not isinstance(p, dict):
            continue
        change = p.get("change") if isinstance(p.get("change"), dict) else {}
        evidence = p.get("evidence") if isinstance(p.get("evidence"), list) else []
        row_id = store.add_harness_proposal(
            shift_id=shift_id, weakness=str(p.get("weakness") or ""),
            kind=str(p.get("kind") or ""), target=str(p.get("target") or ""),
            change=change, rationale=str(p.get("rationale") or "")[:2000],
            evidence=evidence, status=status)
        out.append(store.get_harness_proposal(row_id))
    return out


def plan_harness(store, *, shift_id: Optional[int] = None,
                 claude_fn: Optional[Callable] = None) -> Optional[list[dict]]:
    """Run the harness engineer and, on a valid VALIDATED reply, PERSIST its proposals as
    status='proposed' — this function NEVER applies them (apply is always a separate
    operator act — see `apply_proposal` / `factory harness apply <id>`). Returns the
    persisted proposal rows on success (possibly []), else None (nothing changed).

    Brakes (every one a MUST, mirroring org.plan_org's posture): `killswitch.is_halted()`
    is checked FIRST — STOP vetoes even ATTEMPTING the frontier call. A transport/parse
    failure records a factory learning and persists NOTHING. A VALIDATION failure persists
    EVERY proposal in the reply with status='rejected' (audit trail) plus a learning, but
    applies nothing. Only a batch that parses AND validates is persisted 'proposed'. Spend
    is ledgered role='harness_engineer' regardless of outcome (a failed/rejected plan still
    spent real tokens) — WITH `shift_id` when called from the shift-end hook, WITHOUT it
    for a bare CLI invocation (None is the ledger's own "no shift" convention)."""
    if killswitch.is_halted():                  # STOP vetoes even attempting the frontier call
        return None

    if claude_fn is None:                        # deferred import → tests monkeypatch claude_p
        from ..roles.common import claude_p as claude_fn
    from ..reporting import factory_memory
    from ..reporting.weakness import mine_weaknesses

    mission = store.active_mission()
    clusters = mine_weaknesses(store)
    prompt = build_harness_prompt(store, mission=mission, clusters=clusters)
    model = config.resolve_model("")             # the FRONTIER tier — harness design is judgment
    text, tokens, cost = claude_fn(prompt, model=model)
    store.add_budget("harness_engineer", int(tokens or 0), float(cost or 0.0),
                     notes="harness_engineer", shift_id=shift_id)

    reply = None
    try:
        reply = json.loads((text or "").strip())
    except (ValueError, TypeError):
        # Fall back to the shared balanced-bracket extractor's cousin: strip a stray
        # markdown fence defensively, then retry — the prompt asks for none, but never
        # trust it (mirrors org.plan_org's belt-and-suspenders `_parse_obj` use).
        from ..roles.common import _extract
        try:
            reply = json.loads(_extract(text or "", ("json", "")))
        except (ValueError, TypeError):
            reply = None

    if not isinstance(reply, list):
        factory_memory.record_learning(
            store, "factory",
            "the harness engineer returned unparseable JSON (expected a proposal array) "
            "— no proposals recorded", scope="harness_engineer", shift_id=shift_id)
        return None

    ok, reasons = validate_proposals(reply, store=store, clusters=clusters)
    if not ok:
        _persist(store, reply, status="rejected", shift_id=shift_id)
        factory_memory.record_learning(
            store, "factory",
            f"a harness engineer proposal batch FAILED validation and was rejected — "
            f"{'; '.join(reasons)}"[:1000], scope="harness_engineer", shift_id=shift_id)
        return None

    return _persist(store, reply, status="proposed", shift_id=shift_id)


def _new_evidence_count(store) -> int:
    """How many task_evidence rows exist AFTER the last harness proposal batch's newest
    row (or ALL of them, when no batch has ever run) — the evidence-freshness gate's cost
    check."""
    last = store.latest_harness_proposal()
    since = last["created_at"] if last else None
    return store.count_task_evidence_since(since)


def maybe_plan_harness(store, *, shift_id: Optional[int] = None,
                       claude_fn: Optional[Callable] = None) -> Optional[list[dict]]:
    """The gated automatic trigger (the shift-end hook in orchestrator/orchestrator.py's
    cmd_run): STOP check + an evidence-freshness gate (a free no-op — no frontier call —
    unless at least MIN_NEW_EVIDENCE new failure rows have landed since the last proposal
    batch). Otherwise delegates straight to `plan_harness`.

    DEVIATION from a literal reading of the design (noted, per the organizer precedent):
    the design text lists "STOP check, config gate, and an evidence gate" as this
    function's three gates. org.maybe_plan_org's own precedent, however, does NOT read
    config itself — the `super_worker.organizer` knob gates whether `maybe_plan_org` is
    even WIRED as the org_planner callable, at the cmd_run call site, not inside the
    function. This mirrors that precedent exactly: `super_worker.harness_engineer` gates
    whether this function is wired at all (cmd_run), not a second internal config read
    here — functionally identical (the trigger never fires when the knob is off either
    way) and keeps the un-self-widenable property (binding rule 7) simple to reason about."""
    if killswitch.is_halted():
        return None
    if _new_evidence_count(store) < MIN_NEW_EVIDENCE:
        return None
    return plan_harness(store, shift_id=shift_id, claude_fn=claude_fn)


# =========================================================================================
# Apply / reject — always an OPERATOR act, never automatic (design §D, apply-path asymmetry)
# =========================================================================================
def apply_proposal(store, proposal_id: int, *, decided_by: str = "operator-cli") -> dict:
    """Apply ONE proposal. Apply paths are deliberately ASYMMETRIC:
      - 'setting'            -> `store.set_setting` (the existing runtime-override seam —
        visible in `resolve_setting`'s own 'override' source, reversible the same way).
        Auto-applied; marks 'applied'.
      - 'learning_corrective' -> archive/pin the cited learning + `record_learning` a
        corrective lesson with provenance naming this proposal, when one was supplied.
        Auto-applied; marks 'applied'.
      - 'prompt'              -> NEVER writes a file. Prints the patch + target and marks
        'approved' (not 'applied') — a human/agent lands it through normal git review.
    Refuses (no store mutation beyond nothing) when the id doesn't exist or isn't in a
    live status ('proposed' or 'approved'). Re-checks `harness_surface.check_target` at
    apply time too (belt-and-suspenders — surface facts, e.g. SETTINGS_SPEC's contents,
    could in principle have moved between propose-time and apply-time)."""
    from ..reporting import factory_memory
    row = store.get_harness_proposal(proposal_id)
    if row is None:
        return {"ok": False, "reason": f"no proposal #{proposal_id}"}
    if row["status"] not in ("proposed", "approved"):
        return {"ok": False, "reason": f"proposal #{proposal_id} is {row['status']!r}, "
                                       f"not live (proposed/approved)"}

    kind, target, change = row["kind"], row["target"], row.get("change") or {}
    ok_t, reason_t = harness_surface.check_target(kind, target)
    if not ok_t:
        store.set_harness_proposal_status(proposal_id, "rejected", decided_by=decided_by,
                                          result=f"apply-time re-check failed: {reason_t}")
        return {"ok": False, "reason": reason_t}

    if kind == "setting":
        store.set_setting(target, str(change.get("value")))
        store.set_harness_proposal_status(
            proposal_id, "applied", decided_by=decided_by,
            result=f"set_setting({target!r}, {change.get('value')!r})")
        return {"ok": True, "kind": kind, "target": target}

    if kind == "learning_corrective":
        lid = int(target.split(":", 1)[1])
        src = store.get_learning(lid)
        if src is None:
            store.set_harness_proposal_status(
                proposal_id, "rejected", decided_by=decided_by,
                result=f"learning {lid} no longer exists")
            return {"ok": False, "reason": f"learning {lid} no longer exists"}
        op = change.get("op")
        if op == "archive":
            store.archive_learning(lid)
        elif op == "pin":
            store.pin_learning(lid)
        else:
            store.set_harness_proposal_status(
                proposal_id, "rejected", decided_by=decided_by,
                result=f"unknown op {op!r}")
            return {"ok": False, "reason": f"unknown op {op!r}"}
        corrective = str(change.get("corrective") or "").strip()
        if corrective:
            factory_memory.record_learning(
                store, src["role"],
                f"{corrective} (corrective for learning #{lid}, harness proposal "
                f"#{proposal_id})", scope="harness-corrective")
        store.set_harness_proposal_status(proposal_id, "applied", decided_by=decided_by,
                                          result=f"{op} learning #{lid}")
        return {"ok": True, "kind": kind, "target": target}

    if kind == "prompt":
        # v1 (design's explicit YAGNI): NEVER writes a file. Mark 'approved' (not
        # 'applied') and hand the patch to the operator/agent to land through normal git
        # review — file writes stay OUT of this loop entirely.
        store.set_harness_proposal_status(
            proposal_id, "approved", decided_by=decided_by,
            result="prompt patch approved — land by hand (v1 never auto-writes prompt files)")
        print(f"[harness] proposal #{proposal_id} approved — a PROMPT patch, land it by "
              f"hand (v1 never auto-writes prompt files):")
        print(f"  target: {target}")
        print(f"  summary: {change.get('summary', '')}")
        print(change.get("patch") or "(no patch text)")
        return {"ok": True, "kind": kind, "target": target, "approved_only": True}

    return {"ok": False, "reason": f"unknown proposal kind {kind!r}"}


def reject_proposal(store, proposal_id: int, *, decided_by: str = "operator-cli",
                    note: str = "") -> dict:
    """Reject ONE live proposal — the operator's explicit "no"."""
    row = store.get_harness_proposal(proposal_id)
    if row is None:
        return {"ok": False, "reason": f"no proposal #{proposal_id}"}
    if row["status"] not in ("proposed", "approved"):
        return {"ok": False, "reason": f"proposal #{proposal_id} is {row['status']!r}, "
                                       f"not live (proposed/approved)"}
    store.set_harness_proposal_status(proposal_id, "rejected", decided_by=decided_by,
                                      result=note)
    return {"ok": True}


# =========================================================================================
# CLI surface: factory harness mine|plan|show|apply <id>|reject <id>  (mirrors cmd_org)
# =========================================================================================
def cmd_harness(store, action: str, *, target_id: Optional[str] = None,
                claude_fn: Optional[Callable] = None) -> None:
    """`factory harness`'s CLI surface:
      factory harness mine        # print the rendered weakness table (no LLM, no store write)
      factory harness plan        # the frontier harness engineer proposes a batch (≤5)
      factory harness show        # every live proposal (newest first)
      factory harness apply <id>  # apply ONE proposal (asymmetric per kind — see apply_proposal)
      factory harness reject <id> # reject ONE proposal
    `claude_fn` is test-only plumbing (passed straight through to plan_harness) — the live
    CLI never supplies it, so plan_harness's own default (the real, isolated claude_p)
    applies."""
    if action == "mine":
        from ..reporting.weakness import mine_weaknesses, render_weakness_table
        print(render_weakness_table(mine_weaknesses(store)))
        return
    if action == "plan":
        result = plan_harness(store, claude_fn=claude_fn)
        if result is None:
            print("[harness] plan failed (STOP engaged, or a transport/parse/validation "
                  "failure) — see `factory learn list --role factory`")
        elif not result:
            print("[harness] planned 0 proposals — the evidence didn't clearly support a "
                  "bounded change this round")
        else:
            print(f"[harness] proposed {len(result)} proposal(s):")
            for r in result:
                print(f"  #{r['id']} [{r['kind']}] {r['target']} — "
                      f"{(r.get('rationale') or '')[:80]}")
        return
    if action == "show":
        rows = store.harness_proposals(limit=50)
        if not rows:
            print("[harness] no proposals yet")
            return
        for r in rows:
            print(f"  #{r['id']} [{r['status']}] {r['kind']} {r['target']} "
                  f"(weakness={r['weakness']}) — {(r.get('rationale') or '')[:80]}")
        return
    if action in ("apply", "reject"):
        try:
            pid = int(target_id)                      # type: ignore[arg-type]
        except (TypeError, ValueError):
            print(f"[harness] {action} needs an integer proposal id, got {target_id!r} — "
                  "see `factory harness show`")
            return
        res = (apply_proposal(store, pid, decided_by="operator-cli") if action == "apply"
              else reject_proposal(store, pid, decided_by="operator-cli"))
        if res.get("ok"):
            print(f"[harness] {action} #{pid}: ok"
                  + (" (prompt patch — land by hand, see above)"
                     if res.get("approved_only") else ""))
        else:
            print(f"[harness] {action} #{pid}: {res.get('reason')}")
        return
    print("[harness] usage: factory harness mine|plan|show|apply <id>|reject <id>")
