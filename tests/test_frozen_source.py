"""Frozen-source validator — the CODE-level analog of the spec's frozen block
(design: docs/plans/2026-06-25-autonomous-code-factory.md).

When the factory gains authority to change the target's SOURCE, a candidate diff that
touches a safety-critical path (the command sandbox, permission gates, destructive-action
policy, the self-mod gate) is AUTO-REJECTED before it is ever graded — so the autonomous
factory can make the target better but can never weaken its own safety. Deterministic;
no live agent, no target dependency (the frozen path-set is config, supplied by the
Target Adapter's frozen_paths()).
"""
from factory.common import frozen_source as fz


def test_diff_parser_extracts_changed_paths():
    diff = (
        "diff --git a/clive/tui.py b/clive/tui.py\n"
        "--- a/clive/tui.py\n+++ b/clive/tui.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/clive/sandbox/run.sh b/clive/sandbox/run.sh\n"
        "--- a/clive/sandbox/run.sh\n+++ b/clive/sandbox/run.sh\n"
    )
    assert fz.changed_paths_from_diff(diff) == ["clive/sandbox/run.sh", "clive/tui.py"]


def test_added_and_deleted_files_parsed():
    diff = (
        "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
    )
    assert fz.changed_paths_from_diff(diff) == ["gone.py", "new.py"]


def test_frozen_path_rejected():
    ok, violations = fz.validate_code_candidate(
        changed_paths=["clive/sandbox/run.sh", "clive/tui.py"],
        frozen_patterns=["clive/sandbox/**"])
    assert not ok
    assert violations == ["clive/sandbox/run.sh"]


def test_clean_change_passes():
    ok, violations = fz.validate_code_candidate(
        changed_paths=["clive/tui.py", "clive/helpers.py"],
        frozen_patterns=["clive/sandbox/**", "runtime.py"])
    assert ok and violations == []


def test_matches_exact_glob_and_dir_prefix():
    frozen = ["runtime.py", "**/safety*.py", "clive/sandbox"]
    assert fz.frozen_violations(["runtime.py"], frozen) == ["runtime.py"]          # exact
    assert fz.frozen_violations(["clive/safety_battery.py"], frozen) == \
        ["clive/safety_battery.py"]                                                # glob
    assert fz.frozen_violations(["clive/sandbox/profile.sb"], frozen) == \
        ["clive/sandbox/profile.sb"]                                               # dir prefix
    assert fz.frozen_violations(["clive/ui/widget.py"], frozen) == []             # clean


def test_validate_from_raw_diff():
    diff = ("diff --git a/clive/permissions.py b/clive/permissions.py\n"
            "--- a/clive/permissions.py\n+++ b/clive/permissions.py\n")
    ok, violations = fz.validate_code_candidate(diff_text=diff,
                                                frozen_patterns=["clive/permissions.py"])
    assert not ok and violations == ["clive/permissions.py"]
