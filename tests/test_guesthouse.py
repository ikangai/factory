"""Tests for Phase 0 of the production-hardening roadmap
(docs/plans/2026-08-06-production-hardening-roadmap.md): the guest-house guided install.

Four groups, matching the roadmap's deliverable E:
  (a) install.sh --guest-house wiring — syntax + grep-level contract + byte-compatibility of
      the pre-existing plain-mode flow (the ACTUAL runtime behavior of plain mode is already
      covered end-to-end by tests/test_install_sh.py, which this file does not duplicate or
      touch).
  (b) scripts/guesthouse_check.py — the deterministic doctor: every probe's PASS/FAIL/SKIP
      path via injected Ctx fields (no test needs root, another user, or a real mutation),
      plus the table renderer, main()'s exit codes, and --json shape.
  (c) install.ps1 — static sanity only (no PowerShell available on this host to execute it):
      file integrity (no NUL/CRLF), a crude brace-balance count, and presence of the
      EXPERIMENTAL banner / Set-StrictMode / wsl.conf hardening keys.
  (d) README.md — both one-liners are present.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from factory.common import paths

INSTALL_SH = paths.factory("install.sh")
INSTALL_PS1 = paths.factory("install.ps1")
README = paths.factory("README.md")
GUESTHOUSE_CHECK = paths.factory("scripts", "guesthouse_check.py")
RUNBOOK = paths.factory("docs", "runbooks", "guest-house.md")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# =============================================================================================
# (a) install.sh --guest-house wiring
# =============================================================================================
def test_install_sh_syntax_still_valid():
    r = subprocess.run(["bash", "-n", INSTALL_SH], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


def test_guest_house_flags_are_parsed():
    text = _read(INSTALL_SH)
    assert "--guest-house) GUEST_HOUSE=true" in text
    assert "--wsl) WSL_MODE=true" in text
    assert "--yes) YES=true" in text


def test_guest_house_wizard_functions_exist():
    text = _read(INSTALL_SH)
    for fn in ["run_guest_house_wizard", "gh_wizard_mac", "gh_wizard_wsl", "gh_preflight",
               "gh_confirm", "gh_have_tty", "gh_ensure_factory_checkout", "gh_self_path"]:
        assert f"{fn}() {{" in text, f"missing function definition: {fn}"


def test_dev_tty_handling_present():
    text = _read(INSTALL_SH)
    assert "/dev/tty" in text
    assert "gh_have_tty" in text
    # the abort path: no tty AND no --yes must exit non-zero, never hang
    assert '"$YES" != true' in text


def test_yes_flag_auto_confirms_without_touching_tty():
    text = _read(INSTALL_SH)
    block = text.split("gh_confirm() {", 1)[1].split("\ngh_", 1)[0]
    assert '"$YES" = true' in block
    # the auto-confirm branch returns before ever probing/reading /dev/tty
    yes_branch, _, rest = block.partition('"$YES" = true')
    assert "return 0" in rest.split("fi", 1)[0]


def test_no_tty_and_no_yes_aborts_instead_of_reading():
    """Real (safe) exercise of the abort path: no controlling tty, no --yes -> a loud,
    immediate non-zero exit, never a hang waiting on input."""
    r = subprocess.run(["bash", INSTALL_SH, "--guest-house"],
                        capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    assert r.returncode != 0
    assert "no terminal" in (r.stdout + r.stderr).lower()


def test_wsl_flag_on_darwin_is_refused_before_any_mutation():
    """Safe to actually run: --wsl on a non-Linux platform must refuse during preflight,
    before touching anything. (This suite's own host may or may not be Darwin; the guard is
    keyed on `uname -s`, so this only asserts the refusal fires on whatever platform is
    NOT Linux — skip on Linux, where --wsl is the supported branch instead.)"""
    if subprocess.run(["uname", "-s"], capture_output=True, text=True, timeout=5).stdout.strip() == "Linux":
        pytest.skip("this host is Linux — --guest-house --wsl is the supported branch here")
    r = subprocess.run(["bash", INSTALL_SH, "--guest-house", "--wsl", "--yes"],
                        capture_output=True, text=True, timeout=15)
    assert r.returncode != 0
    assert "only runs inside a Linux (WSL) distro" in (r.stdout + r.stderr)


def test_plain_mode_flags_and_phases_are_untouched():
    """Byte-compatibility guard: every original flag/phase marker from before --guest-house
    was added is still present verbatim. tests/test_install_sh.py exercises the actual
    runtime behavior end-to-end; this is its static companion."""
    text = _read(INSTALL_SH)
    for flag in ["--target)", "--target-dir)", "--name)", "--root)", "--factory-repo)",
                 "--branch)", "--provider)", "--base-branch)", "--port)", "--skip-deps)"]:
        assert flag in text
    for phase in ["# --- 1. preflight ---", "# --- 10. summary ---"]:
        assert phase in text


def test_guest_house_dispatch_short_circuits_before_the_normal_flow():
    text = _read(INSTALL_SH)
    dispatch = text.index('if [ "$GUEST_HOUSE" = true ]; then')
    normal_flow_marker = text.index('# Strip a trailing slash')
    assert dispatch < normal_flow_marker


def test_guest_house_flags_do_not_shadow_unknown_argument_guard():
    """The unknown-argument guard (`*) echo ... exit 2`) must still fire for a typo'd flag —
    proves the new case arms were inserted, not appended after a catch-all."""
    r = subprocess.run(["bash", INSTALL_SH, "--not-a-real-flag"],
                        capture_output=True, text=True, timeout=10)
    assert r.returncode == 2
    assert "unknown argument" in (r.stdout + r.stderr)


# =============================================================================================
# (b) scripts/guesthouse_check.py — the deterministic doctor
# =============================================================================================
def _load_gh_check():
    spec = importlib.util.spec_from_file_location("guesthouse_check", GUESTHOUSE_CHECK)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses on Python 3.14 resolves annotation types via sys.modules[cls.__module__] —
    # a module loaded via importlib without being registered there first raises AttributeError
    # deep inside the @dataclass decorator (Ctx uses one). Register before exec_module, same
    # fix the stdlib's own importlib docs recommend for this exact pattern.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gh = _load_gh_check()


def test_guesthouse_check_syntax_valid():
    r = subprocess.run(["python3", "-c", f"compile(open({GUESTHOUSE_CHECK!r}).read(), 'x', 'exec')"],
                        capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


# --- rule 1: standard-user -------------------------------------------------------------------
def test_standard_user_pass_darwin_non_admin():
    ctx = gh.Ctx(platform_name="Darwin", euid=501,
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "no bob is not a member\n", ""))
    r = gh.rule_standard_user(ctx)
    assert r.status == gh.PASS


def test_standard_user_fail_darwin_admin():
    ctx = gh.Ctx(platform_name="Darwin", euid=501,
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "yes bob is a member\n", ""))
    r = gh.rule_standard_user(ctx)
    assert r.status == gh.FAIL


def test_standard_user_fail_root():
    ctx = gh.Ctx(platform_name="Darwin", euid=0)
    r = gh.rule_standard_user(ctx)
    assert r.status == gh.FAIL
    assert "root" in r.detail


def test_standard_user_linux_pass_and_fail():
    ctx_pass = gh.Ctx(platform_name="Linux", euid=1000,
                       run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "bob users\n", ""))
    assert gh.rule_standard_user(ctx_pass).status == gh.PASS
    ctx_fail = gh.Ctx(platform_name="Linux", euid=1000,
                       run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "bob users sudo\n", ""))
    assert gh.rule_standard_user(ctx_fail).status == gh.FAIL


def test_standard_user_skip_foreign_platform():
    ctx = gh.Ctx(platform_name="Windows")
    r = gh.rule_standard_user(ctx)
    assert r.status == gh.SKIP


# --- rule 2: no-sudo-grant ---------------------------------------------------------------------
def test_no_sudo_grant_pass_and_fail():
    ok = gh.Ctx(platform_name="Darwin", run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    assert gh.rule_no_sudo_grant(ok).status == gh.PASS
    bad = gh.Ctx(platform_name="Darwin", run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert gh.rule_no_sudo_grant(bad).status == gh.FAIL


def test_no_sudo_grant_skip_foreign_platform():
    ctx = gh.Ctx(platform_name="Windows")
    assert gh.rule_no_sudo_grant(ctx).status == gh.SKIP


# --- rule 3: home-dir-perms --------------------------------------------------------------------
def test_home_dir_perms_pass_700(tmp_path):
    home = tmp_path / "home700"
    home.mkdir(mode=0o700)
    os.chmod(home, 0o700)
    ctx = gh.Ctx(home=str(home))
    assert gh.rule_home_dir_perms(ctx).status == gh.PASS


def test_home_dir_perms_fail_group_readable(tmp_path):
    home = tmp_path / "home755"
    home.mkdir(mode=0o755)
    os.chmod(home, 0o755)
    ctx = gh.Ctx(home=str(home))
    assert gh.rule_home_dir_perms(ctx).status == gh.FAIL


def test_home_dir_perms_skip_non_posix():
    ctx = gh.Ctx(is_posix=False)
    assert gh.rule_home_dir_perms(ctx).status == gh.SKIP


# --- rule 4: no-ssh-access -----------------------------------------------------------------------
def test_no_ssh_access_pass_no_dir(tmp_path):
    ctx = gh.Ctx(ssh_dir=str(tmp_path / "nope"), environ={})
    assert gh.rule_no_ssh_access(ctx).status == gh.PASS


def test_no_ssh_access_pass_only_benign_files(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA...\n")
    (ssh / "known_hosts").write_text("github.com ...\n")
    ctx = gh.Ctx(ssh_dir=str(ssh), environ={})
    assert gh.rule_no_ssh_access(ctx).status == gh.PASS


def test_no_ssh_access_fail_private_key_present(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    ctx = gh.Ctx(ssh_dir=str(ssh), environ={})
    r = gh.rule_no_ssh_access(ctx)
    assert r.status == gh.FAIL
    assert "id_ed25519" in r.detail


def test_no_ssh_access_fail_agent_socket(tmp_path):
    ctx = gh.Ctx(ssh_dir=str(tmp_path / "nope"), environ={"SSH_AUTH_SOCK": "/tmp/agent.sock"})
    assert gh.rule_no_ssh_access(ctx).status == gh.FAIL


# --- rule 5: no-docker-socket ----------------------------------------------------------------
def test_no_docker_socket_pass_absent(tmp_path):
    ctx = gh.Ctx(docker_socket=str(tmp_path / "docker.sock"))
    assert gh.rule_no_docker_socket(ctx).status == gh.PASS


def test_no_docker_socket_pass_exists_not_accessible(tmp_path):
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    ctx = gh.Ctx(docker_socket=str(sock), docker_socket_access_fn=lambda p: False)
    assert gh.rule_no_docker_socket(ctx).status == gh.PASS


def test_no_docker_socket_fail_exists_and_accessible(tmp_path):
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    ctx = gh.Ctx(docker_socket=str(sock), docker_socket_access_fn=lambda p: True)
    assert gh.rule_no_docker_socket(ctx).status == gh.FAIL


# --- rule 6: runtime-read-only -----------------------------------------------------------------
def test_runtime_readonly_skip_bare_repo_absent(tmp_path):
    ctx = gh.Ctx(bare_repo_path=str(tmp_path / "nope.git"))
    assert gh.rule_runtime_readonly(ctx).status == gh.SKIP


def test_runtime_readonly_pass_owned_by_other(tmp_path):
    bare = tmp_path / "factory.git"
    bare.mkdir()
    ctx = gh.Ctx(bare_repo_path=str(bare), stat_uid_fn=lambda p: 501, current_uid_fn=lambda: 600)
    assert gh.rule_runtime_readonly(ctx).status == gh.PASS


def test_runtime_readonly_fail_owned_by_self(tmp_path):
    bare = tmp_path / "factory.git"
    bare.mkdir()
    ctx = gh.Ctx(bare_repo_path=str(bare), stat_uid_fn=lambda p: 501, current_uid_fn=lambda: 501)
    assert gh.rule_runtime_readonly(ctx).status == gh.FAIL


# --- rule 7: credentials-hygiene ----------------------------------------------------------------
def test_credentials_hygiene_skip_absent(tmp_path):
    ctx = gh.Ctx(env_file_path=str(tmp_path / "env"))
    assert gh.rule_credentials_hygiene(ctx).status == gh.SKIP


def test_credentials_hygiene_pass(tmp_path):
    envf = tmp_path / "env"
    envf.write_text("export GH_TOKEN=x\n")
    os.chmod(envf, 0o600)
    my_uid = os.stat(envf).st_uid
    ctx = gh.Ctx(env_file_path=str(envf), current_uid_fn=lambda: my_uid)
    assert gh.rule_credentials_hygiene(ctx).status == gh.PASS


def test_credentials_hygiene_fail_wrong_mode(tmp_path):
    envf = tmp_path / "env"
    envf.write_text("export GH_TOKEN=x\n")
    os.chmod(envf, 0o644)
    my_uid = os.stat(envf).st_uid
    ctx = gh.Ctx(env_file_path=str(envf), current_uid_fn=lambda: my_uid)
    assert gh.rule_credentials_hygiene(ctx).status == gh.FAIL


def test_credentials_hygiene_fail_wrong_owner(tmp_path):
    envf = tmp_path / "env"
    envf.write_text("export GH_TOKEN=x\n")
    os.chmod(envf, 0o600)
    ctx = gh.Ctx(env_file_path=str(envf), current_uid_fn=lambda: 999999)
    assert gh.rule_credentials_hygiene(ctx).status == gh.FAIL


# --- rule 8: brakes-engaged --------------------------------------------------------------------
def test_brakes_engaged_skip_no_factory_dir(tmp_path):
    ctx = gh.Ctx(factory_root=str(tmp_path / "nope"))
    assert gh.rule_brakes_engaged(ctx).status == gh.SKIP


def test_brakes_engaged_pass(tmp_path):
    froot = tmp_path / "factory"
    froot.mkdir()
    (froot / "STOP").write_text("halted\n")
    (froot / ".factory-mode").write_text("shift\n")
    ctx = gh.Ctx(factory_root=str(froot))
    assert gh.rule_brakes_engaged(ctx).status == gh.PASS


def test_brakes_engaged_fail_stop_absent(tmp_path):
    froot = tmp_path / "factory"
    froot.mkdir()
    (froot / ".factory-mode").write_text("shift\n")
    ctx = gh.Ctx(factory_root=str(froot))
    assert gh.rule_brakes_engaged(ctx).status == gh.FAIL


def test_brakes_engaged_fail_mode_auto(tmp_path):
    froot = tmp_path / "factory"
    froot.mkdir()
    (froot / "STOP").write_text("halted\n")
    (froot / ".factory-mode").write_text("auto\n")
    ctx = gh.Ctx(factory_root=str(froot))
    assert gh.rule_brakes_engaged(ctx).status == gh.FAIL


# --- rule 9: dashboard-localhost ----------------------------------------------------------------
def test_dashboard_localhost_skip_no_config(tmp_path):
    froot = tmp_path / "factory"
    froot.mkdir()
    ctx = gh.Ctx(factory_root=str(froot))
    assert gh.rule_dashboard_localhost(ctx).status == gh.SKIP


def test_dashboard_localhost_pass(tmp_path):
    froot = tmp_path / "factory"
    froot.mkdir()
    (froot / "config.yaml").write_text("dashboard:\n  host: \"127.0.0.1\"\n  port: 8787\n")
    ctx = gh.Ctx(factory_root=str(froot))
    assert gh.rule_dashboard_localhost(ctx).status == gh.PASS


def test_dashboard_localhost_fail(tmp_path):
    froot = tmp_path / "factory"
    froot.mkdir()
    (froot / "config.yaml").write_text("dashboard:\n  host: \"0.0.0.0\"\n  port: 8787\n")
    ctx = gh.Ctx(factory_root=str(froot))
    assert gh.rule_dashboard_localhost(ctx).status == gh.FAIL


# --- rule 10: wsl-hardening ----------------------------------------------------------------------
def test_wsl_hardening_skip_not_wsl(tmp_path):
    ctx = gh.Ctx(environ={}, wsl_conf_path=str(tmp_path / "wsl.conf"))
    assert gh.rule_wsl_hardening(ctx).status == gh.SKIP


def test_wsl_hardening_fail_conf_missing(tmp_path):
    ctx = gh.Ctx(environ={"WSL_DISTRO_NAME": "factory-guesthouse"},
                 wsl_conf_path=str(tmp_path / "wsl.conf"))
    assert gh.rule_wsl_hardening(ctx).status == gh.FAIL


def test_wsl_hardening_fail_incomplete(tmp_path):
    conf = tmp_path / "wsl.conf"
    conf.write_text("[automount]\nenabled=false\n")
    ctx = gh.Ctx(environ={"WSL_DISTRO_NAME": "factory-guesthouse"}, wsl_conf_path=str(conf))
    assert gh.rule_wsl_hardening(ctx).status == gh.FAIL


def test_wsl_hardening_pass(tmp_path):
    conf = tmp_path / "wsl.conf"
    conf.write_text("[automount]\nenabled=false\n\n[interop]\nenabled=false\nappendWindowsPath=false\n")
    ctx = gh.Ctx(environ={"WSL_DISTRO_NAME": "factory-guesthouse"}, wsl_conf_path=str(conf))
    assert gh.rule_wsl_hardening(ctx).status == gh.PASS


# --- table renderer + main() ----------------------------------------------------------------------
def test_render_table_includes_header_and_summary():
    rules = [gh.Rule("short", gh.PASS, "fine"), gh.Rule("longer-id", gh.FAIL, "broken")]
    table = gh.render_table(rules)
    assert "RULE" in table and "STATUS" in table and "DETAIL" in table
    assert "short" in table and "longer-id" in table and "broken" in table
    assert "1 pass, 1 fail, 0 skip" in table


def test_main_exit_zero_when_all_pass_or_skip(monkeypatch, capsys):
    monkeypatch.setattr(gh, "run_all", lambda ctx=None: [
        gh.Rule("a", gh.PASS, "ok"), gh.Rule("b", gh.SKIP, "n/a")])
    rc = gh.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 pass, 0 fail, 1 skip" in out


def test_main_exit_one_when_any_fail(monkeypatch, capsys):
    monkeypatch.setattr(gh, "run_all", lambda ctx=None: [
        gh.Rule("a", gh.PASS, "ok"), gh.Rule("b", gh.FAIL, "bad")])
    rc = gh.main([])
    assert rc == 1


def test_main_json_shape(monkeypatch, capsys):
    monkeypatch.setattr(gh, "run_all", lambda ctx=None: [gh.Rule("a", gh.PASS, "ok")])
    rc = gh.main(["--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data == [{"id": "a", "status": "PASS", "detail": "ok"}]
    assert rc == 0


def test_real_run_via_subprocess_never_mutates_and_exits_0_or_1(tmp_path):
    """An end-to-end sanity pass against the REAL system (read-only by contract) — never
    asserts specific PASS/FAIL content (that varies by host), only that it runs, prints the
    table, and exits within the documented {0, 1} range."""
    r = subprocess.run(["python3", GUESTHOUSE_CHECK], capture_output=True, text=True, timeout=30)
    assert r.returncode in (0, 1)
    assert "RULE" in r.stdout
    assert "pass," in r.stdout and "fail," in r.stdout and "skip" in r.stdout


# =============================================================================================
# (c) install.ps1 — static sanity (no PowerShell available on this host to execute it)
# =============================================================================================
def test_install_ps1_exists():
    assert os.path.isfile(INSTALL_PS1)


def test_install_ps1_no_nul_or_crlf_surprises():
    data = open(INSTALL_PS1, "rb").read()
    assert b"\x00" not in data, "NUL byte found in install.ps1"
    assert b"\r" not in data, "CRLF/CR found in install.ps1 — expected LF-only"


def test_install_ps1_braces_are_balanced():
    data = open(INSTALL_PS1, "rb").read()
    assert data.count(b"{") == data.count(b"}")


def test_install_ps1_has_experimental_banner():
    text = _read(INSTALL_PS1)
    assert "EXPERIMENTAL" in text


def test_install_ps1_has_set_strict_mode():
    text = _read(INSTALL_PS1)
    assert "Set-StrictMode" in text


def test_install_ps1_has_wsl_conf_hardening_keys():
    text = _read(INSTALL_PS1)
    for key in ["[automount]", "[interop]", "[boot]", "enabled=false",
                "appendWindowsPath=false", "systemd=true"]:
        assert key in text


def test_install_ps1_supports_yes_switch():
    text = _read(INSTALL_PS1)
    assert "[switch]$Yes" in text


def test_install_ps1_never_calls_bare_exit_outside_the_guard():
    """`exit` inside code executed via `irm | iex` would close the caller's whole console —
    every `exit` call must be gated on MyCommand.Path (real-file-only)."""
    text = _read(INSTALL_PS1)
    lines = text.splitlines()
    exit_lines = [i for i, ln in enumerate(lines) if ln.strip().startswith("exit ")]
    assert exit_lines, "expected at least one exit call"
    for i in exit_lines:
        # the exit call must be preceded (within a few lines) by the MyCommand.Path guard
        window = "\n".join(lines[max(0, i - 3):i])
        assert "MyInvocation.MyCommand.Path" in window


# =============================================================================================
# (d) README.md — both one-liners are present
# =============================================================================================
def test_readme_has_the_mac_guest_house_one_liner():
    text = _read(README)
    assert "install.sh" in text and "--guest-house" in text


def test_readme_has_the_windows_one_liner_and_experimental_tag():
    text = _read(README)
    assert "install.ps1" in text and "iex" in text
    assert "EXPERIMENTAL" in text


def test_readme_links_the_guest_house_runbook():
    text = _read(README)
    assert "docs/runbooks/guest-house.md" in text


def test_guest_house_runbook_exists_and_cross_links_roadmap_and_deployment_runbook():
    text = _read(RUNBOOK)
    assert "2026-08-06-production-hardening-roadmap.md" in text
    assert "factory-user-deployment.md" in text
    assert "guesthouse_check.py" in text
