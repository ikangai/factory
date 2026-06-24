"""Target-adapter seam tests (mission axis A: repo-agnostic factory).

Proves the adapter abstraction is a pure indirection over the legacy clive path:
  (a) get_adapter() returns the clive adapter by default,
  (b) the clive adapter actuates a spec BYTE-FOR-BYTE identically to the legacy
      common.spec_applier.apply_spec path,
  (c) the adapter is selected by config.target.provider, and an unknown provider
      is rejected (not silently defaulted).
"""
import os
import tempfile
from dataclasses import asdict

from factory.common import config, spec_applier
from factory.adapters import get_adapter as adapters_get_adapter
from factory.adapters.base import TargetAdapter
from factory.adapters.clive import CliveAdapter


_SPEC = {
    "open": {
        "system_prompt": "be terse",
        "command_affordances": {"toolset": "standard", "progressive_disclosure": True},
        "observation_policy": {"streaming": True, "ps1_exitcode": False},
        "recovery_policy": {"max_turns": 7},
        "skills": [{"name": "clive-rooms"}],
    },
    "frozen": {},
    "meta": {},
}


def test_get_adapter_defaults_to_clive():
    """(a) default provider -> CliveAdapter; both factory entry points agree."""
    adapter = config.get_adapter()
    assert isinstance(adapter, CliveAdapter)
    assert isinstance(adapter, TargetAdapter)
    assert adapter.name == "clive"
    # The adapters package re-export is the same factory.
    assert isinstance(adapters_get_adapter(), CliveAdapter)


def test_clive_adapter_actuates_identically_to_legacy():
    """(b) adapter.actuate == legacy spec_applier.apply_spec, byte-for-byte.

    Run both into SEPARATE run dirs so the system_prompt override file path is the
    only legitimate difference, then normalise that path before comparing — every
    other field (flags, env knobs, pending, notes) must be identical."""
    adapter = CliveAdapter()
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        legacy = spec_applier.apply_spec(_SPEC, d1, "minimal")
        viaad = adapter.actuate(_SPEC, d2, "minimal")

        lg, ad = asdict(legacy), asdict(viaad)
        # The driver-override file lives under each run dir; normalise its dir so
        # only its presence/basename is compared (the actuation logic is identical).
        for blob, root in ((lg, d1), (ad, d2)):
            ov = blob["env"].get("CLIVE_EVAL_DRIVER_OVERRIDE")
            if ov:
                assert ov.startswith(root)
                blob["env"]["CLIVE_EVAL_DRIVER_OVERRIDE"] = os.path.basename(ov)
        assert ad == lg
        # And the actuation actually did something non-trivial (guards a no-op).
        assert "-t" in viaad.flags and "standard" in viaad.flags
        assert viaad.pending  # recovery_policy.max_turns recorded as pending


def test_adapter_selected_by_target_provider(monkeypatch):
    """(c) selection is driven by config.target.provider; unknown -> error."""
    # Default (clive) resolves.
    monkeypatch.setattr(config, "target_config", lambda: {"provider": "clive"})
    assert isinstance(config.get_adapter(), CliveAdapter)

    # An unregistered provider must be rejected, not silently defaulted.
    monkeypatch.setattr(config, "target_config", lambda: {"provider": "acme-repo"})
    try:
        config.get_adapter()
    except ValueError as e:
        assert "acme-repo" in str(e)
    else:
        raise AssertionError("get_adapter accepted an unknown provider")


def test_target_config_backcompat_legacy_clive_block(monkeypatch):
    """Back-compat: a legacy top-level `clive:` block (no `target:`) is treated as
    the clive target and yields the clive adapter."""
    monkeypatch.setattr(
        config, "load_config",
        lambda: {"clive": {"root": "../clive", "entry": "clive.py",
                           "default_toolset": "minimal"}})
    tc = config.target_config()
    assert tc["provider"] == "clive"
    assert tc["root"] == "../clive"
    assert isinstance(config.get_adapter(), CliveAdapter)
