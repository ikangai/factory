"""Clive's frozen surface is derived from clive's OWN constitution — the IMMUTABLE +
GOVERNANCE entries in selfmod/constitution.py's FILE_TIERS — resolved to repo-root
paths, PLUS the command-blocklist + sandbox (execution/runtime.py, sandbox/run.sh).

This is the "read clive's own constitution" choice: self-syncing with clive's governance
(clive's own gate is then a second enforcement layer), and CORE/STANDARD files stay
editable (the factory may develop those). Pure parse — no importing clive.
"""
import os

import pytest

from factory.adapters import clive as clive_adapter
from factory.common import frozen_source as fz

SYNTH = '''
TIERS = {"IMMUTABLE": {}, "GOVERNANCE": {}}
FILE_TIERS: list[tuple[str, str]] = [   # annotated, exactly like clive's real form
    ("selfmod/gate.py", "IMMUTABLE"),
    (".clive/constitution.md", "IMMUTABLE"),
    (".clive/audit/", "IMMUTABLE"),
    ("selfmod/", "GOVERNANCE"),
    (".env", "GOVERNANCE"),
    ("clive.py", "CORE"),
    ("tui.py", "STANDARD"),
]
'''


def test_extracts_immutable_and_governance_to_repo_paths():
    frozen = clive_adapter._frozen_from_constitution(SYNTH)
    assert "src/clive/selfmod/gate.py" in frozen      # IMMUTABLE source file
    assert "src/clive/selfmod/" in frozen             # GOVERNANCE dir → whole selfmod frozen
    assert ".clive/constitution.md" in frozen          # repo-root dotdir (not under src/)
    assert ".clive/audit/" in frozen
    assert ".env" in frozen
    # the command blocklist + sandbox are ALWAYS frozen (the user's explicit add)
    assert "src/clive/execution/runtime.py" in frozen
    assert "src/clive/sandbox/run.sh" in frozen
    # CORE / STANDARD are NOT frozen — the factory may develop clive's actual features
    assert "src/clive/clive.py" not in frozen
    assert "src/clive/tui.py" not in frozen


def test_malformed_constitution_still_freezes_the_hard_safety_files():
    frozen = clive_adapter._frozen_from_constitution("this is not python {[")
    assert "src/clive/execution/runtime.py" in frozen and "src/clive/sandbox/run.sh" in frozen


def test_validator_rejects_a_diff_touching_clive_safety():
    """End-to-end: clive's constitution → frozen set → the validator rejects a diff
    that touches the self-mod gate."""
    frozen = clive_adapter._frozen_from_constitution(SYNTH)
    diff = ("diff --git a/src/clive/selfmod/gate.py b/src/clive/selfmod/gate.py\n"
            "--- a/src/clive/selfmod/gate.py\n+++ b/src/clive/selfmod/gate.py\n")
    ok, violations = fz.validate_code_candidate(diff_text=diff, frozen_patterns=frozen)
    assert not ok and violations == ["src/clive/selfmod/gate.py"]

    # a feature change to a STANDARD file is allowed
    ok2, _ = fz.validate_code_candidate(
        diff_text="diff --git a/src/clive/tui.py b/src/clive/tui.py\n"
                  "--- a/src/clive/tui.py\n+++ b/src/clive/tui.py\n",
        frozen_patterns=frozen)
    assert ok2


@pytest.mark.skipif(not os.path.isdir("../clive/src/clive/selfmod"),
                    reason="clive target not present")
def test_real_clive_constitution_freezes_the_gate_and_runtime():
    frozen = clive_adapter.CliveAdapter().frozen_paths()
    assert any("selfmod" in p for p in frozen)                 # selfmod frozen
    assert "src/clive/execution/runtime.py" in frozen          # command blocklist frozen
