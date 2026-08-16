"""Tests for Phase 0 of the production-hardening roadmap
(docs/plans/2026-08-06-production-hardening-roadmap.md): the guest-house guided install.

Covers the original deliverable E groups PLUS the adversarial-review fix round (2026-08-06,
same day) that found: secrets eating a piped stdin (A1), a dishonest --yes contract (A2), a
dead-end user-creation gate + no abort/interrupt guidance (A3), an unverified admin-account
adoption path (A4), a false "STOP is set" claim (A5), --guest-house hijacking `list` (M3), the
WSL doctor path pointing at a directory that never exists (A7), the WSL re-exec silently
dropping --target/--name/--root/--port (M4); on the doctor: standard-user failing OPEN on an
unresolvable check (B1), a dishonestly-named sudo-grant rule (B2), a home-dir-perms rule the
installers themselves violated (B3), SSH false positives from launchd's always-set
SSH_AUTH_SOCK and a deny-list key scan (B4), 5-7 false FAILs auditing a non-guest-house
account (B5, the context gate), env-var-fragile WSL detection (B6), and an ownership-only
(not writability-proven) runtime-read-only rule (B7); on install.ps1: an inert WSL presence
check (C1), no dependency bootstrap in a fresh distro (C2), destructive reuse of an unmarked
distro (C3), return-stream pollution silently defeating every `if (-not (Step))` guard (C4),
installer failure exiting 0 (C5), no post-install registration verification (C6), no
hardening read-back verification (C7), and StrictMode traps around $MyInvocation/wsl --list/
the registry read (C8).

Six groups:
  (a) install.sh --guest-house wiring — syntax + grep-level contract + byte-compatibility of
      the pre-existing plain-mode flow, PLUS the fix-round items above, exercised with real
      (but safe: no sudo, no mutation, tty genuinely detached) subprocess runs where possible.
  (b) scripts/guesthouse_check.py — every probe's PASS/FAIL/SKIP path via injected Ctx fields
      (no test invokes sudo or touches the real host — D2), the context gate (B5), the table
      renderer, audit()/main()'s exit codes, and --json shape.
  (c) install.ps1 — static sanity only (no PowerShell available on this host to execute it),
      including the fix-round's new C1-C8 signatures.
  (d) README.md + the runbook — one-liners, cross-links, and the fixed system's documented
      contract (--yes semantics, STOP reality, doctor contexts, the marker/reuse rule).
"""
import getpass
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
               "gh_confirm", "gh_have_tty", "gh_ensure_factory_checkout", "gh_self_path",
               "gh_state_summary", "gh_on_exit", "gh_on_interrupt", "gh_verify_factory_non_admin"]:
        assert f"{fn}() {{" in text, f"missing function definition: {fn}"


def test_dev_tty_handling_present():
    text = _read(INSTALL_SH)
    assert "/dev/tty" in text
    assert "gh_have_tty" in text


def test_yes_flag_auto_confirms_without_touching_tty():
    text = _read(INSTALL_SH)
    block = text.split("gh_confirm() {", 1)[1].split("\ngh_", 1)[0]
    assert '"$YES" = true' in block
    # the auto-confirm branch returns before ever probing/reading /dev/tty
    _, _, rest = block.partition('"$YES" = true')
    assert "return 0" in rest.split("fi", 1)[0]


# --- A2: --yes never substitutes for a real terminal on the macOS (secrets) path -------------
def test_yes_contract_is_documented_honestly():
    """--yes must be documented as auto-answering ONLY the wizard's own prompts, never the
    three unavoidable interactive secrets (sudo password, new account password, GitHub
    token) — the old contract implied full unattendedness was possible."""
    text = _read(INSTALL_SH)
    assert "ONLY auto-answers" in text or "ONLY auto-answer" in text
    assert "three genuinely interactive secrets" in text or "three unavoidable interactive secrets" in text


def test_mac_mode_always_requires_a_terminal_regardless_of_yes():
    """A real (safe) exercise: --guest-house --yes with NO tty available must still abort —
    --yes cannot skip the tty requirement on the macOS (non-WSL) path."""
    if subprocess.run(["uname", "-s"], capture_output=True, text=True, timeout=5).stdout.strip() != "Darwin":
        pytest.skip("mac-mode preflight only runs its full body on Darwin")
    r = subprocess.run(["bash", INSTALL_SH, "--guest-house", "--yes"],
                        capture_output=True, text=True, timeout=15,
                        stdin=subprocess.DEVNULL, start_new_session=True)
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "always needs a real terminal" in out or "no terminal" in out


def test_no_tty_and_no_yes_aborts_instead_of_reading_and_mutates_nothing(tmp_path):
    """D1 fix: must hold even on a real interactive dev machine with a genuine controlling
    terminal, not just this sandbox's own lack of one. `start_new_session=True` detaches the
    child from any controlling tty (matching what a real `curl | bash` invocation
    experiences), so this is a REAL exercise of the abort path. A redirected, empty HOME
    proves nothing was cloned/mutated even if the guard were to regress — the old version of
    this test could (on a machine with a real tty) proceed past the guard, hang until its
    timeout, and leave a partial clone under ~/factories."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    r = subprocess.run(["bash", INSTALL_SH, "--guest-house"], env=env,
                        capture_output=True, text=True, timeout=20,
                        stdin=subprocess.DEVNULL, start_new_session=True)
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "no terminal" in out or "real terminal" in out
    assert not (home / "factories").exists()


def test_wsl_flag_on_darwin_is_refused_before_any_mutation():
    """Safe to actually run: --wsl on a non-Linux platform must refuse during preflight,
    before touching anything. (This suite's own host may or may not be Darwin; the guard is
    keyed on `uname -s`, so this only asserts the refusal fires on whatever platform is
    NOT Linux — skip on Linux, where --wsl is the supported branch instead.)"""
    if subprocess.run(["uname", "-s"], capture_output=True, text=True, timeout=5).stdout.strip() == "Linux":
        pytest.skip("this host is Linux — --guest-house --wsl is the supported branch here")
    home_env = {**os.environ}
    r = subprocess.run(["bash", INSTALL_SH, "--guest-house", "--wsl", "--yes"],
                        capture_output=True, text=True, timeout=15,
                        stdin=subprocess.DEVNULL, start_new_session=True, env=home_env)
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
    dispatch = text.index('if [ "$GUEST_HOUSE" = true ]')
    normal_flow_marker = text.index('# Strip a trailing slash')
    assert dispatch < normal_flow_marker


def test_guest_house_flags_do_not_shadow_unknown_argument_guard():
    """The unknown-argument guard (`*) echo ... exit 2`) must still fire for a typo'd flag —
    proves the new case arms were inserted, not appended after a catch-all."""
    r = subprocess.run(["bash", INSTALL_SH, "--not-a-real-flag"],
                        capture_output=True, text=True, timeout=10)
    assert r.returncode == 2
    assert "unknown argument" in (r.stdout + r.stderr)


# --- M3: `list --guest-house` must run `list`, never hijack it into the wizard ---------------
def test_dispatch_only_fires_for_the_install_command():
    text = _read(INSTALL_SH)
    assert 'if [ "$GUEST_HOUSE" = true ] && [ "$CMD" = "install" ]; then' in text


def test_list_guest_house_runs_list_not_the_wizard(tmp_path):
    root = tmp_path / "nonexistent-factories"
    r = subprocess.run(["bash", INSTALL_SH, "list", "--guest-house", "--root", str(root)],
                        capture_output=True, text=True, timeout=15,
                        stdin=subprocess.DEVNULL, start_new_session=True)
    assert r.returncode == 0, r.stderr
    assert "no instances under" in r.stdout.lower()
    assert "guest-house preflight" not in r.stdout  # the wizard never ran


# --- A1: the secrets-eat-the-pipe fix — kit invocations relay the real tty -------------------
def test_kit_invocations_relay_the_real_tty():
    text = _read(INSTALL_SH)
    assert 'sudo bash "$GH_FACTORY_DIR/deploy/user-factory/01-create-user.sh" < /dev/tty' in text
    assert 'sudo -u factory -i bash "$KIT/02-bootstrap-as-factory.sh" < /dev/tty' in text


# --- A3: 01 always runs (no dead-end `id factory` gate) + abort/interrupt trap ---------------
def test_create_user_step_is_not_gated_on_id_factory():
    text = _read(INSTALL_SH)
    mac_fn = text.split("gh_wizard_mac() {", 1)[1].split("\ngh_self_path", 1)[0]
    step1 = mac_fn.split("[1/6]", 1)[1].split("[2/6]", 1)[0]
    # the OLD bug: `if id factory >/dev/null 2>&1; then ... skip ... else ... create ... fi`
    # gated creation on absence — 01-create-user.sh must now always be OFFERED/run.
    assert "gh_confirm \"Run 01-create-user.sh now" in step1


def test_exit_and_interrupt_traps_are_installed():
    text = _read(INSTALL_SH)
    assert "trap gh_on_exit EXIT" in text
    assert "trap gh_on_interrupt INT TERM" in text


def test_state_summary_gives_resume_guidance():
    text = _read(INSTALL_SH)
    fn = text.split("gh_state_summary() {", 1)[1].split("\ngh_on_exit", 1)[0]
    assert "resume" in fn.lower()


# --- A4: adopted-account non-admin verification, fails closed --------------------------------
def test_verify_factory_non_admin_fails_closed_on_ambiguous_result():
    text = _read(INSTALL_SH)
    fn = text.split("gh_verify_factory_non_admin() {", 1)[1].split("\n}\n", 1)[0]
    assert "dseditgroup" in fn
    assert "failing closed" in fn


# --- B3-install-side: the wizard tightens the account home dir it creates --------------------
def test_wizard_chmods_factory_home_to_700():
    text = _read(INSTALL_SH)
    assert "chmod 700 /Users/factory" in text  # mac
    assert 'chmod 700 "$GH_FACTORY_HOME"' in text  # wsl


# --- A5: STOP is actually created by the wizard, and the summary claim is honest -------------
def test_wizard_drops_stop_after_a_successful_bootstrap():
    text = _read(INSTALL_SH)
    assert 'touch "$HOME/fab/factory/STOP"' in text  # mac, after 02 succeeds
    assert 'touch \'$GH_WSL_INSTALL_DIR/STOP\'' in text  # wsl, after install succeeds


def test_summary_does_not_falsely_claim_bootstrap_creates_stop():
    """The old summary text asserted unconditionally that 02-bootstrap-as-factory.sh drops
    STOP — it never did. The fixed summary must be conditioned on this run's own outcome."""
    text = _read(INSTALL_SH)
    assert "GH_BOOTSTRAP_OK" in text
    assert "GH_WSL_INSTALL_OK" in text


# --- A6/M4: the WSL re-exec forwards target/name/root/port instead of silently dropping them --
def test_wsl_reexec_forwards_target_and_layout_flags():
    text = _read(INSTALL_SH)
    fn = text.split("gh_wizard_wsl() {", 1)[1]
    assert 'GH_REEXEC_ARGS=(--factory-repo "$FACTORY_REPO" --branch "$BRANCH" --target "$TARGET"' in fn
    assert '--name "$GH_WSL_NAME" --root "$GH_WSL_ROOT" --port "$PORT")' in fn
    assert 'GH_REEXEC_ARGS+=(--target-dir "$TARGET_DIR_NAME")' in fn
    assert 'GH_REEXEC_ARGS+=(--provider "$PROVIDER")' in fn
    assert 'GH_REEXEC_ARGS+=(--base-branch "$BASE_BRANCH")' in fn


def test_wsl_install_root_is_unified():
    text = _read(INSTALL_SH)
    assert 'GH_WSL_NAME="guest-house"' in text
    assert 'GH_WSL_INSTALL_DIR="$GH_WSL_ROOT/$GH_WSL_NAME/factory"' in text


# --- A7: the WSL doctor path points at the REAL installed clone, not dirname($SELF_PATH) ------
def test_wsl_doctor_uses_the_unified_install_dir_not_self_path_dirname():
    text = _read(INSTALL_SH)
    assert 'GH_WSL_DOCTOR="$GH_WSL_INSTALL_DIR/scripts/guesthouse_check.py"' in text
    # the old, broken form:
    assert 'dirname "$SELF_PATH")/scripts/guesthouse_check.py' not in text


def test_wsl_doctor_distinguishes_could_not_run_from_reported_fail():
    text = _read(INSTALL_SH)
    fn = text.split("gh_wizard_wsl() {", 1)[1]
    assert "doctor could not run" in fn
    assert "doctor reported at least one FAIL" in fn


# --- M5: full GitHub URLs for docs (a curl|bash user has no local checkout) -------------------
def test_doc_references_are_full_urls():
    text = _read(INSTALL_SH)
    assert 'GH_DOCS_BASE_URL="https://github.com/ikangai/factory/blob/main"' in text
    assert "$GH_DOCS_BASE_URL/docs/runbooks/guest-house.md" in text
    assert "$GH_DOCS_BASE_URL/docs/runbooks/factory-user-deployment.md" in text


# --- M7: recommend /tmp for a root-downloaded script inside the distro ------------------------
def test_self_path_error_recommends_tmp():
    text = _read(INSTALL_SH)
    fn = text.split("gh_self_path() {", 1)[1].split("\n}\n", 1)[0]
    assert "-o /tmp/install.sh" in fn
    assert "/tmp/install.sh" in fn


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


# --- rule 1: standard-user — B1: fails CLOSED on an unresolvable check ------------------------
def test_standard_user_pass_darwin_non_admin():
    ctx = gh.Ctx(platform_name="Darwin", euid=501,
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "no bob is not a member\n", ""))
    assert gh.rule_standard_user(ctx).status == gh.PASS


def test_standard_user_pass_darwin_rc67_not_a_member():
    """The real dseditgroup semantics observed in review: a non-member reply can carry a
    non-zero rc (e.g. 67) alongside a 'no ...' line — PASS must key off the 'no' text, not
    require rc==0."""
    ctx = gh.Ctx(platform_name="Darwin", euid=501,
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 67, "no bob is not a member of the group admin\n", ""))
    assert gh.rule_standard_user(ctx).status == gh.PASS


def test_standard_user_fail_darwin_admin():
    ctx = gh.Ctx(platform_name="Darwin", euid=501,
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "yes bob is a member\n", ""))
    assert gh.rule_standard_user(ctx).status == gh.FAIL


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
    assert gh.rule_standard_user(ctx).status == gh.SKIP


def test_standard_user_fails_closed_on_unresolvable_darwin_check():
    """B1 (the demonstrated bug): an unresolvable user used to fall through to PASS. Neither
    'yes' nor 'no' in the output, on any return code, must now FAIL — 'could not determine'
    is never treated as a clean bill of health."""
    ctx = gh.Ctx(platform_name="Darwin", euid=501,
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 67, "", "dseditgroup: no such user"))
    r = gh.rule_standard_user(ctx)
    assert r.status == gh.FAIL
    assert "could not determine" in r.detail.lower()


def test_standard_user_fails_closed_on_linux_id_error():
    ctx = gh.Ctx(platform_name="Linux", euid=1000,
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "id: no such user"))
    r = gh.rule_standard_user(ctx)
    assert r.status == gh.FAIL
    assert "could not determine" in r.detail.lower()


def test_standard_user_fails_closed_on_exception():
    def _raise(*a, **k):
        raise OSError("dseditgroup not found")
    ctx = gh.Ctx(platform_name="Darwin", euid=501, run=_raise)
    assert gh.rule_standard_user(ctx).status == gh.FAIL


# --- rule 2: no-passwordless-sudo (renamed from no-sudo-grant, B2) ----------------------------
def test_no_sudo_grant_renamed_and_honest():
    ctx = gh.Ctx(platform_name="Darwin", run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    r = gh.rule_no_sudo_grant(ctx)
    assert r.id == "no-passwordless-sudo"
    assert r.status == gh.PASS


def test_no_sudo_grant_pass_and_fail():
    ok = gh.Ctx(platform_name="Darwin", run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    assert gh.rule_no_sudo_grant(ok).status == gh.PASS
    bad = gh.Ctx(platform_name="Darwin", run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert gh.rule_no_sudo_grant(bad).status == gh.FAIL


def test_no_sudo_grant_skip_foreign_platform():
    ctx = gh.Ctx(platform_name="Windows")
    assert gh.rule_no_sudo_grant(ctx).status == gh.SKIP


def test_no_sudo_grant_id_is_in_the_rules_table():
    ids = [rid for rid, _ in gh.RULES]
    assert "no-passwordless-sudo" in ids
    assert "no-sudo-grant" not in ids


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


# --- rule 4: no-ssh-access — B4: ssh-add-based agent check + positive key identification -----
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


def test_no_ssh_access_pass_arbitrary_non_key_file(tmp_path):
    """M6: positive identification, not a deny-list — a random file that is neither
    id_*-named nor PEM/OpenSSH-headed must PASS even though it isn't in any hardcoded
    'known benign filename' list (the old deny-list would have flagged anything unrecognized
    as a false positive)."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "notes.txt").write_text("just some random file, not a key\n")
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


def test_no_ssh_access_fail_pem_header_even_with_unusual_name(tmp_path):
    """Positive identification also catches a private key under an UNUSUAL name (not
    id_*-prefixed) via its PEM/OpenSSH header line — the old deny-list would have missed
    this entirely."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "my_custom_key_name").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    ctx = gh.Ctx(ssh_dir=str(ssh), environ={})
    r = gh.rule_no_ssh_access(ctx)
    assert r.status == gh.FAIL
    assert "my_custom_key_name" in r.detail


def test_no_ssh_access_pass_auth_sock_set_but_agent_empty(tmp_path):
    """B4 (the demonstrated bug): macOS launchd sets SSH_AUTH_SOCK in EVERY session
    regardless of whether any key is loaded — merely being set must no longer FAIL."""
    ctx = gh.Ctx(ssh_dir=str(tmp_path / "nope"), environ={"SSH_AUTH_SOCK": "/tmp/agent.sock"},
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "The agent has no identities."))
    assert gh.rule_no_ssh_access(ctx).status == gh.PASS


def test_no_ssh_access_pass_auth_sock_set_but_no_agent_reachable(tmp_path):
    ctx = gh.Ctx(ssh_dir=str(tmp_path / "nope"), environ={"SSH_AUTH_SOCK": "/tmp/agent.sock"},
                 run=lambda *a, **k: subprocess.CompletedProcess(
                     a, 2, "", "Could not open a connection to your authentication agent."))
    assert gh.rule_no_ssh_access(ctx).status == gh.PASS


def test_no_ssh_access_fail_loaded_identities():
    ctx = gh.Ctx(ssh_dir="/nonexistent", environ={"SSH_AUTH_SOCK": "/tmp/agent.sock"},
                 run=lambda *a, **k: subprocess.CompletedProcess(
                     a, 0, "2048 SHA256:abc user@host (RSA)\n", ""))
    r = gh.rule_no_ssh_access(ctx)
    assert r.status == gh.FAIL
    assert "loaded identities" in r.detail


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


# --- rule 6: runtime-read-only — B7: writability proven, not just ownership -------------------
def test_runtime_readonly_skip_bare_repo_absent(tmp_path):
    ctx = gh.Ctx(bare_repo_path=str(tmp_path / "nope.git"))
    assert gh.rule_runtime_readonly(ctx).status == gh.SKIP


def test_runtime_readonly_pass_owned_by_other_and_not_writable(tmp_path):
    bare = tmp_path / "factory.git"
    bare.mkdir()
    ctx = gh.Ctx(bare_repo_path=str(bare), stat_uid_fn=lambda p: 501, current_uid_fn=lambda: 600,
                 access_w_fn=lambda p: False)
    r = gh.rule_runtime_readonly(ctx)
    assert r.status == gh.PASS
    assert "W_OK=False" in r.detail


def test_runtime_readonly_fail_owned_by_self(tmp_path):
    bare = tmp_path / "factory.git"
    bare.mkdir()
    ctx = gh.Ctx(bare_repo_path=str(bare), stat_uid_fn=lambda p: 501, current_uid_fn=lambda: 501,
                 access_w_fn=lambda p: False)
    assert gh.rule_runtime_readonly(ctx).status == gh.FAIL


def test_runtime_readonly_fail_writable_even_when_not_owned(tmp_path):
    """B7: writability alone is sufficient to FAIL, even when ownership looks clean — proves
    the check no longer relies on ownership as its only signal."""
    bare = tmp_path / "factory.git"
    bare.mkdir()
    ctx = gh.Ctx(bare_repo_path=str(bare), stat_uid_fn=lambda p: 501, current_uid_fn=lambda: 600,
                 access_w_fn=lambda p: True)
    r = gh.rule_runtime_readonly(ctx)
    assert r.status == gh.FAIL
    assert "writable" in r.detail


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


# --- rule 10: wsl-hardening — B6: env-var-independent /proc/version detection -----------------
def _proc_version(tmp_path, is_wsl):
    pv = tmp_path / "proc_version"
    if is_wsl:
        pv.write_text("Linux version 5.15.0 (buildd@lcy02-amd64) ... Microsoft ...\n")
    else:
        pv.write_text("Linux version 5.15.0 (buildd@lcy02-amd64) #1 SMP\n")
    return str(pv)


def test_wsl_hardening_skip_not_wsl(tmp_path):
    ctx = gh.Ctx(environ={}, proc_version_path=_proc_version(tmp_path, is_wsl=False),
                 wsl_conf_path=str(tmp_path / "wsl.conf"))
    assert gh.rule_wsl_hardening(ctx).status == gh.SKIP


def test_wsl_hardening_detects_wsl_without_any_env_vars(tmp_path):
    """B6 (the demonstrated bug): detection must work via /proc/version even with
    WSL_DISTRO_NAME/WSL_INTEROP entirely absent — exactly what happens when the doctor runs
    via `sudo` (env stripped by default) or after interop hardening removes WSL_INTEROP."""
    ctx = gh.Ctx(environ={}, proc_version_path=_proc_version(tmp_path, is_wsl=True),
                 wsl_conf_path=str(tmp_path / "missing.conf"))
    r = gh.rule_wsl_hardening(ctx)
    assert r.status == gh.FAIL  # detected WSL correctly; conf is simply missing


def test_wsl_hardening_fail_conf_missing(tmp_path):
    ctx = gh.Ctx(environ={}, proc_version_path=_proc_version(tmp_path, is_wsl=True),
                 wsl_conf_path=str(tmp_path / "wsl.conf"))
    assert gh.rule_wsl_hardening(ctx).status == gh.FAIL


def test_wsl_hardening_fail_incomplete(tmp_path):
    conf = tmp_path / "wsl.conf"
    conf.write_text("[automount]\nenabled=false\n")
    ctx = gh.Ctx(environ={}, proc_version_path=_proc_version(tmp_path, is_wsl=True), wsl_conf_path=str(conf))
    assert gh.rule_wsl_hardening(ctx).status == gh.FAIL


def test_wsl_hardening_pass(tmp_path):
    conf = tmp_path / "wsl.conf"
    conf.write_text("[automount]\nenabled=false\n\n[interop]\nenabled=false\nappendWindowsPath=false\n")
    ctx = gh.Ctx(environ={}, proc_version_path=_proc_version(tmp_path, is_wsl=True), wsl_conf_path=str(conf))
    assert gh.rule_wsl_hardening(ctx).status == gh.PASS


# --- B5: the context gate — auditing a non-guest-house account ---------------------------------
def test_is_guest_house_context_true_for_factory_username(tmp_path):
    ctx = gh.Ctx(username="factory", home=str(tmp_path))
    assert gh.is_guest_house_context(ctx) is True


def test_is_guest_house_context_true_for_fab_factory_dir(tmp_path):
    (tmp_path / "fab" / "factory").mkdir(parents=True)
    ctx = gh.Ctx(username="someoperator", home=str(tmp_path))
    assert gh.is_guest_house_context(ctx) is True


def test_is_guest_house_context_true_for_unified_wsl_install_root(tmp_path):
    root = tmp_path / "factories" / "guest-house" / "factory"
    root.mkdir(parents=True)
    ctx = gh.Ctx(username="someoperator", home=str(tmp_path))
    assert gh.is_guest_house_context(ctx) is True


def test_is_guest_house_context_false_for_ordinary_account(tmp_path):
    ctx = gh.Ctx(username="someoperator", home=str(tmp_path))
    assert gh.is_guest_house_context(ctx) is False


def test_audit_skips_all_account_scoped_rules_in_non_deployed_context(tmp_path):
    """B5 (the demonstrated bug): an operator's own dev checkout used to produce 5+ false
    FAILs. Every account-scoped rule must now SKIP with an explicit, honest reason, and NONE
    of them may invoke a real subprocess (the injected `run` below raises if called)."""
    def _must_not_be_called(*a, **k):
        raise AssertionError(f"account-scoped rule invoked subprocess in non-deployed context: {a}")
    ctx = gh.Ctx(username="someoperator", home=str(tmp_path),
                 factory_root=str(tmp_path / "not-a-factory-checkout"),
                 bare_repo_path=str(tmp_path / "not-a-bare-repo.git"),
                 env_file_path=str(tmp_path / "not-an-env-file"),
                 ssh_dir=str(tmp_path / "not-ssh"),
                 docker_socket=str(tmp_path / "not-docker.sock"),
                 run=_must_not_be_called)
    result = gh.audit(ctx)
    assert result.deployed is False
    by_id = {r.id: r for r in result.rules}
    for rid in gh.ACCOUNT_SCOPED_RULE_IDS:
        assert by_id[rid].status == gh.SKIP, f"{rid} did not SKIP in non-deployed context"
        assert "non-guest-house" in by_id[rid].detail


def test_audit_runs_account_scoped_rules_for_real_in_deployed_context(tmp_path):
    ctx = gh.Ctx(username="factory", home=str(tmp_path),
                 factory_root=str(tmp_path / "no-factory-root"),
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "no not a member\n", ""))
    result = gh.audit(ctx)
    assert result.deployed is True
    by_id = {r.id: r for r in result.rules}
    assert by_id["standard-user"].status == gh.PASS
    assert "non-guest-house" not in by_id["standard-user"].detail


def test_run_all_backcompat_returns_just_the_rules(tmp_path):
    ctx = gh.Ctx(username="factory", home=str(tmp_path),
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "no not a member\n", ""))
    rules = gh.run_all(ctx)
    assert isinstance(rules, list)
    assert len(rules) == 12   # + shared-drop-hygiene (2026-08-09),
                              # + deployment-not-peer-readable (drill 2, 2026-08-16)


# --- table renderer + audit()/main() ----------------------------------------------------------
def test_render_table_includes_header_and_summary():
    rules = [gh.Rule("short", gh.PASS, "fine"), gh.Rule("longer-id", gh.FAIL, "broken")]
    table = gh.render_table(rules)
    assert "RULE" in table and "STATUS" in table and "DETAIL" in table
    assert "short" in table and "longer-id" in table and "broken" in table
    assert "1 pass, 1 fail, 0 skip" in table


def test_main_exit_zero_when_all_pass_or_skip_deployed(monkeypatch, capsys):
    monkeypatch.setattr(gh, "audit", lambda ctx=None: gh.AuditResult(
        rules=[gh.Rule("a", gh.PASS, "ok"), gh.Rule("b", gh.SKIP, "n/a")], deployed=True))
    rc = gh.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 pass, 0 fail, 1 skip" in out


def test_main_exit_one_when_any_fail_in_deployed_context(monkeypatch, capsys):
    monkeypatch.setattr(gh, "audit", lambda ctx=None: gh.AuditResult(
        rules=[gh.Rule("a", gh.PASS, "ok"), gh.Rule("b", gh.FAIL, "bad")], deployed=True))
    rc = gh.main([])
    assert rc == 1


def test_main_exit_zero_in_non_deployed_context_even_with_a_fail(monkeypatch, capsys):
    """B5: exit code is always 0 outside guest-house context, regardless of what a
    checkout-scoped rule (which still runs there) reports."""
    monkeypatch.setattr(gh, "audit", lambda ctx=None: gh.AuditResult(
        rules=[gh.Rule("brakes-engaged", gh.FAIL, "bad")], deployed=False))
    rc = gh.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "does not look like a deployed guest-house user" in out.lower()


def test_main_json_shape_includes_deployed_flag(monkeypatch, capsys):
    monkeypatch.setattr(gh, "audit", lambda ctx=None: gh.AuditResult(
        rules=[gh.Rule("a", gh.PASS, "ok")], deployed=True))
    rc = gh.main(["--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["deployed"] is True
    assert data["rules"] == [{"id": "a", "status": "PASS", "detail": "ok"}]
    assert rc == 0


def test_audit_end_to_end_never_touches_a_real_subprocess(tmp_path, capsys):
    """D2: no test may invoke sudo (or anything else) against the real host. This exercises
    the FULL audit() -> render_table() pipeline with every dependency explicitly injected —
    the only `run` callable reachable from any rule is this hermetic fake, so by
    construction no real subprocess ever executes, in either deployed or non-deployed mode."""
    fake_run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "no not a member\n", "")
    for username in ("factory", "someoperator"):
        ctx = gh.Ctx(username=username, home=str(tmp_path / username),
                     factory_root=str(tmp_path / username / "no-factory-root"),
                     bare_repo_path=str(tmp_path / username / "no-bare.git"),
                     env_file_path=str(tmp_path / username / "no-env"),
                     ssh_dir=str(tmp_path / username / "no-ssh"),
                     docker_socket=str(tmp_path / username / "no-docker.sock"),
                     run=fake_run)
        result = gh.audit(ctx)
        assert len(result.rules) == 12   # + shared-drop-hygiene,
                                         # + deployment-not-peer-readable
        table = gh.render_table(result.rules)
        assert "RULE" in table


def test_real_cli_invocation_in_a_forced_non_deployed_context_never_touches_sudo(tmp_path):
    """A real subprocess run of the shipped script — but with HOME/USER forced to values that
    guarantee the context gate skips every account-scoped rule (including the one that would
    otherwise call `sudo -n -v` for real), satisfying D2 while still proving the CLI's
    argparse/import/render wiring works end to end."""
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USER": "not-the-guest-house-user",
           "USERNAME": "not-the-guest-house-user", "LOGNAME": "not-the-guest-house-user"}
    r = subprocess.run(["python3", GUESTHOUSE_CHECK], capture_output=True, text=True,
                        timeout=30, env=env)
    assert r.returncode == 0  # always 0 outside guest-house context
    assert "does not look like a deployed guest-house user" in r.stdout.lower()
    assert "RULE" in r.stdout


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


def test_install_ps1_here_string_terminator_at_column_zero():
    """A here-string's closing `'@` must be the FIRST two characters of its own line with
    nothing else on it — any leading whitespace or trailing text silently breaks
    PowerShell's parser (a real, well-documented gotcha)."""
    text = _read(INSTALL_PS1)
    lines = text.splitlines()
    opens = [i for i, ln in enumerate(lines) if ln.rstrip().endswith("@'")]
    closes = [i for i, ln in enumerate(lines) if ln == "'@"]
    assert opens, "expected at least one here-string opener (@')"
    assert len(opens) == len(closes), "every here-string opener needs a matching column-0 closer"


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
    every `exit` call must be gated on $PSCommandPath (C8: NOT $MyInvocation.MyCommand.Path
    as an actual GUARD — that expression can be $null under `iex` and, combined with
    Set-StrictMode -Version Latest, THROW on property access rather than safely evaluating
    falsy). The old pattern's NAME may still appear in an explanatory comment; only actual
    `if (...)` guard code is checked here."""
    text = _read(INSTALL_PS1)
    lines = text.splitlines()
    guard_lines = [ln for ln in lines if ln.strip().startswith("if (") and "MyCommand.Path" in ln]
    assert not any("MyInvocation.MyCommand.Path" in ln for ln in guard_lines), \
        "an `if (...)` guard still uses the null-throwing $MyInvocation.MyCommand.Path form"
    exit_lines = [i for i, ln in enumerate(lines) if ln.strip().startswith("exit ")]
    assert exit_lines, "expected at least one exit call"
    for i in exit_lines:
        window = "\n".join(lines[max(0, i - 3):i])
        assert "PSCommandPath" in window


def test_install_ps1_probes_wsl_status_and_version_for_real():
    """C1: Get-Command alone is inert (wsl.exe exists on disk even with WSL disabled) — a
    real `wsl --status` (and `--version`, to detect legacy inbox WSL) probe is required."""
    text = _read(INSTALL_PS1)
    assert "'--status'" in text
    assert "'--version'" in text


def test_install_ps1_bootstraps_apt_dependencies():
    text = _read(INSTALL_PS1)
    assert "apt-get install" in text
    assert "python3-pip" in text and "python3-venv" in text and "curl" in text


def test_install_ps1_has_distro_ownership_marker():
    """C3: reuse of an existing, unmarked distro must be refused."""
    text = _read(INSTALL_PS1)
    assert "factory-guesthouse.marker" in text
    assert "NOT created by" in text


def test_install_ps1_verifies_post_install_registration():
    """C6: an older wsl.exe can silently ignore --name while exiting 0."""
    text = _read(INSTALL_PS1)
    assert "-notcontains $DistroName" in text


def test_install_ps1_reads_back_hardening_and_checks_automount():
    """C7: writing /etc/wsl.conf must be verified, not just assumed to have taken effect."""
    text = _read(INSTALL_PS1)
    assert "cat /etc/wsl.conf" in text
    assert "/mnt/c" in text
    assert "BLOCKED" in text


def test_install_ps1_distinguishes_declined_failed_succeeded():
    """C5: the installer step must not collapse 'operator said no' and 'it errored' into the
    same exit code."""
    text = _read(INSTALL_PS1)
    assert "'declined'" in text
    assert "'failed'" in text
    assert "'succeeded'" in text


def test_install_ps1_sets_console_output_encoding():
    text = _read(INSTALL_PS1)
    assert "OutputEncoding" in text
    assert "Encoding]::Unicode" in text


def test_install_ps1_wraps_registered_distros_probe_in_try_catch():
    text = _read(INSTALL_PS1)
    fn = text.split("function Get-RegisteredDistros {", 1)[1].split("\nfunction ", 1)[0]
    assert "try {" in fn and "catch {" in fn


def test_install_ps1_wraps_registry_read_in_try_catch():
    text = _read(INSTALL_PS1)
    fn = text.split("function Test-Preflight {", 1)[1].split("\nfunction ", 1)[0]
    assert "CurrentBuild" in fn
    assert "try {" in fn and "catch {" in fn


def test_install_ps1_documents_the_parameterized_invocation_form():
    """A bare `irm | iex` cannot forward -Yes/-DistroName — the closing summary must spell
    out the scriptblock form that can."""
    text = _read(INSTALL_PS1)
    assert "[scriptblock]::Create" in text


def test_install_ps1_lists_unverifiable_items_in_the_summary():
    text = _read(INSTALL_PS1)
    fn = text.split("function Write-ClosingSummary {", 1)[1].split("\nfunction ", 1)[0]
    assert "UNVERIFIABLE" in fn
    assert fn.count("\n   ") >= 10 or fn.count("\n  1") >= 1  # the numbered list is present


def test_install_ps1_functions_never_leave_a_bare_unassigned_wsl_call():
    """C4 regression guard: every `& wsl.exe` invocation must be piped (e.g. through
    Write-Host) or captured into a variable — never left bare, which would silently pollute
    the enclosing function's return value with native command output (the exact bug that
    made every `if (-not (Some-Step))` guard always evaluate truthy)."""
    text = _read(INSTALL_PS1)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("& wsl.exe"):
            assert "|" in stripped, f"bare, unpiped wsl.exe call found: {stripped!r}"


def test_install_ps1_helpers_return_exit_code_or_hashtable_not_native_output():
    text = _read(INSTALL_PS1)
    assert "function Invoke-WslVisible" in text
    assert "function Invoke-WslSilent" in text
    assert "return $LASTEXITCODE" in text
    assert "ExitCode = $LASTEXITCODE" in text


# =============================================================================================
# (d) README.md + the runbook
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


def test_runbook_documents_the_honest_yes_contract():
    text = _read(RUNBOOK)
    assert "--yes" in text
    lowered = text.lower()
    assert "unavoidable" in lowered or "cannot" in lowered


def test_runbook_documents_stop_reality():
    text = _read(RUNBOOK)
    assert "STOP" in text


def test_runbook_documents_the_context_gate_and_exit_semantics():
    text = _read(RUNBOOK)
    lowered = text.lower()
    assert "context" in lowered
    assert "exit code" in lowered or "exit 0" in lowered


def test_runbook_documents_the_distro_marker_reuse_rule():
    text = _read(RUNBOOK)
    assert "marker" in text.lower()


def test_runbook_documents_the_parameterized_invocation_form():
    text = _read(RUNBOOK)
    assert "scriptblock" in text.lower()


# ==========================================================================================
# shared-drop-hygiene — /Users/Shared is world-readable AND world-writable, so it is where
# "temporary" hand-off artifacts accumulate. Found live 2026-08-09: a complete 643 KB copy
# of the blackboard at 0644 plus credential-shaped files, months after they were consumed.
# No other rule looks outside the account's own home.
# ==========================================================================================
def test_shared_drop_flags_a_world_readable_store_copy(tmp_path):
    shared = tmp_path / "Shared"
    (shared / "factory-seed").mkdir(parents=True)
    snap = shared / "factory-seed" / "blackboard.db"
    snap.write_bytes(b"x" * 64)
    os.chmod(snap, 0o644)

    ctx = gh.Ctx(shared_dir=str(shared),
                               bare_repo_path=str(shared / "factory.git"))
    rule = gh.rule_shared_drop_hygiene(ctx)
    assert rule.status == gh.FAIL
    assert "blackboard.db" in rule.detail


def test_shared_drop_flags_credential_shaped_files(tmp_path):
    shared = tmp_path / "Shared"
    shared.mkdir()
    for name in ("claude_oa_token.txt", "pat.txt", "fac_session.txt"):
        f = shared / name
        f.write_text("secret-shaped")
        os.chmod(f, 0o644)

    rule = gh.rule_shared_drop_hygiene(
        gh.Ctx(shared_dir=str(shared),
                             bare_repo_path=str(shared / "factory.git")))
    assert rule.status == gh.FAIL
    for name in ("claude_oa_token.txt", "pat.txt", "fac_session.txt"):
        assert name in rule.detail


def test_shared_drop_passes_when_everything_is_owner_only(tmp_path):
    shared = tmp_path / "Shared"
    shared.mkdir()
    snap = shared / "blackboard.db"
    snap.write_bytes(b"x")
    os.chmod(snap, 0o600)
    (shared / "harmless-notes.md").write_text("not sensitive")

    rule = gh.rule_shared_drop_hygiene(
        gh.Ctx(shared_dir=str(shared),
                             bare_repo_path=str(shared / "factory.git")))
    assert rule.status == gh.PASS


def test_shared_drop_never_flags_the_public_transfer_repo(tmp_path):
    """The bare repo carries the factory's own source — a PUBLIC repo (github.com/ikangai/
    factory), including scenarios/held-out and checks/. Readable there is not a disclosure,
    and flagging it would train the operator to ignore this rule."""
    shared = tmp_path / "Shared"
    bare = shared / "factory.git" / "objects"
    bare.mkdir(parents=True)
    obj = bare / "held-out-secret.db"
    obj.write_bytes(b"x")
    os.chmod(obj, 0o644)

    rule = gh.rule_shared_drop_hygiene(
        gh.Ctx(shared_dir=str(shared),
                             bare_repo_path=str(shared / "factory.git")))
    assert rule.status == gh.PASS


# ==========================================================================================
# Phase 3 boundary probes (`--boundary`) — run AS THE GRADING IDENTITY. Polarity is
# inverted: PASS means "I could NOT reach this".
# ==========================================================================================
def test_boundary_rules_pass_when_everything_is_unreachable(tmp_path):
    """The shape of a contained grader: the factory tree exists but is not readable."""
    root = tmp_path / "factory"
    root.mkdir()
    (root / "store").mkdir()
    db = root / "store" / "blackboard.db"
    db.write_bytes(b"x")
    os.chmod(db, 0o000)

    rule = gh.rule_boundary_blackboard(gh.Ctx(factory_root=str(root)))
    assert rule.status == gh.PASS and "permission denied" in rule.detail


def test_boundary_blackboard_fails_when_the_store_is_readable(tmp_path):
    """The bug this whole phase exists to close: candidate code reaching the store."""
    root = tmp_path / "factory"
    (root / "store").mkdir(parents=True)
    (root / "store" / "blackboard.db").write_bytes(b"x")

    rule = gh.rule_boundary_blackboard(gh.Ctx(factory_root=str(root)))
    assert rule.status == gh.FAIL and "IS READABLE" in rule.detail


def test_boundary_reports_honestly_when_a_path_is_simply_absent(tmp_path):
    """An absent file proves nothing about containment and must not be scored as a PASS —
    that is how a boundary check quietly becomes decorative."""
    rule = gh.rule_boundary_secrets(gh.Ctx(env_file_path=str(tmp_path / "nope")))
    assert rule.status == gh.FAIL and "nothing proven" in rule.detail


def test_boundary_killswitch_never_removes_the_stop_file(tmp_path):
    """A probe that deletes STOP to prove STOP is deletable has disarmed the brake it was
    testing. It checks the CONTAINING directory's write bit instead."""
    root = tmp_path / "factory"
    root.mkdir()
    stop = root / "STOP"
    stop.write_text("halt")

    rule = gh.rule_boundary_killswitch(gh.Ctx(factory_root=str(root)))
    assert stop.exists(), "the probe must never unlink STOP"
    assert rule.status == gh.FAIL          # this dir IS writable by the test user


def test_boundary_dashboard_probe_is_non_mutating_by_construction():
    """The first version posted to /api/resume — which SUCCEEDS when the boundary is broken,
    clearing the killswitch. It did exactly that against a live board. The probe must target
    a route whose every outcome is inert."""
    import ast
    import inspect
    import textwrap
    code = ""
    # The payload lives in the per-URL helper since the rule learned to probe more than one
    # board; the contract is over the pair.
    for target in (gh.rule_boundary_dashboard_write, gh._probe_write_route):
        fn = ast.parse(textwrap.dedent(inspect.getsource(target))).body[0]
        if (fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)):
            fn.body = fn.body[1:]      # drop the docstring: it MENTIONS /api/resume to
        code += ast.unparse(ast.Module(body=fn.body, type_ignores=[]))  # explain the incident
    assert "/api/resume" not in code, "the probe must not target a mutating route"
    assert "dashboard_settings_url" in code
    assert "__boundary_probe__" in code, "must use a key that cannot validate"


def test_boundary_audit_has_no_context_gate():
    """The account-scoped gate recognizes only the `factory` account, so boundary rules run
    as a different identity would SKIP themselves and exit 0 — a proof that passes by not
    running."""
    result = gh.audit_boundary(gh.Ctx(factory_root="/nonexistent-xyz"))
    assert result.deployed is True
    assert len(result.rules) == len(gh.BOUNDARY_RULES)
    assert not any(r.detail == gh._CONTEXT_GATED_DETAIL for r in result.rules)


# ==========================================================================================
# Drill-2 probes (2026-08-16) — the five attack classes the original six probes never asked
# about (network, Keychain, process escape, symlinks, dependency substitution) plus the
# credential reach that gives the network class its meaning, and the deployment-exposure
# rule the drill's own findings produced.
# ==========================================================================================

def test_every_drill2_attack_class_has_a_probe():
    """The roadmap sentence names six classes. A drill that silently covers four of them and
    reports green is the failure mode this whole file exists to prevent."""
    ids = {rid for rid, _ in gh.BOUNDARY_RULES}
    for expected in ("boundary-network-egress", "boundary-keychain", "boundary-other-homes",
                     "boundary-process-escape", "boundary-symlink-escape",
                     "boundary-dependency-substitution", "boundary-host-writes",
                     "boundary-credential-reach"):
        assert expected in ids, f"drill 2 lost its {expected} probe"


def test_network_egress_is_reported_but_never_a_failure():
    """Egress is not a containment claim — the deployment needs GitHub and the model API.
    Scoring it FAIL would put a permanent red row in a correct deployment's table, which is
    how operators learn to skim past red rows."""
    ctx = gh.Ctx(tcp_probe_fn=lambda t: (True, "connected"))
    rule = gh.rule_boundary_network_egress(ctx)
    assert rule.status == gh.SKIP and "REACHABLE" in rule.detail
    assert "not a containment claim" in rule.detail
    blocked = gh.rule_boundary_network_egress(gh.Ctx(tcp_probe_fn=lambda t: (False, "refused")))
    assert blocked.status == gh.SKIP


def test_credential_reach_fails_on_a_readable_env_file(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text("GH_TOKEN=ghp_realsecret\n")
    ctx = gh.Ctx(env_file_path=str(env_file), git_credentials_path=str(tmp_path / "none"),
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "not logged in"))
    rule = gh.rule_boundary_credential_reach(ctx)
    assert rule.status == gh.FAIL and str(env_file) in rule.detail


def test_credential_reach_fails_when_gh_holds_a_token(tmp_path):
    """`gh auth status` is read-only and never asked for the token itself (`--show-token`
    would print a live credential into a drill log)."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "  ✓ Logged in to github.com")

    ctx = gh.Ctx(env_file_path=str(tmp_path / "none"),
                 git_credentials_path=str(tmp_path / "none"), run=fake_run)
    rule = gh.rule_boundary_credential_reach(ctx)
    assert rule.status == gh.FAIL and "github.com" in rule.detail
    assert ["gh", "auth", "status"] in calls
    assert not any("--show-token" in a for a in calls[0]), "must never print the token"


def test_credential_reach_passes_when_nothing_can_publish(tmp_path):
    ctx = gh.Ctx(env_file_path=str(tmp_path / "none"),
                 git_credentials_path=str(tmp_path / "none"),
                 run=lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "not logged in"))
    assert gh.rule_boundary_credential_reach(ctx).status == gh.PASS


def test_keychain_probe_reports_nothing_proven_rather_than_passing(tmp_path, monkeypatch):
    """An empty search is not containment. This probe's first cut PASSed when no other
    identity's keychain directory existed at all — the same decorative-green failure the
    absent-path rule above was written for."""
    monkeypatch.setattr(gh, "_other_user_homes", lambda ctx: [str(tmp_path / "someone")])
    (tmp_path / "someone").mkdir()
    rule = gh.rule_boundary_keychain(gh.Ctx(platform_name="Darwin"))
    assert rule.status == gh.SKIP and "nothing proven" in rule.detail


def test_keychain_probe_fails_when_another_identity_is_readable(tmp_path, monkeypatch):
    home = tmp_path / "someone"
    (home / "Library" / "Keychains").mkdir(parents=True)
    (home / "Library" / "Keychains" / "login.keychain-db").write_bytes(b"x")
    monkeypatch.setattr(gh, "_other_user_homes", lambda ctx: [str(home)])
    rule = gh.rule_boundary_keychain(gh.Ctx(platform_name="Darwin"))
    assert rule.status == gh.FAIL and "in reach" in rule.detail


def test_process_escape_fails_on_a_sudo_grant_beyond_the_grading_wrapper():
    def fake_run(argv, **kwargs):
        if argv[:2] == ["sudo", "-n"]:
            return subprocess.CompletedProcess(argv, 0, "    (ALL) NOPASSWD: ALL\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    ctx = gh.Ctx(run=fake_run, foreign_processes_fn=lambda c: [])
    rule = gh.rule_boundary_process_escape(ctx)
    assert rule.status == gh.FAIL and "beyond the grading wrapper" in rule.detail


def test_process_escape_passes_on_exactly_the_grading_grant():
    def fake_run(argv, **kwargs):
        if argv[:2] == ["sudo", "-n"]:
            return subprocess.CompletedProcess(
                argv, 0, "    (factory-grader) NOPASSWD: /opt/factory/run-target-code\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    ctx = gh.Ctx(run=fake_run, foreign_processes_fn=lambda c: [])
    rule = gh.rule_boundary_process_escape(ctx)
    assert rule.status == gh.PASS and "exactly the grading grant" in rule.detail


def test_process_escape_never_delivers_a_signal():
    """`os.kill(pid, 0)` asks the kernel for permission and delivers nothing. A probe that
    actually signalled a process to prove it could would be the /api/resume incident again."""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(gh.rule_boundary_process_escape))
    fn = ast.parse(src).body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]
    code = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
    assert "os.kill(pid, 0)" in code, "must probe with signal 0 only"
    for lethal in ("SIGKILL", "SIGTERM", "SIGSTOP"):
        assert lethal not in code


def test_symlink_probe_says_nothing_proven_when_nothing_is_refused(tmp_path, monkeypatch):
    """With no refused path there is nothing a symlink could launder — reporting PASS there
    would credit containment that was never tested."""
    root = tmp_path / "factory"
    (root / "store").mkdir(parents=True)
    (root / "store" / "blackboard.db").write_bytes(b"x")     # readable
    # Hermetic: the probe also anchors on other accounts' homes, which on a real machine are
    # refused and would answer the question this test is asking about the tmp tree.
    monkeypatch.setattr(gh, "_other_user_homes", lambda ctx: [])
    rule = gh.rule_boundary_symlink_escape(
        gh.Ctx(factory_root=str(root), env_file_path=str(tmp_path / "none"),
               config_path=str(tmp_path / "none")))
    assert rule.status == gh.SKIP and "nothing proven" in rule.detail


def test_symlink_probe_passes_when_a_link_cannot_launder_access(tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"x")
    os.chmod(secret, 0o000)
    rule = gh.rule_boundary_symlink_escape(
        gh.Ctx(factory_root=str(tmp_path / "nofactory"), env_file_path=str(secret),
               config_path=str(tmp_path / "none")))
    assert rule.status == gh.PASS and "through a symlink" in rule.detail


def test_dependency_substitution_fails_on_a_writable_import_dir(tmp_path):
    root = tmp_path / "parent" / "factory"
    root.mkdir(parents=True)
    ctx = gh.Ctx(factory_root=str(root), import_path_dirs_fn=lambda: [], environ={})
    rule = gh.rule_boundary_dependency_substitution(ctx)
    assert rule.status == gh.FAIL and "import path" in rule.detail
    assert str(tmp_path / "parent") in rule.detail, "the dir `python3 -m factory.*` resolves from"


def test_dependency_substitution_flags_an_auto_imported_hook(tmp_path):
    root = tmp_path / "parent" / "factory"
    root.mkdir(parents=True)
    (root / "sitecustomize.py").write_text("# imported with no import statement anywhere\n")
    ctx = gh.Ctx(factory_root=str(root), import_path_dirs_fn=lambda: [], environ={})
    assert "sitecustomize.py" in gh.rule_boundary_dependency_substitution(ctx).detail


def test_dependency_substitution_passes_when_the_import_path_is_read_only(tmp_path):
    parent = tmp_path / "ro"
    root = parent / "factory"
    root.mkdir(parents=True)
    os.chmod(root, 0o500)
    os.chmod(parent, 0o500)
    try:
        ctx = gh.Ctx(factory_root=str(root), import_path_dirs_fn=lambda: [], environ={})
        rule = gh.rule_boundary_dependency_substitution(ctx)
        assert rule.status == gh.PASS, rule.detail
    finally:
        os.chmod(parent, 0o700)
        os.chmod(root, 0o700)


def test_host_writes_reports_nothing_proven_when_no_host_path_exists(tmp_path):
    ctx = gh.Ctx(host_write_paths=(str(tmp_path / "absent"),),
                 grader_wrapper_path=str(tmp_path / "absent-wrapper"),
                 home=str(tmp_path))
    rule = gh.rule_boundary_host_writes(ctx)
    assert rule.status == gh.SKIP and "nothing proven" in rule.detail


def test_host_writes_fails_on_a_writable_grading_wrapper(tmp_path):
    """The wrapper is root-owned for a reason: whoever can rewrite it decides what "run the
    candidate's tests in a confined identity" means."""
    wrapper = tmp_path / "run-target-code"
    wrapper.write_text("#!/bin/sh\n")
    ctx = gh.Ctx(host_write_paths=(), grader_wrapper_path=str(wrapper), home=str(tmp_path))
    rule = gh.rule_boundary_host_writes(ctx)
    assert rule.status == gh.FAIL and str(wrapper) in rule.detail


# -- the finding drill 2 produced: a peer local account reading the deployment -------------
def test_deployment_peer_readable_fails_on_the_shape_drill2_found(tmp_path):
    """The operator's own deployed guest house, 2026-08-16: /Users/factory at 0750 group
    `staff` (every macOS account is in `staff`) over a 0755 tree of 0644 files — so an
    unrelated local account could read the blackboard and the config."""
    home = tmp_path / "factory"
    root = home / "fab" / "factory"
    (root / "store").mkdir(parents=True)
    (root / "store" / "blackboard.db").write_bytes(b"SQLite format 3\x00")
    (root / "config.yaml").write_text("dashboard: {host: 127.0.0.1}\n")
    os.chmod(home, 0o750)
    rule = gh.rule_deployment_not_peer_readable(
        gh.Ctx(home=str(home), factory_root=str(root), username="factory"))
    assert rule.status == gh.FAIL
    assert "store/blackboard.db" in rule.detail and "config.yaml" in rule.detail
    assert f"chmod 700 {home}" in rule.detail


def test_deployment_peer_readable_passes_on_a_closed_home(tmp_path):
    home = tmp_path / "factory"
    root = home / "fab" / "factory"
    (root / "store").mkdir(parents=True)
    (root / "store" / "blackboard.db").write_bytes(b"x")     # still 0644 inside
    os.chmod(home, 0o700)
    rule = gh.rule_deployment_not_peer_readable(
        gh.Ctx(home=str(home), factory_root=str(root), username="factory"))
    assert rule.status == gh.PASS and "no peer account can enter" in rule.detail


def test_deployment_peer_readable_ignores_files_behind_a_closed_ancestor(tmp_path):
    """A 0644 file under a 0700 directory is not exposed. Reporting it would bury the real
    finding in noise the operator cannot act on."""
    home = tmp_path / "factory"
    root = home / "fab" / "factory"
    (root / "store").mkdir(parents=True)
    (root / "store" / "blackboard.db").write_bytes(b"x")
    (root / "config.yaml").write_text("x\n")
    os.chmod(home, 0o755)
    os.chmod(root / "store", 0o700)
    try:
        rule = gh.rule_deployment_not_peer_readable(
            gh.Ctx(home=str(home), factory_root=str(root), username="factory"))
        assert "store/blackboard.db" not in rule.detail
        assert rule.status == gh.FAIL and "config.yaml" in rule.detail   # this one IS exposed
    finally:
        os.chmod(root / "store", 0o700)


def test_deployment_peer_readable_is_context_gated():
    """On an ordinary developer checkout the question is wrong — an operator's own tree under
    an operator's own home. It gates with the other account-scoped rules."""
    assert "deployment-not-peer-readable" in gh.ACCOUNT_SCOPED_RULE_IDS


def test_boundary_banner_names_the_identity_and_the_polarity(tmp_path):
    """Run as the tree's owner the FAILs are correct, not defects. A table that cannot be
    read without knowing which of those two situations you are in is a trap."""
    root = tmp_path / "factory"
    root.mkdir()
    banner = gh.boundary_banner(gh.Ctx(factory_root=str(root), username=getpass.getuser()))
    assert "PASS means" in banner and "could NOT" in banner
    assert "negative control" in banner


def test_boundary_config_probe_derives_its_path(tmp_path):
    """It used to read ctx.config_path alone, which nothing defaults — so every real run
    reported `FAIL no path configured`, a breach report caused by an unset field."""
    root = tmp_path / "factory"
    root.mkdir()
    (root / "config.yaml").write_text("x\n")
    rule = gh.rule_boundary_config(gh.Ctx(factory_root=str(root)))
    assert "no path configured" not in rule.detail
    assert str(root / "config.yaml") in rule.detail


# ==========================================================================================
# The wrong-account audit (drill 2's perimeter run, 2026-08-16). `sudo -u factory` rewrites
# USER/LOGNAME but NOT $HOME, so the doctor reported "'factory' is a standard user" in the
# same table where home-dir-perms measured /Users/martintreiber — and passed, because the
# operator's own home is well-formed. A green table certifying the wrong account is exactly
# what the context gate exists to prevent, re-entering through the environment.
# ==========================================================================================

def test_home_comes_from_the_passwd_entry_not_from_env(monkeypatch, tmp_path):
    real_home = tmp_path / "factory-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "operator-home"))
    monkeypatch.setattr(gh, "_default_username", lambda: "factory")
    monkeypatch.setattr(gh, "_account_home",
                        lambda name: str(real_home) if name == "factory" else None)
    assert gh._default_home() == str(real_home)
    assert gh.Ctx().home == str(real_home)


def test_home_falls_back_to_env_when_passwd_has_no_entry(monkeypatch, tmp_path):
    """A container/WSL layout where the account isn't in the local passwd db must still
    audit something rather than crash."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(gh, "_default_username", lambda: "nosuchuser")
    monkeypatch.setattr(gh, "_account_home", lambda name: None)
    assert gh._default_home() == str(tmp_path)


def test_home_env_mismatch_names_the_sudo_cause():
    ctx = gh.Ctx(username="factory", home="/Users/factory",
                 environ={"HOME": "/Users/martintreiber"})
    msg = gh.home_env_mismatch(ctx)
    assert msg and "sudo" in msg and "-H" in msg
    assert "/Users/factory" in msg and "/Users/martintreiber" in msg


def test_home_env_mismatch_is_silent_when_they_agree():
    ctx = gh.Ctx(username="factory", home="/Users/factory",
                 environ={"HOME": "/Users/factory"})
    assert gh.home_env_mismatch(ctx) is None


def test_home_dir_perms_measures_the_audited_account(tmp_path, monkeypatch):
    """The regression in one assertion: the rule must report on the account named in the
    table, whatever the invoking shell's HOME says."""
    audited = tmp_path / "factory"
    audited.mkdir()
    os.chmod(audited, 0o750)                       # the shape drill 2 found
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setattr(gh, "_default_username", lambda: "factory")
    monkeypatch.setattr(gh, "_account_home", lambda name: str(audited))
    rule = gh.rule_home_dir_perms(gh.Ctx())
    assert rule.status == gh.FAIL and str(audited) in rule.detail


def test_ssh_agent_half_is_not_answered_by_another_accounts_agent(tmp_path, monkeypatch):
    """Same class as the $HOME leak: sudo carries SSH_AUTH_SOCK across, so `ssh-add -l`
    would describe the invoking user's agent under the audited account's name."""
    sock = tmp_path / "agent.sock"
    sock.write_bytes(b"")
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "256 SHA256:xxx operator@mac (ED25519)\n", "")

    monkeypatch.setattr(gh, "_account_uid", lambda name: os.getuid() + 1)   # not this uid
    rule = gh.rule_no_ssh_access(gh.Ctx(username="factory", ssh_dir=str(ssh_dir),
                                        environ={"SSH_AUTH_SOCK": str(sock)}, run=fake_run))
    assert not calls, "must not ask an agent that belongs to another account"
    assert rule.status == gh.SKIP and "agent check skipped" in rule.detail
    assert "no private key material" in rule.detail    # the half that DID hold, stated


def test_ssh_agent_half_still_runs_for_your_own_agent(tmp_path, monkeypatch):
    sock = tmp_path / "agent.sock"
    sock.write_bytes(b"")
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "256 SHA256:xxx me@mac (ED25519)\n", "")

    monkeypatch.setattr(gh, "_account_uid", lambda name: os.getuid())
    rule = gh.rule_no_ssh_access(gh.Ctx(username=getpass.getuser(), ssh_dir=str(ssh_dir),
                                        environ={"SSH_AUTH_SOCK": str(sock)}, run=fake_run))
    assert rule.status == gh.FAIL and "loaded identities" in rule.detail


def test_json_output_names_the_account_it_audited(capsys, monkeypatch, tmp_path):
    """Machine-readable consumers need to be able to catch a wrong-account audit too — the
    account and home the run actually used are part of the result, not just the banner."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setattr(gh, "_default_username", lambda: "factory")
    monkeypatch.setattr(gh, "_account_home", lambda name: str(tmp_path / "factory"))
    (tmp_path / "factory").mkdir()
    gh.main(["--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["auditing"] == "factory"
    assert doc["home"] == str(tmp_path / "factory")
    assert "sudo" in (doc["home_env_mismatch"] or "")


def test_dashboard_probe_targets_the_board_this_deployment_runs(tmp_path):
    """It used to hold a literal `127.0.0.1:9788`. On the deployed guest house (board on
    9787) and on this checkout (8787) that reported a clean 403 from a board belonging to
    neither — a green row about something other than the thing under test, which is the
    wrong-account audit's failure mode wearing a port number."""
    root = tmp_path / "factory"
    root.mkdir()
    (root / "config.yaml").write_text("dashboard:\n  host: 127.0.0.1\n  port: 9787\n")
    ctx = gh.Ctx(factory_root=str(root))
    assert ctx.dashboard_settings_url == "http://127.0.0.1:9787/api/settings"
    # ...and the second board (viz --serve), which no config field carries, is still probed
    assert any(":9788/api/settings" in u for u in ctx.extra_dashboard_urls)


def test_dashboard_probe_falls_back_when_the_config_is_unreadable(tmp_path):
    """From a contained grading identity the config IS unreadable — the probe must still
    have somewhere to aim, and its own SKIP path reports an unreachable board honestly."""
    ctx = gh.Ctx(factory_root=str(tmp_path / "nothing-here"))
    assert ctx.dashboard_settings_url == "http://127.0.0.1:9788/api/settings"


def test_dashboard_probe_survives_a_config_that_is_not_a_mapping(tmp_path):
    root = tmp_path / "factory"
    root.mkdir()
    (root / "config.yaml").write_text("just-a-scalar\n")
    assert gh.Ctx(factory_root=str(root)).dashboard_settings_url.endswith("/api/settings")


def test_dashboard_rule_fails_if_any_board_accepts(monkeypatch, tmp_path):
    def fake_probe(url):
        return (gh.FAIL, "ACCEPTED an unauthenticated write (200)") if "9788" in url \
            else (gh.PASS, "refused unauthenticated write (403)")

    monkeypatch.setattr(gh, "_probe_write_route", fake_probe)
    rule = gh.rule_boundary_dashboard_write(
        gh.Ctx(dashboard_settings_url="http://127.0.0.1:9787/api/settings",
               extra_dashboard_urls=("http://127.0.0.1:9788/api/settings",)))
    assert rule.status == gh.FAIL and "9788" in rule.detail


def test_symlink_probe_can_anchor_on_a_refused_directory(tmp_path, monkeypatch):
    """On a correctly closed deployment the other account's HOME is often the only refused
    path visible — everything under it is invisible rather than refused. Without a directory
    anchor the probe skipped itself exactly where containment was tightest (drill 2's
    perimeter run said "nothing is refused to this identity" on an account that could not
    enter the operator's home at all)."""
    other = tmp_path / "someone"
    other.mkdir()
    os.chmod(other, 0o000)
    monkeypatch.setattr(gh, "_other_user_homes", lambda ctx: [str(other)])
    try:
        rule = gh.rule_boundary_symlink_escape(
            gh.Ctx(factory_root=str(tmp_path / "nofactory"),
                   env_file_path=str(tmp_path / "none"), config_path=str(tmp_path / "none")))
        assert rule.status == gh.PASS and str(other) in rule.detail
    finally:
        os.chmod(other, 0o700)
