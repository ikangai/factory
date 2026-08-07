"""F1 (round-2 integration fix, docs/plans/2026-08-06-publication-broker-design.md):
the deployment must resolve the SAME spool/bare paths on both the broker's side (the
plist's EnvironmentVariables, rendered by 04-install-broker-agent.sh) and the factory's
side (common/paths.py's own resolution) — a silent mismatch was a permanent no-op (the
broker polls a real, empty directory forever). This exercises the REAL installer script
(--dry-run: no filesystem writes, no launchctl calls) and compares its rendered
substitutions against what common.paths resolves for the identical spool root.
"""
import re
import subprocess

from factory.common import paths

SCRIPT = paths.factory("deploy", "user-factory", "04-install-broker-agent.sh")


def _run_dry(spool):
    return subprocess.run(["bash", SCRIPT, "--dry-run", "--spool", spool],
                          capture_output=True, text=True, timeout=30)


def _grep(text, token):
    m = re.search(rf"{re.escape(token)}\s+-> (\S+)", text)
    assert m, f"{token} not found in installer output:\n{text}"
    return m.group(1)


def test_installer_never_writes_anything_in_dry_run(tmp_path):
    spool = str(tmp_path / "spool")
    out = _run_dry(spool)
    assert out.returncode == 0, out.stderr
    assert not (tmp_path / "spool").exists()


def test_installer_rendered_outbox_matches_paths_py_resolution(tmp_path):
    spool = str(tmp_path / "spool")
    out = _run_dry(spool)
    rendered = _grep(out.stdout, "__OUTBOX_DIR__")
    assert rendered == paths.broker_outbox_dir(root=spool)


def test_installer_rendered_spool_root_matches_paths_py_resolution(tmp_path):
    spool = str(tmp_path / "spool")
    out = _run_dry(spool)
    rendered = _grep(out.stdout, "__SPOOL_ROOT__")
    assert rendered == paths.broker_spool_root(spool) == spool


def test_installer_rendered_bare_repo_matches_paths_py_resolution(tmp_path):
    spool = str(tmp_path / "spool")
    out = _run_dry(spool)
    rendered = _grep(out.stdout, "__BARE_REPO__")
    assert rendered == paths.broker_bare_repo(spool)


def test_installer_prints_the_config_yaml_follow_up_line(tmp_path):
    """The critical F1 fix: the operator must be told, explicitly, what to paste into the
    FACTORY side's config.yaml — the script itself cannot reach that account."""
    spool = str(tmp_path / "spool")
    out = _run_dry(spool)
    assert "broker_spool_root" in out.stdout
    assert spool in out.stdout
