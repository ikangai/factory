#!/usr/bin/env python3
"""guesthouse_check.py — the deterministic doctor for the guest-house isolation rules
(docs/plans/2026-08-06-production-hardening-roadmap.md, Phase 0, binding principle 5:
"Deterministic doctors over prose checklists"; docs/runbooks/guest-house.md documents each
rule in prose — this module VERIFIES).

Standalone: `python3 scripts/guesthouse_check.py [--json]`. Also invoked by
`install.sh --guest-house` as its final step.

Design: ten small, PURE probe functions (`rule_*`), each taking one `Ctx` (every external
dependency — platform, subprocess runner, environment, filesystem paths — injected with a
real-world default) and returning one `Rule(id, status, detail)`. `main()` runs the fixed
rule list, renders a table (or `--json`), and exits 1 iff any rule FAILed (0 if every rule
PASSed or SKIPped). Every rule SKIPs with a reason instead of erroring when its precondition
doesn't hold on this platform/layout — a fresh, non-deployed checkout must never look like a
failure.

This doctor NEVER mutates anything: every probe only reads (stat/listdir/env/a read-only
subprocess like `sudo -n -v` or `dseditgroup -o checkmember`). Tests inject `Ctx` fields
directly (e.g. `Ctx(euid=0)`, a fake `run`, tmp-dir paths) — no test needs to actually run as
root, another user, or touch the real system.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import platform
import stat
import subprocess
import sys
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import yaml

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

Rule = namedtuple("Rule", "id status detail")

_NON_KEY_SSH_FILES = {"known_hosts", "known_hosts.old", "config", "authorized_keys",
                       "environment"}


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
    ssh_dir: Optional[str] = None
    docker_socket: str = "/var/run/docker.sock"
    bare_repo_path: str = "/Users/Shared/factory.git"
    factory_root: Optional[str] = None
    env_file_path: Optional[str] = None
    config_path: Optional[str] = None
    wsl_conf_path: str = "/etc/wsl.conf"

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


# --- rule 1: standard (non-admin, non-root) user -------------------------------------------
def rule_standard_user(ctx: Ctx) -> Rule:
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
            if out.startswith("yes"):
                return Rule(rid, FAIL, f"'{ctx.username}' is a member of the admin group")
            return Rule(rid, PASS, f"'{ctx.username}' is a standard (non-admin) user")
        else:  # Linux
            r = ctx.run(["id", "-nG", ctx.username], capture_output=True, text=True, timeout=10)
            groups = (r.stdout or "").split()
            admin_groups = sorted(set(groups) & {"sudo", "wheel", "admin"})
            if admin_groups:
                return Rule(rid, FAIL,
                             f"'{ctx.username}' is in admin group(s): {', '.join(admin_groups)}")
            return Rule(rid, PASS, f"'{ctx.username}' is not in sudo/wheel/admin")
    except (OSError, subprocess.SubprocessError) as e:
        return Rule(rid, SKIP, f"could not determine group membership: {e}")


# --- rule 2: no sudo grant ------------------------------------------------------------------
def rule_no_sudo_grant(ctx: Ctx) -> Rule:
    rid = "no-sudo-grant"
    if ctx.platform_name not in ("Darwin", "Linux"):
        return Rule(rid, SKIP, f"not macOS/Linux (platform={ctx.platform_name})")
    try:
        r = ctx.run(["sudo", "-n", "-v"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return Rule(rid, SKIP, f"sudo unavailable: {e}")
    if r.returncode == 0:
        return Rule(rid, FAIL, "'sudo -n -v' succeeded — a passwordless/cached sudo grant is active")
    return Rule(rid, PASS, "'sudo -n -v' failed — no active sudo grant")


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


# --- rule 4: no SSH private keys / no agent socket -------------------------------------------
def rule_no_ssh_access(ctx: Ctx) -> Rule:
    rid = "no-ssh-access"
    auth_sock = ctx.environ.get("SSH_AUTH_SOCK")
    if auth_sock:
        return Rule(rid, FAIL, f"SSH_AUTH_SOCK is set ({auth_sock}) — an agent socket is reachable")
    if not os.path.isdir(ctx.ssh_dir):
        return Rule(rid, PASS, f"no {ctx.ssh_dir} directory and SSH_AUTH_SOCK unset")
    found: List[str] = []
    try:
        for name in sorted(os.listdir(ctx.ssh_dir)):
            path = os.path.join(ctx.ssh_dir, name)
            if not os.path.isfile(path):
                continue
            if name.endswith(".pub") or name in _NON_KEY_SSH_FILES:
                continue
            found.append(name)
    except OSError as e:
        return Rule(rid, SKIP, f"cannot list {ctx.ssh_dir}: {e}")
    if found:
        return Rule(rid, FAIL, f"private key material in {ctx.ssh_dir}: {', '.join(found)}")
    return Rule(rid, PASS, f"{ctx.ssh_dir} has no private key material, SSH_AUTH_SOCK unset")


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
    if owner_uid == my_uid:
        return Rule(rid, FAIL,
                     f"{ctx.bare_repo_path} is owned by the current user — not read-only via the bare-repo split")
    return Rule(rid, PASS, f"{ctx.bare_repo_path} owned by uid {owner_uid} (not this user) — read via group only")


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
    is_wsl = bool(ctx.environ.get("WSL_DISTRO_NAME") or ctx.environ.get("WSL_INTEROP"))
    if not is_wsl:
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


RULES: List[Callable[[Ctx], Rule]] = [
    rule_standard_user,
    rule_no_sudo_grant,
    rule_home_dir_perms,
    rule_no_ssh_access,
    rule_no_docker_socket,
    rule_runtime_readonly,
    rule_credentials_hygiene,
    rule_brakes_engaged,
    rule_dashboard_localhost,
    rule_wsl_hardening,
]


def run_all(ctx: Optional[Ctx] = None) -> List[Rule]:
    ctx = ctx or Ctx()
    return [rule(ctx) for rule in RULES]


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

    rules = run_all()
    if args.json:
        print(json.dumps([r._asdict() for r in rules], indent=2))
    else:
        print(render_table(rules))
    return 1 if any(r.status == FAIL for r in rules) else 0


if __name__ == "__main__":
    sys.exit(main())
