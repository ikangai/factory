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
    assert len(rules) == 10


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
        assert len(result.rules) == 10
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
