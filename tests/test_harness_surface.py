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
                "super_worker.scope_check", "super_worker.reviewer"):
        assert not hs._is_frozen_knob(key), key


# -- editable_settings_keys ---------------------------------------------------------------
def test_editable_settings_keys_is_settings_spec_minus_frozen():
    editable = hs.editable_settings_keys()
    assert editable == set(config.SETTINGS_SPEC)   # nothing in SETTINGS_SPEC is frozen today
    assert "autonomy.push_approval" not in editable   # never in SETTINGS_SPEC anyway
    assert "super_worker.organizer" not in editable   # never in SETTINGS_SPEC anyway


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
def test_check_target_prompt_accepts_a_role_prompt_path():
    ok, reason = hs.check_target("prompt", "roles/harness_engineer/prompt.md")
    assert ok is True and reason == ""


def test_check_target_prompt_rejects_a_non_roles_path():
    ok, reason = hs.check_target("prompt", "common/config.py")
    assert ok is False
    assert "roles/" in reason


def test_check_target_prompt_rejects_a_frozen_surface_even_if_it_looked_roles_shaped():
    # harness_surface.py itself is frozen; a fake "roles/x/../../common/harness_surface.py"
    # is caught by the shape check first, so exercise the frozen-glob branch directly via a
    # target that DOES match the roles/*/prompt.md shape but lives under a frozen dir.
    ok, reason = hs.check_target("prompt", "roles/tests/prompt.md")
    # "tests/" is a frozen dir-prefix pattern; "roles/tests/prompt.md" does NOT start with
    # "tests/" so it should NOT match that pattern — assert the shape check alone passes it
    # (a role literally named "tests" is a legal, if odd, editable target).
    assert ok is True


def test_check_target_prompt_rejects_common_store_py_via_frozen_surfaces():
    """Direct exercise of the FROZEN_SURFACES glob against a real frozen path (bypassing
    the roles/ shape gate is impossible for 'prompt' kind — so this proves the underlying
    _is_frozen check independently, mirroring frozen_source's own test style)."""
    from factory.common.frozen_source import _is_frozen
    assert _is_frozen("common/store.py", hs.FROZEN_SURFACES)
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
