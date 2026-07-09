"""scripts/configure_instance.py: the per-instance config.yaml patcher behind the single-line
installer (docs/plans/2026-07-09-single-line-installer-design.md). Same discipline as
deploy/user-factory/apply-config-overlay.py (block-scoped, exactly-once, comment-preserving,
yaml-reparse-and-assert) but SETS parameterized values instead of a fixed 4-literal overlay.

Mirrors tests/test_deploy_kit.py / tests/test_vendored_bus.py's approach: real subprocess
calls against the shipped script, hermetic tmp_path copies of the real config.yaml — never
the real store or the real ~/factories. `assert_effective` (the final yaml-assert gate) is
additionally unit-tested by importing the module directly, since forcing a "bad patch" through
the CLI boundary alone can't exercise that specific safety net.
"""
import glob
import importlib.util
import os
import shutil
import socket
import subprocess

import pytest
import yaml

from factory.common import paths

SCRIPT = os.path.join(paths.FACTORY_ROOT, "scripts", "configure_instance.py")
REAL_CONFIG = paths.CONFIG_YAML


def _load_module():
    spec = importlib.util.spec_from_file_location("configure_instance", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _instance_config(root, name):
    """<root>/<name>/factory/config.yaml, a copy of the real config.yaml — mirrors the
    installer's forced per-instance layout (bin/factory requires the clone be named
    `factory`), which is exactly the glob configure_instance.py --list relies on."""
    d = root / name / "factory"
    d.mkdir(parents=True)
    dest = d / "config.yaml"
    shutil.copy(REAL_CONFIG, dest)
    return dest


def _run(args):
    return subprocess.run(["python3", SCRIPT, *args], capture_output=True, text=True, timeout=30)


def _patch(cfg, root, *, target_root="../t", provider="clive", base_branch="factory/base",
           port="auto", port_base=None):
    args = [str(cfg), "--target-root", target_root, "--provider", provider,
            "--base-branch", base_branch, "--port", port, "--instances-root", str(root)]
    if port_base is not None:
        # Probing bind-tests REAL host ports; an uncommon per-test base keeps the literal
        # port assertions hermetic on any machine (8787 may legitimately be busy here).
        args += ["--port-base", str(port_base)]
    return _run(args)


# --- comment-preserving SET ------------------------------------------------------------
def test_patch_preserves_comments_and_sets_requested_values(tmp_path):
    cfg = _instance_config(tmp_path, "acme")
    original = cfg.read_text()
    assert "# clive-harness-factory configuration (Phase 0)." in original

    r = _patch(cfg, tmp_path, target_root="../acme-target", provider="acme",
               base_branch="factory/base", port="9001")
    assert r.returncode == 0, r.stderr

    patched = cfg.read_text()
    # the file header and a couple of WHY-comments elsewhere in the file survive verbatim
    assert "# clive-harness-factory configuration (Phase 0)." in patched
    assert "# Everything is files + a SQLite" in patched
    assert "the required posture for an UNATTENDED run" in patched  # super_worker block prose
    assert "agent's OWN claude" in patched  # inline comment on the claude_bin line itself

    doc = yaml.safe_load(patched)
    assert doc["target"]["root"] == "../acme-target"
    assert doc["target"]["provider"] == "acme"
    assert doc["target"]["base_branch"] == "factory/base"
    assert doc["dashboard"]["port"] == 9001
    assert doc["autopilot"]["prod"] is False
    assert doc["super_worker"]["user"] == ""
    assert doc["super_worker"]["claude_bin"] == "claude"

    assert r.stdout.strip() == "PORT=9001"  # the ONE machine-readable stdout line


# --- exactly-once drift guard -----------------------------------------------------------
def test_duplicated_field_line_in_a_block_is_a_loud_non_zero_failure(tmp_path):
    cfg = _instance_config(tmp_path, "acme")
    text = cfg.read_text()
    assert text.count("  port: 8787\n") == 1
    text = text.replace("  port: 8787\n", "  port: 8787\n  port: 8787\n", 1)
    cfg.write_text(text)

    r = _patch(cfg, tmp_path, port="9001")
    assert r.returncode != 0
    assert "dashboard.port" in r.stderr
    assert "exactly once" in r.stderr.lower() or "expected exactly" in r.stderr.lower()
    # a failed patch must never partially write the file
    assert cfg.read_text() == text


# --- idempotent install (auto) + update (keep) --------------------------------------------
def test_fresh_auto_probes_then_keep_rerun_is_a_content_noop(tmp_path):
    """Fresh install (--port auto) PROBES — the repo-default 8787 in a fresh clone says
    nothing about what's free on this machine; the update re-run (--port keep, what
    install.sh passes) then keeps the assigned port without churn."""
    cfg = _instance_config(tmp_path, "acme")

    r1 = _patch(cfg, tmp_path, target_root="../acme-target", port="auto", port_base=43870)
    assert r1.returncode == 0, r1.stderr
    assert r1.stdout.strip() == "PORT=43870"  # nothing taken in the test range -> the base
    after_first = cfg.read_text()

    r2 = _patch(cfg, tmp_path, target_root="../acme-target", port="keep")
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == "PORT=43870"
    assert cfg.read_text() == after_first


def test_fresh_auto_does_bind_test_even_with_no_siblings(tmp_path):
    """The first instance must not inherit a busy port just because it has no siblings —
    auto always bind-tests (this was the 'probe loop is dead code for instance #1' bug)."""
    cfg = _instance_config(tmp_path, "acme")
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 43890))
    held.listen(1)
    try:
        r = _patch(cfg, tmp_path, target_root="../acme-target", port="auto", port_base=43890)
    finally:
        held.close()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "PORT=43900"  # base bound -> next pair


# --- auto-port vs siblings (dashboard AND implied fleet ports) -----------------------------
def test_auto_port_skips_a_sibling_instances_port_pair(tmp_path):
    sibling = _instance_config(tmp_path, "sibling")
    r = _patch(sibling, tmp_path, target_root="../sibling-target", port="43910")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "PORT=43910"

    other = _instance_config(tmp_path, "other")
    r2 = _patch(other, tmp_path, target_root="../other-target", port="auto", port_base=43910)
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == "PORT=43920"  # first free pair after the sibling's
    assert yaml.safe_load(other.read_text())["dashboard"]["port"] == 43920


def test_auto_port_skips_a_siblings_implied_fleet_port(tmp_path):
    """A sibling on an off-grid explicit port implies fleet port +1; a probed pair must
    dodge BOTH (this was the fleet-port blind spot)."""
    sibling = _instance_config(tmp_path, "sibf")
    _patch(sibling, tmp_path, target_root="../sibf-target", port="43931")  # fleet = 43932

    other = _instance_config(tmp_path, "otherf")
    # base pair (43930, 43931): 43931 is the sibling's dashboard port -> skip;
    # next pair (43940, 43941) is clean.
    r = _patch(other, tmp_path, target_root="../otherf-target", port="auto", port_base=43930)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "PORT=43940"


def test_keep_never_reassigns_a_non_colliding_port(tmp_path):
    """--port keep (the update path): a previously-assigned port that doesn't collide with
    any sibling is kept with NO bind test — its own live board legitimately holds it, and
    a bind test cannot tell 'my own board' from 'someone else's process'."""
    mine = _instance_config(tmp_path, "mine")
    r1 = _patch(mine, tmp_path, target_root="../mine-target", port="9500")
    assert r1.returncode == 0, r1.stderr

    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # simulate the LIVE board
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 9500))
    held.listen(1)
    try:
        r2 = _patch(mine, tmp_path, target_root="../mine-target", port="keep")
    finally:
        held.close()
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == "PORT=9500"  # kept even while bound — it's OUR board


def test_keep_reassigns_only_on_a_sibling_collision(tmp_path):
    mine = _instance_config(tmp_path, "minec")
    _patch(mine, tmp_path, target_root="../minec-target", port="43950")

    sibling = _instance_config(tmp_path, "sibc")  # operator forced the same port explicitly
    _patch(sibling, tmp_path, target_root="../sibc-target", port="43950")

    r = _patch(mine, tmp_path, target_root="../minec-target", port="keep", port_base=43950)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "PORT=43960"  # collides now -> probed to the next free pair


# --- explicit port collision --------------------------------------------------------------
def test_explicit_port_collision_warns_but_proceeds(tmp_path):
    sibling = _instance_config(tmp_path, "sib4")
    _patch(sibling, tmp_path, target_root="../sib4-target", port="43970")

    fresh = _instance_config(tmp_path, "fourth")
    r = _patch(fresh, tmp_path, target_root="../fourth-target", port="43970")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "PORT=43970"
    assert "collide" in r.stderr.lower()


def test_explicit_port_warns_on_a_fleet_port_collision_too(tmp_path):
    sibling = _instance_config(tmp_path, "sib5")
    _patch(sibling, tmp_path, target_root="../sib5-target", port="43980")  # fleet = 43981

    fresh = _instance_config(tmp_path, "fifth")
    r = _patch(fresh, tmp_path, target_root="../fifth-target", port="43981")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "PORT=43981"
    assert "collide" in r.stderr.lower()


# --- --list ---------------------------------------------------------------------------------
def test_list_includes_name_root_provider_ports_and_mode(tmp_path):
    alpha = _instance_config(tmp_path, "alpha")
    _patch(alpha, tmp_path, target_root="../alpha-target", provider="clive", port="9111")
    (alpha.parent / ".factory-mode").write_text("shift\n")

    beta = _instance_config(tmp_path, "beta")
    _patch(beta, tmp_path, target_root="../beta-target", provider="clive", port="9121")
    # beta has NO .factory-mode -> mode falls back to "-"

    r = _run(["--list", "--instances-root", str(tmp_path)])
    assert r.returncode == 0, r.stderr

    lines = {l.split()[0]: l for l in r.stdout.splitlines() if l.strip()}
    assert "alpha" in lines["alpha"] and "../alpha-target" in lines["alpha"]
    assert "target=?" in lines["alpha"]  # no git repo behind the root -> failure-tolerant "?"
    assert "port=9111" in lines["alpha"] and "fleet_port=9112" in lines["alpha"]
    assert "mode=shift" in lines["alpha"]
    assert "port=9121" in lines["beta"] and "fleet_port=9122" in lines["beta"]
    assert "mode=-" in lines["beta"]


def test_list_with_no_instances_says_so(tmp_path):
    r = _run(["--list", "--instances-root", str(tmp_path / "empty")])
    assert r.returncode == 0, r.stderr
    assert "no instances under" in r.stdout.lower()


# --- the final yaml re-parse + assert (the actual correctness gate) -----------------------
def test_final_yaml_assert_catches_a_bad_patch():
    mod = _load_module()
    doc = {"target": {"root": "../wrong"}, "dashboard": {"port": 1}}
    expected = {("target", "root"): "../right", ("dashboard", "port"): 1}
    with pytest.raises(SystemExit):
        mod.assert_effective(doc, expected)


def test_final_yaml_assert_passes_when_everything_matches():
    mod = _load_module()
    doc = {"target": {"root": "../right"}, "dashboard": {"port": 1}}
    expected = {("target", "root"): "../right", ("dashboard", "port"): 1}
    mod.assert_effective(doc, expected)  # must not raise
