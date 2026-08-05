"""common/harness_surface.py — the factory's own editable-vs-frozen surface manifest
(design: docs/plans/2026-08-05-self-harness-loop-design.md, Component A). Pure module,
no store, no LLM — see tests/test_organizer.py for the naming/structure this mirrors.
"""
from factory.common import config, harness_surface as hs


# -- FROZEN_KNOB_PREFIXES / _is_frozen_knob ----------------------------------------------
def test_frozen_knob_blocks_every_autonomy_key():
    for key in ("autonomy.push_approval", "autonomy.loop_token_budget",
                "autonomy.enforce_shift_budget", "autonomy.anything_future"):
        assert hs._is_frozen_knob(key), key


def test_frozen_knob_blocks_every_grade_key():
    assert hs._is_frozen_knob("grade.mode")
    assert hs._is_frozen_knob("grade.rebaseline_autorevert")


def test_frozen_knob_blocks_organizer_and_harness_engineer_triggers():
    assert hs._is_frozen_knob("super_worker.organizer")
    assert hs._is_frozen_knob("super_worker.harness_engineer")


def test_frozen_knob_blocks_even_if_settings_spec_were_to_list_it():
    """Belt-and-suspenders (binding rule 7): the manifest freezes these independent of
    SETTINGS_SPEC's own contents — simulate a future edit that lists one anyway."""
    assert "super_worker.organizer" not in config.SETTINGS_SPEC   # today's real state
    assert hs._is_frozen_knob("super_worker.organizer")           # frozen regardless


def test_frozen_knob_does_not_block_ordinary_capacity_keys():
    for key in ("super_worker.max_parallel", "super_worker.max_tasks_per_shift",
                "super_worker.refill_threshold", "super_worker.max_profiles",
                "super_worker.dispatch_waves", "super_worker.auto_decompose",
                "super_worker.retry_on_discard", "super_worker.investigate_blocked"):
        assert not hs._is_frozen_knob(key), key


# -- FROZEN_KNOB_KEYS: gate/verifier knobs (adversarial-review fix round, BLOCKER 3) --------
def test_frozen_knob_keys_blocks_every_gate_verifier_knob():
    """The authority line says 'gates, verifiers... are FROZEN' — the ORIGINAL
    FROZEN_KNOB_KEYS was empty, so `super_worker.require_test=false` validated clean and
    would have let the rail merge code without tests from the next shift. Every SPEC key
    that flips a merge/dispatch-quality CHECK must be frozen."""
    for key in ("super_worker.require_test", "super_worker.acceptance_exec",
                "super_worker.scope_check", "super_worker.reviewer",
                "super_worker.milestone_verify"):
        assert hs._is_frozen_knob(key), key
        assert key in hs.FROZEN_KNOB_KEYS, key


def test_frozen_knob_keys_is_exactly_the_five_gate_verifier_keys():
    assert hs.FROZEN_KNOB_KEYS == {
        "super_worker.require_test", "super_worker.acceptance_exec",
        "super_worker.scope_check", "super_worker.reviewer",
        "super_worker.milestone_verify"}


# -- editable_settings_keys ---------------------------------------------------------------
def test_editable_settings_keys_is_settings_spec_minus_frozen():
    editable = hs.editable_settings_keys()
    assert editable == set(config.SETTINGS_SPEC) - hs.FROZEN_KNOB_KEYS
    assert "autonomy.push_approval" not in editable   # never in SETTINGS_SPEC anyway
    assert "super_worker.organizer" not in editable   # never in SETTINGS_SPEC anyway
    for key in hs.FROZEN_KNOB_KEYS:
        assert key not in editable, key
    assert "super_worker.max_parallel" in editable    # a plain capacity int stays editable


# -- SANE_BOUNDS ---------------------------------------------------------------------------
def test_sane_bounds_covers_every_settings_spec_int_key():
    int_keys = {k for k, kind in config.SETTINGS_SPEC.items() if kind is int}
    assert int_keys == set(hs.SANE_BOUNDS)


def test_sane_bounds_ranges_are_sane_low_le_high():
    for key, (lo, hi) in hs.SANE_BOUNDS.items():
        assert lo <= hi, key


# -- check_target: kind='setting' ----------------------------------------------------------
def test_check_target_setting_accepts_an_editable_key():
    ok, reason = hs.check_target("setting", "super_worker.max_parallel")
    assert ok is True and reason == ""


def test_check_target_setting_rejects_unknown_key():
    ok, reason = hs.check_target("setting", "super_worker.not_a_real_knob")
    assert ok is False and "SETTINGS_SPEC" in reason


def test_check_target_setting_rejects_frozen_autonomy_key():
    # autonomy.* is never in SETTINGS_SPEC either — both guards reject it; the "not a
    # SETTINGS_SPEC key" message fires first (see the belt-and-suspenders test below for
    # the FROZEN message on a key that PASSES the membership check).
    ok, reason = hs.check_target("setting", "autonomy.push_approval")
    assert ok is False and reason


def test_check_target_setting_rejects_frozen_grade_key():
    ok, reason = hs.check_target("setting", "grade.mode")
    assert ok is False and reason


def test_check_target_setting_rejects_organizer_and_harness_engineer_triggers():
    ok1, r1 = hs.check_target("setting", "super_worker.organizer")
    ok2, r2 = hs.check_target("setting", "super_worker.harness_engineer")
    assert ok1 is False and r1
    assert ok2 is False and r2


def test_check_target_setting_rejects_every_gate_verifier_knob():
    for key in hs.FROZEN_KNOB_KEYS:
        ok, reason = hs.check_target("setting", key)
        assert ok is False and "FROZEN" in reason, key


def test_check_target_setting_frozen_message_wins_when_membership_would_otherwise_pass(
        monkeypatch):
    """Belt-and-suspenders (binding rule 7): simulate a FUTURE SETTINGS_SPEC edit that
    lists a frozen knob anyway — check_target must still reject it, with the FROZEN
    message (not silently accept it just because membership now passes)."""
    fake_spec = dict(config.SETTINGS_SPEC)
    fake_spec["autonomy.push_approval"] = bool
    monkeypatch.setattr(config, "SETTINGS_SPEC", fake_spec)
    ok, reason = hs.check_target("setting", "autonomy.push_approval")
    assert ok is False and "FROZEN" in reason


# -- check_target: kind='prompt' -------------------------------------------------------------
def test_check_target_prompt_accepts_a_non_frozen_role_prompt_path():
    ok, reason = hs.check_target("prompt", "roles/organizer/prompt.md")
    assert ok is True and reason == ""


def test_check_target_prompt_rejects_a_non_roles_path():
    ok, reason = hs.check_target("prompt", "common/config.py")
    assert ok is False
    assert "roles/" in reason


# -- FROZEN_ROLE_PROMPTS (adversarial-review fix round, item 8) -----------------------------
def test_check_target_prompt_rejects_its_own_prompt_meta_harness_exclusion():
    """The design's own YAGNI: the loop must never tune the very prompt that states its
    own bounds. This ALSO fixes a test that used to (wrongly) assert this target was
    ACCEPTABLE — see test_check_target_prompt_accepts_a_non_frozen_role_prompt_path above
    for the now-legitimate accept example."""
    ok, reason = hs.check_target("prompt", "roles/harness_engineer/prompt.md")
    assert ok is False
    assert "FROZEN" in reason and "meta-harness" in reason.lower()


def test_check_target_prompt_rejects_every_verifier_gate_role_prompt():
    for target in ("roles/reviewer/prompt.md", "roles/scope_check/prompt.md",
                   "roles/decompose/prompt.md", "roles/judge/prompt.md"):
        ok, reason = hs.check_target("prompt", target)
        assert ok is False and "FROZEN" in reason, target
        assert target in hs.FROZEN_ROLE_PROMPTS


def test_frozen_role_prompts_only_names_roles_that_actually_exist():
    """A frozen entry that doesn't correspond to a real roles/<x>/prompt.md file would be
    silent dead weight — assert every entry resolves on disk."""
    import os

    from factory.common import paths
    for target in hs.FROZEN_ROLE_PROMPTS:
        assert os.path.isfile(os.path.join(paths.FACTORY_ROOT, target)), target


# -- path-traversal / shape hardening (adversarial-review fix round, item 8a) ---------------
def test_check_target_prompt_rejects_the_reported_traversal_probe():
    """The EXACT probe the adversarial review reported: a target that starts with
    'roles/' and ends with '/prompt.md' (passing the OLD naive shape check) but
    normpath-collapses OUTSIDE roles/ entirely, landing on a frozen file — the OLD
    _is_frozen check matched against the RAW (untraversed) string and never fired."""
    ok, reason = hs.check_target("prompt", "roles/../common/code_gate.py/prompt.md")
    assert ok is False
    assert "roles/<x>/prompt.md" in reason


def test_check_target_prompt_rejects_nested_traversal():
    ok, reason = hs.check_target("prompt", "roles/x/../../common/code_gate.py/prompt.md")
    assert ok is False


def test_check_target_prompt_rejects_nested_path_segments():
    """A target with MORE than one segment under roles/ must reject even with no
    traversal at all — _PROMPT_TARGET_RE requires exactly one segment."""
    ok, reason = hs.check_target("prompt", "roles/a/b/prompt.md")
    assert ok is False


def test_check_target_prompt_rejects_an_absolute_path():
    ok, reason = hs.check_target("prompt", "/roles/organizer/prompt.md")
    assert ok is False


def test_check_target_prompt_accepts_a_legitimately_odd_but_valid_role_name():
    # "tests" would be a legal (if odd) role slug under the shape/traversal rules alone —
    # prove the regex+normpath gate doesn't over-reject a single clean segment. It still
    # doesn't correspond to a REAL prompt file, but check_target is a pure surface check,
    # not an existence check (existence isn't checked for any 'prompt' target — the
    # operator applying a nonexistent role's patch by hand would notice immediately).
    ok, reason = hs.check_target("prompt", "roles/tests/prompt.md")
    assert ok is True


def test_check_target_prompt_rejects_common_store_py_via_frozen_surfaces():
    """Direct exercise of the FROZEN_SURFACES glob against a real frozen path (bypassing
    the roles/ shape gate is impossible for 'prompt' kind — so this proves the underlying
    _is_frozen check independently, mirroring frozen_source's own test style)."""
    from factory.common.frozen_source import _is_frozen
    assert _is_frozen("common/store.py", hs.FROZEN_SURFACES)
    assert _is_frozen("common/bus.py", hs.FROZEN_SURFACES)              # exact path
    assert _is_frozen("common/budget.py", hs.FROZEN_SURFACES)           # exact path
    assert _is_frozen("dashboard/fleet_server.py", hs.FROZEN_SURFACES)   # dir-prefix
    assert _is_frozen("vendor/anything.py", hs.FROZEN_SURFACES)         # dir-prefix
    assert not _is_frozen("roles/harness_engineer/prompt.md", hs.FROZEN_SURFACES)


# -- check_target: kind='learning_corrective' -------------------------------------------------
def test_check_target_learning_corrective_accepts_learning_colon_int():
    ok, reason = hs.check_target("learning_corrective", "learning:42")
    assert ok is True and reason == ""


def test_check_target_learning_corrective_rejects_missing_prefix():
    ok, reason = hs.check_target("learning_corrective", "42")
    assert ok is False and "learning:" in reason


def test_check_target_learning_corrective_rejects_non_integer_id():
    ok, reason = hs.check_target("learning_corrective", "learning:abc")
    assert ok is False and "integer" in reason


# -- check_target: general -----------------------------------------------------------------
def test_check_target_rejects_empty_target():
    ok, reason = hs.check_target("setting", "")
    assert ok is False and "empty" in reason


def test_check_target_rejects_unknown_kind():
    ok, reason = hs.check_target("bogus", "super_worker.max_parallel")
    assert ok is False and "bogus" in reason
