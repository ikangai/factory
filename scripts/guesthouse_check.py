#!/usr/bin/env python3
"""guesthouse_check.py — the deterministic doctor for the guest-house isolation rules
(docs/plans/2026-08-06-production-hardening-roadmap.md, Phase 0, binding principle 5:
"Deterministic doctors over prose checklists"; docs/runbooks/guest-house.md documents each
rule in prose — this module VERIFIES).

Standalone: `python3 scripts/guesthouse_check.py [--json]`. Also invoked by
`install.sh --guest-house` as its final step.

Design: ten small, PURE probe functions (`rule_*`), each taking one `Ctx` (every external
dependency — platform, subprocess runner, environment, filesystem paths — injected with a
real-world default) and returning one `Rule(id, status, detail)`. `audit()` runs the fixed
rule list under a CONTEXT GATE (see below) and returns an `AuditResult(rules, deployed)`;
`main()` renders it as a table (or `--json`) and picks the exit code.

Context gate (fix round, finding B5/I9): seven of the ten rules are ACCOUNT-scoped — they
ask "is THIS ACCOUNT a safely isolated guest-house user" (standard-user, no-passwordless-
sudo, home-dir-perms, no-ssh-access, no-docker-socket, runtime-read-only, credentials-
hygiene). Run on an ordinary operator/developer account (not the deployed 'factory' user,
no ~/fab/factory or WSL install root present), every one of those questions is simply the
wrong question — of COURSE the operator has admin rights, SSH keys, and Docker access; that
is not a guest-house violation, it is a different account entirely being asked about the
wrong thing. `is_guest_house_context()` detects whether the account being audited actually
IS a deployed guest-house layout; if not, those seven rules are not even invoked (no
subprocess calls, no filesystem probing beyond the detection itself) and are reported as an
explicit SKIP, and the overall exit code is always 0 — auditing a non-guest-house account is
not a failure of anything. The three checkout-scoped rules (brakes-engaged, dashboard-
localhost, wsl-hardening) still run and still affect the exit code in DEPLOYED context; in
non-deployed context they still run (their own file-presence SKIPs already handle "not
applicable" honestly) but never flip the exit code, per the same "exit 0 outside guest-house
context" contract.

This doctor NEVER mutates anything: every probe only reads (stat/listdir/env/a read-only
subprocess like `sudo -n -v`, `dseditgroup -o checkmember`, or `ssh-add -l`). NOTE (B2): a
`sudo -n -v` probe against a REAL host, if a sudo timestamp is already cached, can EXTEND
that cached timestamp's expiry as an inherent side effect of how `sudo -v` works — there is
no side-effect-free way to test cached-credential state. Tests must never invoke this rule
against the real host (inject `ctx.run`); the doctor's own CLI naturally does invoke it for
real, which is the intended use.

Tests inject `Ctx` fields directly (e.g. `Ctx(euid=0)`, a fake `run`, tmp-dir paths) — no
test needs to actually run as root, another user, or touch the real system.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import platform
import re
import stat
import subprocess
import sys
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import yaml

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

Rule = namedtuple("Rule", "id status detail")
AuditResult = namedtuple("AuditResult", "rules deployed")

# Positive identification of private-key MATERIAL (B4/M6) — not a deny-list of "everything
# that isn't a known-benign filename". A file counts as key material if its basename looks
# like an OpenSSH identity file (id_*) OR its first line is a PEM/OpenSSH private-key header.
_PRIVATE_KEY_BASENAME_RE = re.compile(r"^id_[A-Za-z0-9_-]+$")
_PRIVATE_KEY_HEADER_RE = re.compile(r"^-----BEGIN .*PRIVATE KEY-----")

# Rules that ask a question about the CURRENT ACCOUNT's own identity/permissions rather than
# about a specific checkout's files — see the context-gate note in the module docstring.
ACCOUNT_SCOPED_RULE_IDS = {
    "standard-user", "no-passwordless-sudo", "home-dir-perms", "no-ssh-access",
    "no-docker-socket", "runtime-read-only", "credentials-hygiene",
}

NON_GUEST_HOUSE_BANNER = (
    "NOTE: this account does not look like a deployed guest-house user (not named "
    "'factory', no ~/fab/factory, no WSL install root found) — the seven account-scoped "
    "rules below SKIP rather than report on the wrong account, and the exit code is always "
    "0 in this context. Run this on the deployed account for strict PASS/FAIL semantics."
)
_CONTEXT_GATED_DETAIL = (
    "auditing a non-guest-house account — account-scoped rule skipped (not the deployed "
    "'factory' user, no ~/fab/factory or WSL install root found)"
)


def _default_username() -> str:
    for key in ("USER", "USERNAME", "LOGNAME"):
        v = os.environ.get(key)
        if v:
            return v
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return "unknown"


def _default_factory_root() -> str:
    # scripts/guesthouse_check.py -> factory/
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Ctx:
    """Every external dependency a probe needs, with a real-world default. Tests override
    fields directly instead of monkeypatching os/subprocess globally."""
    platform_name: str = field(default_factory=platform.system)
    username: str = field(default_factory=_default_username)
    home: str = field(default_factory=lambda: os.path.expanduser("~"))
    environ: Dict[str, str] = field(default_factory=lambda: dict(os.environ))
    run: Callable[..., "subprocess.CompletedProcess"] = subprocess.run
    is_posix: bool = field(default_factory=lambda: hasattr(os, "getuid"))
    euid: Optional[int] = field(default_factory=lambda: os.geteuid() if hasattr(os, "geteuid") else None)
    current_uid_fn: Callable[[], Optional[int]] = field(
        default_factory=lambda: (os.getuid if hasattr(os, "getuid") else (lambda: None)))
    stat_uid_fn: Optional[Callable[[str], int]] = None
    docker_socket_access_fn: Optional[Callable[[str], bool]] = None
    access_w_fn: Optional[Callable[[str], bool]] = None
    ssh_dir: Optional[str] = None
    docker_socket: str = "/var/run/docker.sock"
    bare_repo_path: str = "/Users/Shared/factory.git"
    factory_root: Optional[str] = None
    env_file_path: Optional[str] = None
    config_path: Optional[str] = None
    wsl_conf_path: str = "/etc/wsl.conf"
    proc_version_path: str = "/proc/version"
    wsl_install_root: Optional[str] = None

    def __post_init__(self):
        if self.ssh_dir is None:
            self.ssh_dir = os.path.join(self.home, ".ssh")
        if self.factory_root is None:
            self.factory_root = _default_factory_root()
        if self.env_file_path is None:
            self.env_file_path = os.path.join(self.home, ".factory-secrets", "env")
        if self.stat_uid_fn is None:
            self.stat_uid_fn = lambda p: os.stat(p).st_uid
        if self.docker_socket_access_fn is None:
            self.docker_socket_access_fn = lambda p: os.access(p, os.R_OK)
        if self.access_w_fn is None:
            self.access_w_fn = lambda p: os.access(p, os.W_OK)
        if self.wsl_install_root is None:
            # the unified WSL guest-house layout install.sh --guest-house --wsl defaults to
            self.wsl_install_root = os.path.join(self.home, "factories", "guest-house", "factory")


def is_guest_house_context(ctx: Ctx) -> bool:
    """True iff the account being audited looks like an actually-deployed guest-house user —
    either literally named 'factory', or a deploy layout (~/fab/factory on macOS, or the
    unified WSL install root) is present. See the module docstring's context-gate note."""
    if ctx.username == "factory":
        return True
    if os.path.isdir(os.path.join(ctx.home, "fab", "factory")):
        return True
    if ctx.wsl_install_root and os.path.isdir(ctx.wsl_install_root):
        return True
    return False


def _detect_wsl(ctx: Ctx) -> bool:
    """Env-var-independent WSL detection (B6): WSL_DISTRO_NAME/WSL_INTEROP are stripped by
    `sudo` (no -E) and WSL_INTEROP specifically disappears once interop is hardened OFF —
    exactly the post-hardening, via-sudo scenario this doctor most needs to detect correctly.
    /proc/version containing 'microsoft' is set by the WSL kernel itself, independent of any
    environment the caller was invoked with."""
    try:
        with open(ctx.proc_version_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError:
        return False
    return "microsoft" in content.lower()


# --- rule 1: standard (non-admin, non-root) user -------------------------------------------
def rule_standard_user(ctx: Ctx) -> Rule:
    """Fails CLOSED (B1): an unresolvable/ambiguous membership check is a FAIL, not a PASS —
    an audit that silently passes when it can't actually determine the answer is worse than
    useless."""
    rid = "standard-user"
    if ctx.platform_name not in ("Darwin", "Linux"):
        return Rule(rid, SKIP, f"not macOS/Linux (platform={ctx.platform_name})")
    if not ctx.is_posix:
        return Rule(rid, SKIP, "no POSIX identity model on this platform")
    if ctx.euid == 0:
        return Rule(rid, FAIL, "running as root (euid 0)")
    try:
        if ctx.platform_name == "Darwin":
            r = ctx.run(["dseditgroup", "-o", "checkmember", "-m", ctx.username, "admin"],
                        capture_output=True, text=True, timeout=10)
            out = (r.stdout or "").strip().lower()
            if r.returncode == 0 and out.startswith("yes"):
                return Rule(rid, FAIL, f"'{ctx.username}' is a member of the admin group")
            if out.startswith("no"):
                return Rule(rid, PASS, f"'{ctx.username}' is a standard (non-admin) user")
            return Rule(rid, FAIL,
                         f"could not determine admin-group membership for '{ctx.username}' "
                         f"(dseditgroup rc={r.returncode}, output={out!r}) — failing closed")
        else:  # Linux
            r = ctx.run(["id", "-nG", ctx.username], capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return Rule(rid, FAIL,
                             f"could not determine group membership for '{ctx.username}' "
                             f"(id exited {r.returncode}) — failing closed")
            groups = (r.stdout or "").split()
            admin_groups = sorted(set(groups) & {"sudo", "wheel", "admin"})
            if admin_groups:
                return Rule(rid, FAIL,
                             f"'{ctx.username}' is in admin group(s): {', '.join(admin_groups)}")
            return Rule(rid, PASS, f"'{ctx.username}' is not in sudo/wheel/admin")
    except (OSError, subprocess.SubprocessError) as e:
        return Rule(rid, FAIL, f"could not determine group membership (failing closed): {e}")


# --- rule 2: no passwordless/cached sudo grant -----------------------------------------------
def rule_no_sudo_grant(ctx: Ctx) -> Rule:
    """Renamed from 'no-sudo-grant' (B2): `sudo -n -v` only ever detects a PASSWORDLESS or
    already-CACHED sudo credential — it says nothing about whether the account could sudo
    given a password (that's rule 1's job, the actual admin/sudo-group membership check). Do
    NOT run this against the real host in a test — `sudo -n -v`, when a valid ticket already
    exists, can itself EXTEND that ticket's expiry as a side effect of how sudo's timestamp
    cache works; inject `ctx.run` instead."""
    rid = "no-passwordless-sudo"
    if ctx.platform_name not in ("Darwin", "Linux"):
        return Rule(rid, SKIP, f"not macOS/Linux (platform={ctx.platform_name})")
    try:
        r = ctx.run(["sudo", "-n", "-v"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return Rule(rid, SKIP, f"sudo unavailable: {e}")
    if r.returncode == 0:
        return Rule(rid, FAIL,
                     "'sudo -n -v' succeeded — a passwordless or currently-cached sudo credential is active")
    return Rule(rid, PASS, "'sudo -n -v' failed — no passwordless/cached sudo credential right now")


# --- rule 3: home directory not world/group readable ----------------------------------------
def rule_home_dir_perms(ctx: Ctx) -> Rule:
    rid = "home-dir-perms"
    if not ctx.is_posix:
        return Rule(rid, SKIP, "no POSIX permission model on this platform")
    try:
        st = os.stat(ctx.home)
    except OSError as e:
        return Rule(rid, SKIP, f"cannot stat home dir {ctx.home}: {e}")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        return Rule(rid, FAIL,
                     f"{ctx.home} mode {oct(mode)} is group/other-accessible (want 700 or tighter)")
    return Rule(rid, PASS, f"{ctx.home} mode {oct(mode)}")


# --- rule 4: no SSH private keys / no loaded agent identities --------------------------------
def rule_no_ssh_access(ctx: Ctx) -> Rule:
    """B4 fix: `SSH_AUTH_SOCK` being SET is not itself a problem — macOS's launchd sets it in
    every session regardless of whether any key is actually loaded. The agent check now asks
    `ssh-add -l` (rc0 + output = identities ARE loaded -> FAIL; rc1 "no identities" or rc2 "no
    agent reachable" -> PASS, nothing to leak). Key-file detection is now POSITIVE
    identification (id_* basenames, or a PEM/OpenSSH private-key header line), not a
    deny-list of "everything that isn't a known-benign filename" (M6)."""
    rid = "no-ssh-access"
    problems: List[str] = []

    if ctx.environ.get("SSH_AUTH_SOCK"):
        try:
            r = ctx.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and (r.stdout or "").strip():
                first = (r.stdout or "").strip().splitlines()[0]
                problems.append(f"ssh-agent has loaded identities: {first}")
            # rc 1 = agent reachable, no identities loaded; rc 2 = no agent reachable at all —
            # neither is a problem in itself.
        except (OSError, subprocess.SubprocessError):
            pass  # ssh-add unavailable — nothing positively confirmed, don't fail on it

    if os.path.isdir(ctx.ssh_dir):
        found: List[str] = []
        try:
            for name in sorted(os.listdir(ctx.ssh_dir)):
                path = os.path.join(ctx.ssh_dir, name)
                if not os.path.isfile(path):
                    continue
                is_key = bool(_PRIVATE_KEY_BASENAME_RE.match(name))
                if not is_key:
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                            first_line = fh.readline().strip()
                        is_key = bool(_PRIVATE_KEY_HEADER_RE.match(first_line))
                    except OSError:
                        pass
                if is_key:
                    found.append(name)
        except OSError as e:
            return Rule(rid, SKIP, f"cannot list {ctx.ssh_dir}: {e}")
        if found:
            problems.append(f"private key material in {ctx.ssh_dir}: {', '.join(found)}")

    if problems:
        return Rule(rid, FAIL, "; ".join(problems))
    return Rule(rid, PASS, "no loaded ssh-agent identities and no private key material found")


# --- rule 5: no Docker socket access ----------------------------------------------------------
def rule_no_docker_socket(ctx: Ctx) -> Rule:
    rid = "no-docker-socket"
    if not os.path.exists(ctx.docker_socket):
        return Rule(rid, PASS, f"{ctx.docker_socket} not present")
    if ctx.docker_socket_access_fn(ctx.docker_socket):
        return Rule(rid, FAIL, f"{ctx.docker_socket} exists and is accessible")
    return Rule(rid, PASS, f"{ctx.docker_socket} exists but is not accessible to this user")


# --- rule 6: factory runtime read-only (bare-repo ownership split) ---------------------------
def rule_runtime_readonly(ctx: Ctx) -> Rule:
    """B7: proves READ-ONLY, not just "not owned by me" — ownership alone doesn't establish
    that group/other permission bits actually deny writes. Checks BOTH ownership and effective
    writability (`os.access(path, os.W_OK)`); either one being true is a FAIL, and the detail
    text says exactly what was proven."""
    rid = "runtime-read-only"
    if not os.path.exists(ctx.bare_repo_path):
        return Rule(rid, SKIP, f"{ctx.bare_repo_path} not present — not a deployed guest-house layout")
    if not ctx.is_posix:
        return Rule(rid, SKIP, "no POSIX ownership model on this platform")
    try:
        owner_uid = ctx.stat_uid_fn(ctx.bare_repo_path)
    except OSError as e:
        return Rule(rid, SKIP, f"cannot stat {ctx.bare_repo_path}: {e}")
    my_uid = ctx.current_uid_fn()
    if my_uid is None:
        return Rule(rid, SKIP, "cannot determine current uid")
    owned_by_self = owner_uid == my_uid
    writable = ctx.access_w_fn(ctx.bare_repo_path)
    if owned_by_self or writable:
        reasons = []
        if owned_by_self:
            reasons.append("owned by the current user")
        if writable:
            reasons.append("writable by the current user (os.access W_OK=True)")
        return Rule(rid, FAIL,
                     f"{ctx.bare_repo_path}: " + " and ".join(reasons) +
                     " — not read-only via the bare-repo split")
    return Rule(rid, PASS,
                 f"{ctx.bare_repo_path} owned by uid {owner_uid} (not this user) and "
                 f"os.access W_OK=False — proven not writable by this account")


# --- rule 7: credentials hygiene (the with-env.sh env file) -----------------------------------
def rule_credentials_hygiene(ctx: Ctx) -> Rule:
    rid = "credentials-hygiene"
    if not os.path.exists(ctx.env_file_path):
        return Rule(rid, SKIP, f"{ctx.env_file_path} not present")
    if not ctx.is_posix:
        return Rule(rid, SKIP, "no POSIX permission model on this platform")
    try:
        st = os.stat(ctx.env_file_path)
    except OSError as e:
        return Rule(rid, SKIP, f"cannot stat {ctx.env_file_path}: {e}")
    mode = stat.S_IMODE(st.st_mode)
    owner_uid = ctx.stat_uid_fn(ctx.env_file_path)
    my_uid = ctx.current_uid_fn()
    problems = []
    if mode != 0o600:
        problems.append(f"mode {oct(mode)} (want 0o600)")
    if my_uid is not None and owner_uid != my_uid:
        problems.append(f"owned by uid {owner_uid}, not the current user")
    if problems:
        return Rule(rid, FAIL, f"{ctx.env_file_path}: " + "; ".join(problems))
    return Rule(rid, PASS, f"{ctx.env_file_path} is mode 0600 and owned by the current user")


# --- rule 8: brakes engaged (STOP present, mode != auto) --------------------------------------
def rule_brakes_engaged(ctx: Ctx) -> Rule:
    rid = "brakes-engaged"
    if not ctx.factory_root or not os.path.isdir(ctx.factory_root):
        return Rule(rid, SKIP, "no factory directory found")
    stop_path = os.path.join(ctx.factory_root, "STOP")
    mode_path = os.path.join(ctx.factory_root, ".factory-mode")
    stop_present = os.path.exists(stop_path)
    mode = "shift"
    if os.path.exists(mode_path):
        try:
            with open(mode_path, "r", encoding="utf-8") as fh:
                m = fh.read().strip().lower()
            if m in ("auto", "shift"):
                mode = m
        except OSError:
            pass
    if stop_present and mode != "auto":
        return Rule(rid, PASS, f"STOP present, mode={mode}")
    reasons = []
    if not stop_present:
        reasons.append("STOP absent")
    if mode == "auto":
        reasons.append("mode=auto")
    return Rule(rid, FAIL, "brakes not engaged: " + ", ".join(reasons))


# --- rule 9: dashboard bound to localhost ------------------------------------------------------
def rule_dashboard_localhost(ctx: Ctx) -> Rule:
    rid = "dashboard-localhost"
    config_path = ctx.config_path
    if config_path is None and ctx.factory_root:
        config_path = os.path.join(ctx.factory_root, "config.yaml")
    if not config_path or not os.path.exists(config_path):
        return Rule(rid, SKIP, "config.yaml not found")
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as e:
        return Rule(rid, SKIP, f"cannot read/parse {config_path}: {e}")
    host = str((doc.get("dashboard") or {}).get("host") or "").strip().lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return Rule(rid, PASS,
                     f"dashboard.host = {host!r} (fleet server defaults to 127.0.0.1 in code unless overridden)")
    return Rule(rid, FAIL, f"dashboard.host = {host!r} is not localhost-only")


# --- rule 10: WSL-only — automount/interop hardening -------------------------------------------
def rule_wsl_hardening(ctx: Ctx) -> Rule:
    rid = "wsl-hardening"
    if not _detect_wsl(ctx):
        return Rule(rid, SKIP, "not running inside WSL")
    if not os.path.exists(ctx.wsl_conf_path):
        return Rule(rid, FAIL, f"{ctx.wsl_conf_path} not present — automount/interop hardening not applied")
    try:
        with open(ctx.wsl_conf_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        return Rule(rid, SKIP, f"cannot read {ctx.wsl_conf_path}: {e}")
    cfg = configparser.ConfigParser()
    try:
        cfg.read_string(text)
    except configparser.Error as e:
        return Rule(rid, FAIL, f"{ctx.wsl_conf_path} does not parse as ini: {e}")
    automount_off = (cfg.has_section("automount")
                      and cfg["automount"].get("enabled", "true").strip().lower() == "false")
    interop_off = (cfg.has_section("interop")
                    and cfg["interop"].get("enabled", "true").strip().lower() == "false")
    if automount_off and interop_off:
        return Rule(rid, PASS, f"{ctx.wsl_conf_path}: automount and interop both disabled")
    missing = []
    if not automount_off:
        missing.append("[automount] enabled=false")
    if not interop_off:
        missing.append("[interop] enabled=false")
    return Rule(rid, FAIL, f"{ctx.wsl_conf_path} missing: {', '.join(missing)}")


RULES: List[Tuple[str, Callable[[Ctx], Rule]]] = [
    ("standard-user", rule_standard_user),
    ("no-passwordless-sudo", rule_no_sudo_grant),
    ("home-dir-perms", rule_home_dir_perms),
    ("no-ssh-access", rule_no_ssh_access),
    ("no-docker-socket", rule_no_docker_socket),
    ("runtime-read-only", rule_runtime_readonly),
    ("credentials-hygiene", rule_credentials_hygiene),
    ("brakes-engaged", rule_brakes_engaged),
    ("dashboard-localhost", rule_dashboard_localhost),
    ("wsl-hardening", rule_wsl_hardening),
]


def audit(ctx: Optional[Ctx] = None) -> AuditResult:
    """Runs every rule under the context gate (module docstring) and returns both the rule
    results and whether this account was recognized as a deployed guest-house layout."""
    ctx = ctx or Ctx()
    deployed = is_guest_house_context(ctx)
    rules: List[Rule] = []
    for rule_id, fn in RULES:
        if not deployed and rule_id in ACCOUNT_SCOPED_RULE_IDS:
            rules.append(Rule(rule_id, SKIP, _CONTEXT_GATED_DETAIL))
        else:
            rules.append(fn(ctx))
    return AuditResult(rules=rules, deployed=deployed)


def run_all(ctx: Optional[Ctx] = None) -> List[Rule]:
    """Back-compat convenience for callers/tests that only want the rule list, without the
    context-gate flag `audit()` also returns."""
    return audit(ctx).rules


def render_table(rules: List[Rule]) -> str:
    id_w = max((len(r.id) for r in rules), default=2)
    header = f"{'RULE':<{id_w}}  {'STATUS':<4}  DETAIL"
    lines = [header, "-" * len(header)]
    for r in rules:
        lines.append(f"{r.id:<{id_w}}  {r.status:<4}  {r.detail}")
    passed = sum(1 for r in rules if r.status == PASS)
    failed = sum(1 for r in rules if r.status == FAIL)
    skipped = sum(1 for r in rules if r.status == SKIP)
    lines.append("")
    lines.append(f"{passed} pass, {failed} fail, {skipped} skip")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guest-house deterministic doctor — audits the guest-house isolation rules "
                     "(docs/runbooks/guest-house.md). Never mutates anything.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
    args = parser.parse_args(argv)

    result = audit()
    if args.json:
        print(json.dumps({"deployed": result.deployed,
                           "rules": [r._asdict() for r in result.rules]}, indent=2))
    else:
        if not result.deployed:
            print(NON_GUEST_HOUSE_BANNER)
            print()
        print(render_table(result.rules))
    if not result.deployed:
        return 0
    return 1 if any(r.status == FAIL for r in result.rules) else 0


if __name__ == "__main__":
    sys.exit(main())
