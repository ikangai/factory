"""install.sh: the single-line installer (docs/plans/2026-07-09-single-line-installer-design.md).

Hermetic end-to-end: a synthetic local 'target' repo stands in for a real GitHub target, and
--factory-repo points at THIS checkout (a local-path clone hits no network) on the CURRENT
branch (git clone of a local path pulls every ref, so origin/<branch> resolves even though
main doesn't have install.sh yet). HOME is redirected to a tmp dir so the launcher never
touches the real ~/.local/bin. Mirrors tests/test_bin_factory_bus.py / test_vendored_bus.py's
"real subprocess, hermetic env" approach.

The first install is expensive (two git clones + pip-skip + init + smoke), so it runs ONCE via
a module-scoped fixture; every other assertion in this file reads its result instead of
re-installing.
"""
import os
import stat
import subprocess

import pytest
import yaml

from factory.common import paths

INSTALL_SH = os.path.join(paths.FACTORY_ROOT, "install.sh")


def test_install_sh_syntax_is_valid():
    r = subprocess.run(["bash", "-n", INSTALL_SH], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


def _current_branch() -> str:
    r = subprocess.run(["git", "-C", paths.FACTORY_ROOT, "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, timeout=10, check=True)
    return r.stdout.strip()


def _make_synthetic_target(base_dir, name):
    """A tiny hermetic stand-in target repo: git init, one commit, no requirements.txt,
    default branch 'main' — install.sh must never touch a real network target in a test."""
    d = base_dir / name
    d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True, timeout=10)
    (d / "README.md").write_text(f"# {name}\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", "-C", str(d), "add", "README.md"], check=True, timeout=10, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "init"], check=True, timeout=10, env=env)
    return d


def _run_install(args, home):
    # A fresh HOME (no ~/.gitconfig) also exercises install.sh's git-identity fallback for the
    # step-6 config.yaml commit — the same fallback 02-bootstrap-as-factory.sh uses.
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(["bash", INSTALL_SH, *args], capture_output=True, text=True,
                           env=env, timeout=300)


@pytest.fixture(scope="module")
def install_env(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    root = tmp_path_factory.mktemp("root") / "factories"
    target_repo = _make_synthetic_target(tmp_path_factory.mktemp("targets"), "widget")
    branch = _current_branch()

    r = _run_install([
        "--factory-repo", paths.FACTORY_ROOT,
        "--branch", branch,
        "--target", str(target_repo),
        "--root", str(root),
        "--skip-deps",
    ], home)
    assert r.returncode == 0, f"install.sh failed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"

    return {"home": home, "root": root, "target_repo": target_repo, "branch": branch,
            "name": "widget", "first_run": r}


def _factory_dir(env):
    return env["root"] / env["name"] / "factory"


def _target_dir(env):
    return env["root"] / env["name"] / env["name"]


def test_layout_is_one_parent_dir_per_instance(install_env):
    assert (_factory_dir(install_env) / "bin" / "factory").exists()
    assert (_target_dir(install_env) / ".git").is_dir()


def test_factory_clone_is_on_the_instance_branch(install_env):
    r = subprocess.run(["git", "-C", str(_factory_dir(install_env)), "branch", "--show-current"],
                        capture_output=True, text=True, timeout=10, check=True)
    assert r.stdout.strip() == f"instance/{install_env['name']}"


def test_config_yaml_is_patched(install_env):
    doc = yaml.safe_load((_factory_dir(install_env) / "config.yaml").read_text())
    assert doc["target"]["root"] == "../widget"
    assert doc["target"]["provider"] == "clive"
    assert isinstance(doc["dashboard"]["port"], int)
    assert doc["autopilot"]["prod"] is False


def test_base_branch_exists_in_the_target_clone(install_env):
    r = subprocess.run(["git", "-C", str(_target_dir(install_env)), "branch", "--show-current"],
                        capture_output=True, text=True, timeout=10, check=True)
    # widget != clive -> the else-branch default: factory/base
    assert r.stdout.strip() == "factory/base"


def test_factory_mode_defaults_to_shift(install_env):
    assert (_factory_dir(install_env) / ".factory-mode").read_text().strip() == "shift"


def test_launcher_exists_and_is_executable(install_env):
    launcher = install_env["home"] / ".local" / "bin" / f"factory-{install_env['name']}"
    assert launcher.exists()
    assert launcher.stat().st_mode & stat.S_IXUSR
    text = launcher.read_text()
    assert text.splitlines()[0] == "#!/usr/bin/env bash"
    assert str(_factory_dir(install_env)) in text


def test_store_is_initialized(install_env):
    assert (_factory_dir(install_env) / "store" / "blackboard.db").exists()


def test_bin_factory_status_exits_zero(install_env):
    r = subprocess.run([str(_factory_dir(install_env) / "bin" / "factory"), "status"],
                        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_rerun_is_idempotent_and_keeps_the_port(install_env):
    cfg = _factory_dir(install_env) / "config.yaml"
    before_port = yaml.safe_load(cfg.read_text())["dashboard"]["port"]

    r = _run_install([
        "--factory-repo", paths.FACTORY_ROOT,
        "--branch", install_env["branch"],
        "--target", str(install_env["target_repo"]),
        "--root", str(install_env["root"]),
        "--skip-deps",
    ], install_env["home"])
    assert r.returncode == 0, f"re-run failed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"

    after_port = yaml.safe_load(cfg.read_text())["dashboard"]["port"]
    assert after_port == before_port


def test_second_instance_gets_a_non_colliding_port(install_env, tmp_path_factory):
    target2 = _make_synthetic_target(tmp_path_factory.mktemp("targets2"), "gadget")

    r = _run_install([
        "--factory-repo", paths.FACTORY_ROOT,
        "--branch", install_env["branch"],
        "--target", str(target2),
        "--name", "gadget-instance",
        "--root", str(install_env["root"]),
        "--skip-deps",
    ], install_env["home"])
    assert r.returncode == 0, f"second install failed\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"

    port1 = yaml.safe_load((_factory_dir(install_env) / "config.yaml").read_text())["dashboard"]["port"]
    cfg2 = install_env["root"] / "gadget-instance" / "factory" / "config.yaml"
    port2 = yaml.safe_load(cfg2.read_text())["dashboard"]["port"]
    assert port1 != port2

    r2 = _run_install(["list", "--root", str(install_env["root"])], install_env["home"])
    assert r2.returncode == 0, r2.stderr
    assert install_env["name"] in r2.stdout
    assert "gadget-instance" in r2.stdout
