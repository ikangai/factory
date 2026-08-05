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

Adversarial-review fix round (2026-08-05) hardened this module considerably — see each
function's docstring for the specific probe it closes: apply-time re-cast (never trust a
propose-time cast survives to apply time), a watermark-marker convention so the evidence-
freshness gate advances on every LLM call (not only ones with a real proposal), per-
cluster (not global-union) evidence binding, and pinned/counterproductive-learning
re-checks at apply time.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from ..common import config, harness_surface, killswitch

MAX_PROPOSALS = 5           # at most this many proposals per batch
MIN_NEW_EVIDENCE = 10       # the evidence-freshness gate: a free no-op below this many
                            # NEW failure rows since the last proposal batch
VALID_KINDS = ("setting", "prompt", "learning_corrective")

# Marker rows (adversarial-review fix round — the evidence-freshness watermark bug): a
# batch outcome that spent a real frontier call but produced no CITABLE proposal (an
# honest empty `[]`, or an unparseable reply) still gets ONE row here, kind='none',
# status in ('empty', 'error') — never a real proposal (kind is never a VALID_KINDS
# member), but its `created_at` is what makes `store.latest_harness_proposal()` (the
# evidence-freshness gate's watermark) advance. Without this, an honest "nothing to
# propose" answer left NO row behind, so the gate re-fired a frontier call every single
# shift forever once its evidence threshold was crossed once.
_MARKER_KIND = "none"

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
    — what the authority line means IN PRACTICE for a proposal aimed at this batch.

    Wording note (adversarial-review fix round): the `learning_corrective` bullet used to
    reference "the {MEMORY} section above" — LITERAL curly braces inside an f-string
    rendered as the literal text "{MEMORY}" in the finished SURFACE text, and
    `build_harness_prompt` replaces `{SURFACE}` BEFORE `{MEMORY}`, so that stray literal
    got a SECOND pass and spliced the real memory card into the middle of this sentence.
    Fixed by never emitting a brace-wrapped seam NAME in prose."""
    editable = sorted(harness_surface.editable_settings_keys())
    bool_keys = sorted(k for k in editable if config.SETTINGS_SPEC.get(k) is bool)
    frozen_knob_keys = sorted(harness_surface.FROZEN_KNOB_KEYS)
    frozen_role_prompts = sorted(harness_surface.FROZEN_ROLE_PROMPTS)
    bounds_kv = ", ".join(
        f"{k} in [{lo},{hi}]" for k, (lo, hi) in sorted(harness_surface.SANE_BOUNDS.items()))
    return (
        f"- Editable `setting` targets: {', '.join(editable)}.\n"
        f"- Numeric bounds for the INT ones: {bounds_kv}.\n"
        f"- The BOOLEAN ones ({', '.join(bool_keys)}) need no bounds — `true`/`false` "
        f"only.\n"
        f"- Editable `prompt` targets: any `roles/<x>/prompt.md` file NOT listed as a "
        f"frozen role prompt below — proposing one NEVER writes a file; the operator "
        f"applies it by hand after review.\n"
        f"- Editable `learning_corrective` targets: `learning:<id>` naming an EXISTING "
        f"learnings row that is NOT already pinned by the operator (see the learnings "
        f"surfaced above, or cite one from the mined weaknesses' bad-lore cluster).\n"
        f"- FROZEN surfaces (a `prompt` target touching any of these is rejected "
        f"wholesale): {', '.join(harness_surface.FROZEN_SURFACES)}.\n"
        f"- FROZEN role prompts (never a legal `prompt` target, even though they match "
        f"the roles/<x>/prompt.md shape): {', '.join(frozen_role_prompts)} — your OWN "
        f"prompt is in this list (meta-harness exclusion) and so is every verifier/gate "
        f"role's.\n"
        f"- FROZEN knobs (a `setting` target naming any of these is rejected wholesale, "
        f"even if a future SETTINGS_SPEC edit were to list one): every `autonomy.*` and "
        f"`grade.*` key; every gate/verifier knob ({', '.join(frozen_knob_keys)}) — "
        f"brakes and verifiers are never tunable by this loop; and any "
        f"`*.organizer`/`*.harness_engineer` trigger knob.\n"
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


# The seams roles/harness_engineer/prompt.md MUST carry — checked by build_harness_prompt
# before filling (adversarial-review fix round): a prompt edit that silently drops one
# would otherwise fail closed and SILENT (a `.replace()` on a seam that isn't there is a
# no-op, not an error) — e.g. deleting {BOUNDS} from the template would ship a prompt with
# no authority line and nothing would ever say so. Now it raises loudly instead.
_REQUIRED_SEAMS = ("{MISSION}", "{WEAKNESS}", "{SURFACE}", "{SETTINGS}", "{MEMORY}", "{BOUNDS}")


def build_harness_prompt(store, *, mission: Optional[dict], clusters: list[dict],
                         weakness_text: Optional[str] = None) -> str:
    """Fill roles/harness_engineer/prompt.md's seams from the live store + a weakness
    report. `weakness_text` may be passed in (plan_harness renders it once, both to the
    prompt AND for validate_proposals' citation vocabulary) or left None to render it
    here from `clusters`. Raises ValueError if the template is missing a required seam
    (see `_REQUIRED_SEAMS`) — a loud failure beats a silently-incomplete prompt."""
    from ..common.textutil import clean_line
    from ..reporting import factory_memory
    from ..reporting.weakness import render_weakness_table
    from ..roles.common import _load_prompt
    template = _load_prompt("harness_engineer")
    missing = [s for s in _REQUIRED_SEAMS if s not in template]
    if missing:
        raise ValueError(
            f"roles/harness_engineer/prompt.md is missing seam(s) {missing} — a prompt "
            f"edit must never silently drop a seam the authority line depends on")
    weakness_text = weakness_text if weakness_text is not None else render_weakness_table(clusters)
    mission_text = clean_line((mission or {}).get("statement") or "", cap=1000)
    return (template
            .replace("{MISSION}", mission_text or "(no active mission)")
            .replace("{WEAKNESS}", weakness_text)
            .replace("{SURFACE}", _surface_text())
            .replace("{SETTINGS}", _settings_text(store))
            .replace("{MEMORY}", factory_memory.memory_card(store, "harness_engineer"))
            .replace("{BOUNDS}", _bounds_text()))


def _cast_and_check_setting(target: str, raw_value) -> tuple[bool, object, str]:
    """Cast `raw_value` to `target`'s SETTINGS_SPEC type and check it against
    harness_surface.SANE_BOUNDS. Returns (ok, casted, reason) — `casted` is the
    CANONICAL value on success (a real bool/int, never the raw JSON value) and is what
    BOTH `validate_proposals` (propose-time) and `apply_proposal` (apply-time — MUST
    re-run this, never trust the propose-time result survives) persist.

    BLOCKER (adversarial-review fix round): apply_proposal used to `str(change.get(
    "value"))` the RAW, uncast JSON value straight into `store.set_setting` — a validated
    `{"value": 2.0}` for an int knob stored the literal string '2.0', and
    `config.resolve_setting`/`_cast_setting` raises ValueError on `int('2.0')` at EVERY
    subsequent read (cmd_run, _settings_text, dashboard resources) — bricking the rail
    AND this module's own next `factory harness plan`. Only the canonical `str(casted)`
    (e.g. '2') may ever reach `store.set_setting`.

    Also implements the documented-but-unenforced SANE_BOUNDS rule (adversarial-review
    fix round, item 9): an int SETTINGS_SPEC key with NO SANE_BOUNDS entry is out of the
    editable surface — `harness_surface`'s own docstring says so, but nothing checked it
    until now."""
    spec_kind = config.SETTINGS_SPEC.get(target)
    try:
        casted = config._cast_setting(spec_kind, raw_value)
    except (TypeError, ValueError):
        return False, None, f"value {raw_value!r} does not cast to {spec_kind}"
    bounds = harness_surface.SANE_BOUNDS.get(target)
    if spec_kind is int and bounds is None:
        return False, None, (f"{target} is an int SETTINGS_SPEC key with no SANE_BOUNDS "
                             f"entry — not editable until one is declared")
    if bounds is not None and not (bounds[0] <= casted <= bounds[1]):
        return False, None, f"value {casted} is outside SANE_BOUNDS {bounds} for {target}"
    return True, casted, ""


def validate_proposals(props, *, store, clusters: list[dict]) -> tuple[bool, list[str]]:
    """Enforce the authority line IN CODE (never trust the harness engineer's own claim of
    compliance). Returns (ok, reasons) — a batch with ANY violation is rejected WHOLESALE;
    `reasons` names every violation found (not just the first). An empty batch (`[]`) is a
    legitimate "nothing evidence-grounded to propose" answer and validates as ok.

    Evidence binding (adversarial-review fix round, BLOCKER): citations are checked
    against the NAMED cluster's own `evidence_ids` — NOT a union across every cluster in
    the report (a probe showed an off-topic edit citing an UNRELATED cluster's rows and
    passing, because ANY cluster's rows justified ANY edit under the old union check). A
    `learning_corrective`'s own `target` must additionally be among the cited evidence —
    citing SOME row from the right cluster isn't enough; it must cite the row it corrects."""
    reasons: list[str] = []
    if not isinstance(props, list):
        return False, ["reply is not a JSON array"]
    if len(props) > MAX_PROPOSALS:
        reasons.append(f"{len(props)} proposals exceeds MAX_PROPOSALS ({MAX_PROPOSALS})")

    evidence_by_weakness = {c["id"]: set(c.get("evidence_ids") or []) for c in clusters}
    weakness_slugs = set(evidence_by_weakness)

    from ..reporting import factory_memory

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

        for field in ("rationale", "expected_effect", "risk"):
            if not isinstance(p.get(field), str) or not p.get(field, "").strip():
                reasons.append(f"{tag}: {field!r} must be a non-empty string")

        cluster_evidence = evidence_by_weakness.get(weakness)
        if cluster_evidence is None:
            reasons.append(f"{tag}: weakness {weakness!r} does not name a cluster in the "
                           f"current weakness report")
            cluster_evidence = set()

        if not isinstance(evidence, list) or not evidence or not all(
                isinstance(e, str) and e for e in evidence):
            reasons.append(f"{tag}: evidence must be a non-empty list of non-blank strings")
        else:
            bad = [e for e in evidence if e not in cluster_evidence]
            if bad:
                reasons.append(f"{tag}: evidence id(s) {bad} are not among the NAMED "
                               f"cluster {weakness!r}'s own evidence_ids (citing another "
                               f"cluster's rows does not justify this edit)")

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
                ok_v, _casted, reason_v = _cast_and_check_setting(target, change["value"])
                if not ok_v:
                    reasons.append(f"{tag}: {reason_v}")
        elif kind == "prompt":
            if not isinstance(change, dict) or not str(change.get("patch") or "").strip():
                reasons.append(f"{tag}: prompt change must include a non-empty 'patch'")
        elif kind == "learning_corrective":
            if not isinstance(change, dict) or change.get("op") not in ("archive", "pin"):
                reasons.append(f"{tag}: learning_corrective change.op must be 'archive' "
                               f"or 'pin'")
            if isinstance(evidence, list) and target not in evidence:
                reasons.append(f"{tag}: learning_corrective target {target!r} must be "
                               f"among the cited evidence ids (citing the CLUSTER isn't "
                               f"enough — cite the row it corrects)")
            if ok_t:
                lid_str = target.split(":", 1)[1]
                row = store.get_learning(int(lid_str))
                if row is None:
                    reasons.append(f"{tag}: {target} does not exist")
                elif row.get("pinned"):
                    reasons.append(f"{tag}: {target} is pinned by the operator — not a "
                                   f"valid corrective target")
                elif (isinstance(change, dict) and change.get("op") == "pin"
                      and factory_memory.is_counterproductive(row)):
                    # BLOCKER (adversarial-review fix round, self-poisoning): 'pin' makes
                    # a learning survive is_counterproductive's own suppression (see
                    # factory_memory.memory_card_with_ids) and lead the card FOREVER —
                    # pinning a row already PROVEN counterproductive is the exact
                    # self-poisoning failure this whole loop exists to fix.
                    reasons.append(f"{tag}: {target} is proven counterproductive (its own "
                                   f"outcome counters) — 'pin' would re-inject it into "
                                   f"every worker's card; only 'archive' is valid here")

    return (len(reasons) == 0), reasons


def _compose_rationale(p: dict) -> str:
    """Fold rationale + expected_effect + risk into ONE persisted text block
    (adversarial-review fix round, BLOCKER: these were silently DROPPED before —
    `factory harness show <id>` is where an operator reads them back before deciding)."""
    parts = []
    rationale = str(p.get("rationale") or "").strip()
    if rationale:
        parts.append(rationale)
    expected = str(p.get("expected_effect") or "").strip()
    if expected:
        parts.append(f"Expected effect: {expected}")
    risk = str(p.get("risk") or "").strip()
    if risk:
        parts.append(f"Risk: {risk}")
    return "\n".join(parts)[:2000]


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
            change=change, rationale=_compose_rationale(p),
            evidence=evidence, status=status)
        out.append(store.get_harness_proposal(row_id))
    return out


def _persist_marker(store, *, status: str, rationale: str, shift_id: Optional[int]) -> dict:
    """Persist ONE watermark marker row (kind='none', see the module-level docstring) for
    an outcome that consumed a frontier call but produced no citable proposal. This is
    what makes `store.latest_harness_proposal()` — the evidence-freshness gate's
    watermark — advance on EVERY LLM call, not only ones with >=1 real proposal; without
    it an honest `[]` left no row behind and the gate re-fired a frontier call every
    single shift forever (adversarial-review fix round, BLOCKER)."""
    row_id = store.add_harness_proposal(
        shift_id=shift_id, weakness="", kind=_MARKER_KIND, target="", change={},
        rationale=rationale[:2000], evidence=[], status=status)
    return store.get_harness_proposal(row_id)


def plan_harness(store, *, shift_id: Optional[int] = None,
                 claude_fn: Optional[Callable] = None) -> Optional[list[dict]]:
    """Run the harness engineer and, on a valid VALIDATED reply, PERSIST its proposals as
    status='proposed' — this function NEVER applies them (apply is always a separate
    operator act — see `apply_proposal` / `factory harness apply <id>`). Returns the
    persisted proposal rows on success (possibly []), else None (nothing changed).

    Brakes (every one a MUST, mirroring org.plan_org's posture): `killswitch.is_halted()`
    is checked FIRST — STOP vetoes even ATTEMPTING the frontier call. A transport/parse
    failure records a factory learning AND a watermark marker row (status='error'). A
    VALIDATION failure persists EVERY proposal in the reply with status='rejected' (audit
    trail) plus a learning, but applies nothing. A validated but EMPTY batch persists a
    watermark marker row (status='empty') and returns `[]`. Only a batch that parses,
    validates, AND has >=1 proposal is persisted as real 'proposed' rows. Spend is
    ledgered role='harness_engineer' regardless of outcome (a failed/rejected/empty plan
    still spent real tokens) — WITH `shift_id` when called from the shift-end hook,
    WITHOUT it for a bare CLI invocation (None is the ledger's own "no shift"
    convention)."""
    if killswitch.is_halted():                  # STOP vetoes even attempting the frontier call
        return None

    if claude_fn is None:                        # deferred import → tests monkeypatch claude_p
        from ..roles.common import claude_p as claude_fn
    from ..common.textutil import clean_line
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
        _persist_marker(store, status="error",
                        rationale="unparseable JSON reply (expected a proposal array)",
                        shift_id=shift_id)
        return None

    ok, reasons = validate_proposals(reply, store=store, clusters=clusters)
    if not ok:
        _persist(store, reply, status="rejected", shift_id=shift_id)
        reasons_text = clean_line("; ".join(reasons), cap=1000)
        factory_memory.record_learning(
            store, "factory",
            f"a harness engineer proposal batch FAILED validation and was rejected — "
            f"{reasons_text}", scope="harness_engineer", shift_id=shift_id)
        return None

    if not reply:               # a VALIDATED, empty batch — an honest "nothing to propose"
        _persist_marker(
            store, status="empty",
            rationale="the harness engineer proposed nothing this round — the evidence "
                      "didn't clearly support a bounded change", shift_id=shift_id)
        return []

    return _persist(store, reply, status="proposed", shift_id=shift_id)


def _new_evidence_count(store) -> int:
    """How many task_evidence rows exist AFTER the last harness proposal batch's newest
    row (or ALL of them, when no batch has ever run) — the evidence-freshness gate's cost
    check. `latest_harness_proposal` is the ONE reader that must see marker rows too (it
    IS the watermark) — see the module-level `_MARKER_KIND` docstring."""
    last = store.latest_harness_proposal()
    since = last["created_at"] if last else None
    return store.count_task_evidence_since(since)


def maybe_plan_harness(store, *, shift_id: Optional[int] = None,
                       claude_fn: Optional[Callable] = None) -> Optional[list[dict]]:
    """The gated automatic trigger (injected into `orchestrator.shift.run_shift` as
    `harness_planner`, wired from `orchestrator.orchestrator.cmd_run` — see that module
    for the injection seam, adversarial-review fix round): STOP check + an evidence-
    freshness gate (a free no-op — no frontier call — unless at least MIN_NEW_EVIDENCE new
    failure rows have landed since the last proposal batch's watermark). Otherwise
    delegates straight to `plan_harness`.

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


def _change_summary(kind: str, change: dict) -> str:
    """One-line, human-legible summary of WHAT a proposal changes (adversarial-review fix
    round, BLOCKER 6): shown inline by `factory harness show` and carried into the
    board's newest-5 list (reporting.fleet_viz.harness_state) so an operator gate is never
    asked to approve a proposal it can't see the substance of at a glance — a
    `require_test=false` setting must never look indistinguishable from a benign retune."""
    change = change or {}
    if kind == "setting":
        return f"value={change.get('value')!r}"
    if kind == "learning_corrective":
        corrective = str(change.get("corrective") or "").strip()
        base = f"op={change.get('op')!r}"
        return f"{base} corrective={corrective!r}" if corrective else base
    if kind == "prompt":
        return f"summary={change.get('summary') or '(none)'!r}"
    return "(unknown kind)"


# =========================================================================================
# Apply / reject — always an OPERATOR act, never automatic (design §D, apply-path asymmetry)
# =========================================================================================
def apply_proposal(store, proposal_id: int, *, decided_by: str = "operator-cli") -> dict:
    """Apply ONE proposal. Apply paths are deliberately ASYMMETRIC:
      - 'setting'            -> re-cast + re-bounds-check (never trust propose-time —
        surface facts can drift between propose and apply), then `store.set_setting`
        with the CANONICAL cast value (never the raw JSON value — see
        `_cast_and_check_setting`'s docstring for the bug this closes). Auto-applied.
      - 'learning_corrective' -> re-checks pinned (an operator pin made AFTER this
        proposal was filed must win, never be silently overridden) and, for op='pin',
        re-checks is_counterproductive (a proven-bad learning must never be pinned —
        the self-poisoning failure this loop exists to fix); archives/pins the cited
        learning + `record_learning`s a corrective lesson with provenance, when one was
        supplied. Auto-applied.
      - 'prompt'              -> NEVER writes a file. Prints the patch + target and marks
        'approved' (not 'applied') — a human/agent lands it through normal git review.
    Every branch PRINTS what it is about to do BEFORE doing it (adversarial-review fix
    round, BLOCKER 6: an operator approving a proposal must see the concrete action, not
    just a terse OK afterward). Refuses when the id doesn't exist or isn't in a live
    status ('proposed' or 'approved'). Re-checks `harness_surface.check_target` at apply
    time too (belt-and-suspenders — surface facts, e.g. SETTINGS_SPEC's contents, could
    in principle have moved between propose-time and apply-time)."""
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
        ok_v, casted, reason_v = _cast_and_check_setting(target, change.get("value"))
        if not ok_v:
            store.set_harness_proposal_status(
                proposal_id, "rejected", decided_by=decided_by,
                result=f"apply-time re-check failed: {reason_v}")
            return {"ok": False, "reason": reason_v}
        current = config.resolve_setting(store, target, None)[0]
        print(f"[harness] applying proposal #{proposal_id}: setting {target} = "
              f"{casted!r} (was {current!r})")
        store.set_setting(target, str(casted))
        store.set_harness_proposal_status(
            proposal_id, "applied", decided_by=decided_by,
            result=f"set_setting({target!r}, {casted!r})")
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
        if op not in ("archive", "pin"):
            store.set_harness_proposal_status(
                proposal_id, "rejected", decided_by=decided_by,
                result=f"unknown op {op!r}")
            return {"ok": False, "reason": f"unknown op {op!r}"}
        # Apply-time re-checks (adversarial-review fix round, BLOCKER 5c): the operator
        # (or a later proposal) may have acted on this SAME learning between propose and
        # apply — re-run what propose-time already checked, never trust it survived.
        if src.get("pinned"):
            reason = (f"learning {lid} is now pinned by the operator — a pin made after "
                      f"this proposal was filed must win, never be silently overridden")
            store.set_harness_proposal_status(proposal_id, "rejected", decided_by=decided_by,
                                              result=reason)
            return {"ok": False, "reason": reason}
        if op == "pin" and factory_memory.is_counterproductive(src):
            reason = (f"learning {lid} is proven counterproductive — pinning it would "
                      f"re-inject proven-false lore into every worker's card")
            store.set_harness_proposal_status(proposal_id, "rejected", decided_by=decided_by,
                                              result=reason)
            return {"ok": False, "reason": reason}
        corrective = str(change.get("corrective") or "").strip()
        print(f"[harness] applying proposal #{proposal_id}: {op} learning #{lid} "
              f"({src['content'][:80]!r})"
              + (f" + corrective: {corrective!r}" if corrective else ""))
        if op == "archive":
            store.archive_learning(lid)
        else:
            store.pin_learning(lid)
        if corrective:
            factory_memory.record_learning(
                store, src["role"],
                f"{corrective} (corrective for learning #{lid}, harness proposal "
                f"#{proposal_id})", scope="harness-corrective")
        store.set_harness_proposal_status(proposal_id, "applied", decided_by=decided_by,
                                          result=f"{op} learning #{lid}")
        return {"ok": True, "kind": kind, "target": target}

    if kind == "prompt":
        # v1 (design's explicit YAGNI): NEVER writes a file. Print the patch, THEN mark
        # 'approved' (not 'applied') — a human/agent lands it through normal git review.
        print(f"[harness] applying proposal #{proposal_id}: a PROMPT patch — will be "
              f"marked 'approved' (v1 never auto-writes prompt files); land it by hand:")
        print(f"  target: {target}")
        print(f"  summary: {change.get('summary', '')}")
        print(change.get("patch") or "(no patch text)")
        store.set_harness_proposal_status(
            proposal_id, "approved", decided_by=decided_by,
            result="prompt patch approved — land by hand (v1 never auto-writes prompt files)")
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
# CLI surface: factory harness mine|plan|show[ <id>]|apply <id>|reject <id>  (mirrors cmd_org)
# =========================================================================================
def cmd_harness(store, action: str, *, target_id: Optional[str] = None,
                claude_fn: Optional[Callable] = None) -> None:
    """`factory harness`'s CLI surface:
      factory harness mine        # print the rendered weakness table (no LLM, no store write)
      factory harness plan        # the frontier harness engineer proposes a batch (≤5)
      factory harness show        # every proposal (any status), newest first, one line
                                   # each with an inline change summary
      factory harness show <id>   # the FULL detail of ONE proposal (change JSON,
                                   # rationale/expected-effect/risk, evidence ids, status)
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
                print(f"  #{r['id']} [{r['kind']}] {r['target']} "
                      f"{_change_summary(r['kind'], r.get('change') or {})} — "
                      f"{(r.get('rationale') or '')[:80]}")
        return
    if action == "show":
        if target_id is not None:
            try:
                pid = int(target_id)
            except (TypeError, ValueError):
                print(f"[harness] show needs an integer proposal id, got {target_id!r} — "
                      "see `factory harness show` (no id) for the list")
                return
            row = store.get_harness_proposal(pid)
            if row is None:
                print(f"[harness] no proposal #{pid}")
                return
            print(f"[harness] proposal #{row['id']} [{row['status']}]")
            print(f"  kind:      {row['kind']}")
            print(f"  target:    {row['target']}")
            print(f"  weakness:  {row['weakness']}")
            print(f"  change:    {json.dumps(row.get('change') or {}, indent=2)}")
            print(f"  evidence:  {', '.join(row.get('evidence') or []) or '(none)'}")
            print(f"  rationale: {row.get('rationale') or '(none)'}")
            if row.get("decided_by"):
                print(f"  decided:   {row['decided_by']} at {row.get('decided_at')} — "
                      f"{row.get('result') or ''}")
            return
        rows = store.harness_proposals(limit=50)
        if not rows:
            print("[harness] no proposals yet")
            return
        for r in rows:
            print(f"  #{r['id']} [{r['status']}] {r['kind']} {r['target']} "
                  f"{_change_summary(r['kind'], r.get('change') or {})} "
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
    print("[harness] usage: factory harness mine|plan|show[ <id>]|apply <id>|reject <id>")
