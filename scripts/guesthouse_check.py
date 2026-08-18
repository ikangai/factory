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
import urllib.parse
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
    # asks what a PEER can read of THIS account's deployment — on a dev checkout that is the
    # wrong question (an operator's own tree under an operator's own home), so it gates with
    # the other account-scoped rules rather than emitting a FAIL nobody should act on.
    "deployment-not-peer-readable",
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


def _tcp_probe(target: Tuple[str, int]) -> Tuple[bool, str]:
    """Open a TCP connection and close it, writing NOTHING — the whole observation is
    whether the handshake completes. Used only by the egress probe, which never FAILs (see
    `rule_boundary_network_egress`)."""
    import socket
    try:
        with socket.create_connection(target, timeout=3):
            return True, f"connected to {target[0]}:{target[1]}"
    except OSError as e:
        return False, f"{target[0]}:{target[1]}: {e}"


def _import_path_dirs() -> List[str]:
    """site-packages + the user site dir of the interpreter running this probe — the rest of
    the import path the dependency-substitution rule inspects comes from FACTORY_ROOT and
    PYTHONPATH, which are Ctx fields."""
    dirs: List[str] = []
    try:
        import site
        dirs += list(getattr(site, "getsitepackages", lambda: [])() or [])
        if getattr(site, "ENABLE_USER_SITE", False):
            user = site.getusersitepackages()
            dirs += [user] if isinstance(user, str) else list(user)
    except Exception:  # noqa: BLE001 — a probe must never die on an odd interpreter layout
        pass
    return dirs


def _foreign_processes(ctx: "Ctx") -> List[Tuple[int, str]]:
    """(pid, owner) for processes owned by a PEER identity — another human/service account,
    root excluded. Root is excluded deliberately: no unprivileged identity can signal root
    on any correctly configured system, so including it would pad the probe with an answer
    that proves nothing about the boundary drill 2 actually tests (grader vs factory,
    factory vs operator). Read-only: `ps` output, no signals sent here —
    `rule_boundary_process_escape` asks the kernel for permission with signal 0 and delivers
    nothing."""
    try:
        r = ctx.run(["ps", "-axo", "pid=,user="], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (getattr(r, "stdout", "") or "").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, owner = int(parts[0]), parts[1]
        if owner != ctx.username and owner != "root":
            out.append((pid, owner))
    return out


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


def _account_home(username: str) -> Optional[str]:
    """The home directory the PASSWD DATABASE gives for `username`, or None."""
    try:
        import pwd
        return pwd.getpwnam(username).pw_dir
    except (KeyError, ImportError, AttributeError, OSError):
        return None


def _account_uid(username: str) -> Optional[int]:
    try:
        import pwd
        return pwd.getpwnam(username).pw_uid
    except (KeyError, ImportError, AttributeError, OSError):
        return None


def _default_home() -> str:
    """The home of the account being AUDITED, resolved from the passwd database — NOT from
    `$HOME`.

    Found by drill 2's perimeter run (2026-08-16), and it had defeated the doctor's entire
    headline use: `sudo -u factory python3 …/guesthouse_check.py` leaves `$HOME` pointing at
    the INVOKING user (sudo only rewrites HOME with `-H`, or with `always_set_home` in
    sudoers, which macOS's default does not set) while rewriting `USER`/`LOGNAME` to the
    target. So `standard-user` correctly said "'factory' is a standard user" in the same
    table where `home-dir-perms` reported on `/Users/martintreiber` — and PASSed, because
    the operator's own home is well-formed. `credentials-hygiene` SKIPped "not present" for
    an env file that exists, under the audited account's real home, holding the PAT.

    A green table certifying the wrong account is the exact failure the context gate was
    built to prevent, re-entering through the environment instead of through the account
    name. The passwd entry is the account's home by definition; `$HOME` is whatever the
    calling shell happened to be carrying."""
    name = _default_username()
    return _account_home(name) or os.path.expanduser("~")


def home_env_mismatch(ctx: "Ctx") -> Optional[str]:
    """A one-line warning when `$HOME` disagrees with the audited account's real home — the
    fingerprint of `sudo` without `-H`. The audit uses the passwd home regardless; this
    exists so the operator can see WHY a table just changed under them."""
    env_home = ctx.environ.get("HOME")
    if not env_home or not ctx.home:
        return None
    if os.path.realpath(env_home) == os.path.realpath(ctx.home):
        return None
    return (f"NOTE: $HOME is {env_home} but {ctx.username}'s home is {ctx.home} — you are "
            f"probably running under `sudo` without `-H`. The audit uses {ctx.home} (the "
            f"passwd entry), which is the account's real home; before 2026-08-16 it followed "
            f"$HOME and silently audited the invoking user's account instead.")


def _dashboard_url_from_config(ctx: "Ctx") -> str:
    """`/api/settings` on the board THIS deployment actually runs, read from its own
    config.yaml. Falls back to the historic literal when the config is unreadable — from a
    contained grading identity it will be, and a probe that cannot find the board must say
    so through its own SKIP path rather than silently target a stranger."""
    host, port = "127.0.0.1", 9788
    path = os.path.join(ctx.factory_root, "config.yaml") if ctx.factory_root else ""
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            # A config that isn't a mapping (truncated, half-written, or a stray scalar) is
            # a malformed config, not a reason for the doctor to crash.
            dash = doc.get("dashboard") if isinstance(doc, dict) else None
            if isinstance(dash, dict):
                host = str(dash.get("host") or host).strip() or host
                port = int(dash.get("port") or port)
        except (OSError, yaml.YAMLError, TypeError, ValueError):
            pass
    return f"http://{host}:{port}/api/settings"


def _default_factory_root() -> str:
    """The deployment under test. `$FACTORY_ROOT` first, then the path this file sits in.

    The env var exists because the boundary probes have to run AS THE GRADING IDENTITY, and
    that identity cannot read the 0700 guest-house home where this file normally lives — so
    `05-create-grader-user.sh` installs a copy beside the wrapper at
    `/opt/factory/guesthouse_check.py`, for which the path-derived answer would be `/opt`.
    The probes need the factory root's PATH, never read access to it: being refused IS the
    result they are looking for. Found trying to follow this repo's own runbook step after
    the guest-house home was tightened (drill 2, 2026-08-16)."""
    env = os.environ.get("FACTORY_ROOT")
    if env:
        return env
    # scripts/guesthouse_check.py -> factory/
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Ctx:
    """Every external dependency a probe needs, with a real-world default. Tests override
    fields directly instead of monkeypatching os/subprocess globally."""
    platform_name: str = field(default_factory=platform.system)
    # Both default_factories go through a lambda so they resolve the module global at
    # CALL time: a dataclass field binds the function object at class-definition time,
    # which makes the identity these two derive from the one thing a test cannot
    # substitute — in the module whose whole failure mode is auditing the wrong account.
    username: str = field(default_factory=lambda: _default_username())
    home: str = field(default_factory=lambda: _default_home())
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
    shared_dir: str = "/Users/Shared"
    # None => derived in __post_init__ from THIS deployment's config.yaml. A literal default
    # meant the probe answered about whatever happened to be listening on the hardcoded port
    # — on the deployed guest house (board on 9787, this checkout on 8787) it reported a
    # clean 403 from a board belonging to neither. Same class as the wrong-account audit:
    # a green row about something other than the thing under test. Found by drill 2's
    # perimeter run, 2026-08-16.
    dashboard_settings_url: Optional[str] = None
    extra_dashboard_urls: Optional[Tuple[str, ...]] = None
    factory_root: Optional[str] = None
    env_file_path: Optional[str] = None
    config_path: Optional[str] = None
    wsl_conf_path: str = "/etc/wsl.conf"
    proc_version_path: str = "/proc/version"
    wsl_install_root: Optional[str] = None
    # -- drill-2 probe seams (2026-08-16). Same idiom as the fields above: a real-world
    # default, overridden field-by-field in tests so no test needs another identity, a real
    # keychain, or a network.
    git_credentials_path: Optional[str] = None
    grader_wrapper_path: str = "/opt/factory/run-target-code"
    egress_probe_target: Tuple[str, int] = ("1.1.1.1", 443)
    host_write_paths: Tuple[str, ...] = ("/Library/LaunchAgents", "/Library/LaunchDaemons",
                                          "/usr/local/bin", "/Users/Shared/factory.git")
    tcp_probe_fn: Optional[Callable[[Tuple[str, int]], Tuple[bool, str]]] = None
    import_path_dirs_fn: Optional[Callable[[], List[str]]] = None
    foreign_processes_fn: Optional[Callable[["Ctx"], List[Tuple[int, str]]]] = None

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
        if self.git_credentials_path is None:
            self.git_credentials_path = os.path.join(self.home, ".git-credentials")
        if self.dashboard_settings_url is None:
            self.dashboard_settings_url = _dashboard_url_from_config(self)
        if self.extra_dashboard_urls is None:
            # The `viz --serve` board is a SECOND server with its own write routes (see
            # docs/runbooks/worker-isolation.md, "the board's write channel") on its own
            # port, which no config field carries. Probe its documented default too, unless
            # that is already the config-derived one.
            legacy = "http://127.0.0.1:9788/api/settings"
            self.extra_dashboard_urls = () if legacy == self.dashboard_settings_url else (legacy,)
        if self.tcp_probe_fn is None:
            self.tcp_probe_fn = _tcp_probe
        if self.import_path_dirs_fn is None:
            self.import_path_dirs_fn = _import_path_dirs
        if self.foreign_processes_fn is None:
            self.foreign_processes_fn = _foreign_processes


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

    sock = ctx.environ.get("SSH_AUTH_SOCK")
    foreign_agent = ""
    if sock:
        # Same class as the $HOME leak (see `_default_home`): under `sudo -u factory` the
        # agent socket still belongs to the INVOKING user, so `ssh-add -l` would answer for
        # the operator's agent while the row claims to describe the guest house — in either
        # direction (a false FAIL from the operator's keys, or a false PASS). Only skip when
        # the socket demonstrably belongs to someone else; an unstattable path proves
        # nothing and keeps the original behavior.
        audited_uid = _account_uid(ctx.username)
        try:
            sock_uid = os.stat(sock).st_uid
        except OSError:
            sock_uid = None
        if sock_uid is not None and audited_uid is not None and sock_uid != audited_uid:
            foreign_agent = (f"agent check skipped: SSH_AUTH_SOCK belongs to uid {sock_uid}, "
                             f"not {ctx.username} (uid {audited_uid})")
    if sock and not foreign_agent:
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
    if foreign_agent:
        # Half-answered is not a PASS: the key-file half held, the agent half was never
        # asked. Say which, so nobody reads it as "no agent identities".
        return Rule(rid, SKIP, f"no private key material under {ctx.ssh_dir}, but the "
                               f"{foreign_agent} — rerun in a session owned by that account "
                               f"for the agent half")
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


# --- rule 7b: the deployment is not readable by a PEER local account ---------------------------
# Added 2026-08-16, by drill 2, which found exactly this on the operator's own deployed guest
# house: `/Users/factory` was mode 750 group `staff`, and the tree inside it 755/644 — so the
# unrelated local account `agent` (in `staff`, like every macOS account by default) could read
# the deployment's blackboard, its config, and its whole tree.
#
# `home-dir-perms` already asks whether the home mode is right. This rule asks the question
# that mode is a proxy FOR — what a peer can actually read — and names the artifacts, because
# "mode 750" reads as a nit while "any local account can read your held-out corpus and your
# entire decision history" reads as what it is. The two are deliberately separate verdicts:
# a deployment can fix the consequence (tighten the artifacts) or the cause (tighten the home),
# and only the second makes the first irrelevant.
PEER_READABLE_ARTIFACTS = (
    ("store/blackboard.db", "the blackboard — every task, approval, learning and decision"),
    ("config.yaml", "the deployment's configuration, including its knobs and target"),
    ("corpus", "the scenario corpus, including the held-out partitions blindness rests on"),
    ("state", "runtime state (broker spool, mode flag, dashboard snapshot)"),
)


def _peer_can_traverse(path: str, stop_at: str) -> bool:
    """True iff every directory from `stop_at` down to `path`'s parent grants group OR other
    execute — i.e. a peer account can actually reach `path` at all. Without this the rule
    would report a 644 file as exposed even when a 700 ancestor makes it unreachable."""
    bits = stat.S_IXGRP | stat.S_IXOTH
    cur = os.path.dirname(os.path.abspath(path))
    stop_at = os.path.abspath(stop_at)
    while True:
        try:
            if not (os.stat(cur).st_mode & bits):
                return False
        except OSError:
            return False
        if cur == stop_at or cur == os.path.dirname(cur):
            return True
        cur = os.path.dirname(cur)


def rule_deployment_not_peer_readable(ctx: Ctx) -> Rule:
    rid = "deployment-not-peer-readable"
    if not ctx.is_posix:
        return Rule(rid, SKIP, "no POSIX permission model on this platform")
    root = ctx.factory_root or ""
    if not root or not os.path.isdir(root):
        return Rule(rid, SKIP, "no factory directory found")
    read_bits = stat.S_IRGRP | stat.S_IROTH
    try:
        home_mode = stat.S_IMODE(os.stat(ctx.home).st_mode)
    except OSError as e:
        return Rule(rid, SKIP, f"cannot stat {ctx.home}: {e}")
    if not (home_mode & (read_bits | stat.S_IXGRP | stat.S_IXOTH)):
        return Rule(rid, PASS,
                    f"{ctx.home} is mode {oct(home_mode)} — no peer account can enter the "
                    f"deployment, whatever the modes inside it are")
    exposed = []
    for rel, what in PEER_READABLE_ARTIFACTS:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            continue
        if (mode & read_bits) and _peer_can_traverse(path, ctx.home):
            exposed.append(f"{rel} (mode {oct(mode)}) — {what}")
    if exposed:
        return Rule(rid, FAIL,
                    f"{ctx.home} is mode {oct(home_mode)}, so any local account in its group "
                    f"(on macOS that is every account, via `staff`) can read: "
                    + "; ".join(exposed)
                    + f". Fix the cause: chmod 700 {ctx.home}")
    return Rule(rid, PASS,
                f"{ctx.home} is mode {oct(home_mode)} (peer-traversable), but no high-value "
                f"artifact under {root} is group/other-readable")


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


def _config_path(ctx: Ctx) -> str:
    """`ctx.config_path`, or the checkout's own config.yaml. Nothing defaults the field, so
    every consumer has to fall back the same way (see `rule_boundary_config`)."""
    if ctx.config_path:
        return ctx.config_path
    return os.path.join(ctx.factory_root, "config.yaml") if ctx.factory_root else ""


# --- rule 9: dashboard bound to localhost ------------------------------------------------------
def rule_dashboard_localhost(ctx: Ctx) -> Rule:
    rid = "dashboard-localhost"
    config_path = _config_path(ctx)
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



# --- rule 11: nothing sensitive left world-readable in the shared drop area ------------------
# /Users/Shared is world-readable AND world-writable (drwxrwxrwt) — it is the one place the
# install flow has to use to hand files between two accounts, and therefore the one place
# where "temporarily" world-readable artifacts accumulate. Found live on the reference
# deployment (2026-08-09): a 643 KB COMPLETE COPY of the blackboard (69 tasks, learnings,
# approvals) at mode 0644, plus credential-shaped drop files, months after they were
# consumed. Nothing else in this doctor would have caught it — every other rule looks at the
# account's own home.
_SENSITIVE_SHARED_NAMES = ("blackboard", "token", "pat", "secret", "session", "credential")


def _looks_sensitive(name: str) -> bool:
    low = name.lower()
    return low.endswith(".db") or any(t in low for t in _SENSITIVE_SHARED_NAMES)


def rule_shared_drop_hygiene(ctx: Ctx) -> Rule:
    rid = "shared-drop-hygiene"
    if not ctx.is_posix:
        return Rule(rid, SKIP, "no POSIX permission model on this platform")
    if not os.path.isdir(ctx.shared_dir):
        return Rule(rid, SKIP, f"{ctx.shared_dir} not present")
    exposed = []
    for root, dirs, files in os.walk(ctx.shared_dir):
        # Never descend into the bare transfer repo: its contents are the factory's own
        # source, which is a PUBLIC repo — readable there is not a disclosure.
        dirs[:] = [d for d in dirs
                   if os.path.join(root, d) != ctx.bare_repo_path and not d.startswith(".")]
        for name in files:
            if not _looks_sensitive(name):
                continue
            path = os.path.join(root, name)
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
            except OSError:
                continue
            if mode & 0o077:                      # any group/other bit at all
                exposed.append(f"{path} ({oct(mode)})")
        if len(exposed) >= 10:
            break
    if exposed:
        return Rule(rid, FAIL,
                    "world/group-readable sensitive file(s) in the shared drop area — "
                    "chmod 600 (or delete once consumed): " + "; ".join(exposed[:10]))
    return Rule(rid, PASS,
                f"no group/other-readable sensitive files under {ctx.shared_dir}")


RULES: List[Tuple[str, Callable[[Ctx], Rule]]] = [
    ("standard-user", rule_standard_user),
    ("no-passwordless-sudo", rule_no_sudo_grant),
    ("home-dir-perms", rule_home_dir_perms),
    ("no-ssh-access", rule_no_ssh_access),
    ("no-docker-socket", rule_no_docker_socket),
    ("runtime-read-only", rule_runtime_readonly),
    ("credentials-hygiene", rule_credentials_hygiene),
    ("deployment-not-peer-readable", rule_deployment_not_peer_readable),
    ("brakes-engaged", rule_brakes_engaged),
    ("dashboard-localhost", rule_dashboard_localhost),
    ("wsl-hardening", rule_wsl_hardening),
    ("shared-drop-hygiene", rule_shared_drop_hygiene),
]



# ==========================================================================================
# BOUNDARY PROBES (`--boundary`) — run AS THE GRADING IDENTITY, not as the factory user.
#
# Phase 3 (docs/plans/2026-08-09-worker-isolation-design.md) isolates every execution of
# candidate-authored code behind common/target_exec.py. That is a CLAIM until something
# stands in the grader's shoes and fails to reach the control plane. These rules are that
# something, and they invert the usual polarity: PASS means "I could NOT do this".
#
# They deliberately bypass the account-scoped context gate above. That gate recognizes only
# the `factory` account, so boundary rules run as a different identity would SKIP themselves
# and exit 0 — a proof that passes by not running, which is worse than no proof at all.
# ==========================================================================================

REFUSED, ABSENT, PRESENT = "refused", "absent", "present"


def _reachability(path: str) -> Tuple[str, str]:
    """(state, detail) — the three answers `os.path.exists()` collapses into one False.

    `exists()` is False both for "there is no such file" and for "you are not allowed to
    look", and to a boundary probe those are OPPOSITE results: the first proves nothing, the
    second IS the containment being measured. Every boundary rule here used to pre-check
    `exists()`, so on a correctly contained deployment — a 0700 guest-house home, which is
    what drill 2 fixed on 2026-08-16 — the grader's control-plane probes reported three
    FAILs ("does not exist (nothing proven)") and two SKIPs, and `--boundary`'s "every rule
    must pass" was unsatisfiable. Same class as the symlink probe's missing directory
    anchor: the rule reports its most negative verdict exactly where containment is
    tightest, because tight containment makes the path invisible rather than refused.

    `os.stat` separates them: ENOENT only when the parent could actually be read."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return ABSENT, f"{path} does not exist (nothing proven)"
    except PermissionError:
        return REFUSED, f"{path}: permission denied"
    except OSError as e:
        return REFUSED, f"{path}: {e}"
    return PRESENT, path


def _cannot_read(path: str) -> Tuple[bool, str]:
    """(unreadable, detail). A path this identity cannot even reach counts as containment;
    a path that is genuinely absent proves nothing and is reported as such, never as a
    PASS."""
    if not path:
        return False, "no path configured"
    state, detail = _reachability(path)
    if state != PRESENT:
        return state == REFUSED, detail
    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except PermissionError:
        return True, f"{path}: permission denied"
    except OSError as e:
        return True, f"{path}: {e}"
    return False, f"{path} IS READABLE from here"


def rule_boundary_blackboard(ctx: Ctx) -> Rule:
    rid = "boundary-blackboard"
    path = os.path.join(ctx.factory_root or "", "store", "blackboard.db")
    ok, detail = _cannot_read(path)
    return Rule(rid, PASS if ok else FAIL, detail)


def rule_boundary_config(ctx: Ctx) -> Rule:
    """Derives the config path from FACTORY_ROOT exactly like `rule_dashboard_localhost`
    does. It used to read `ctx.config_path` alone, which nothing defaults, so every real run
    reported `FAIL  no path configured` — a probe that cries breach because it was never
    told where to look trains an operator to skim past FAILs (found running drill 2)."""
    rid = "boundary-config"
    ok, detail = _cannot_read(_config_path(ctx))
    return Rule(rid, PASS if ok else FAIL, detail)


def rule_boundary_secrets(ctx: Ctx) -> Rule:
    rid = "boundary-secrets"
    ok, detail = _cannot_read(ctx.env_file_path or "")
    return Rule(rid, PASS if ok else FAIL, detail)


def rule_boundary_factory_root(ctx: Ctx) -> Rule:
    """Listing the tree at all. A grader that can read FACTORY_ROOT can read the held-out
    scenarios and the graders' own check modules — the blindness ARCHITECTURE claims."""
    rid = "boundary-factory-root"
    root = ctx.factory_root or ""
    if not root:
        return Rule(rid, SKIP, "(unset) not present")
    state, detail = _reachability(root)
    if state == ABSENT:
        return Rule(rid, SKIP, detail)
    if state == REFUSED:
        return Rule(rid, PASS, detail)         # cannot even stat it — see `_reachability`
    try:
        os.listdir(root)
    except PermissionError:
        return Rule(rid, PASS, f"{root}: permission denied")
    except OSError as e:
        return Rule(rid, PASS, f"{root}: {e}")
    return Rule(rid, FAIL, f"{root} IS LISTABLE from here")


def rule_boundary_killswitch(ctx: Ctx) -> Rule:
    """The single highest-value action an attacker takes. Checks UNLINK permission without
    ever removing it: deletion needs write on the CONTAINING directory, so that is what is
    tested — never `os.remove`, which would disarm the brake to prove it was armed."""
    rid = "boundary-killswitch"
    root = ctx.factory_root or ""
    stop = os.path.join(root, "STOP")
    state, detail = _reachability(stop)
    if state == REFUSED:
        # The strongest answer, not a missing one: an identity that cannot reach STOP cannot
        # unlink it, and whether it is engaged is not this probe's question. Reported as
        # "STOP not present — engage it" before the fix, sending the operator to arm a brake
        # that was already armed and invisible to them from that account.
        return Rule(rid, PASS, f"{detail} — it cannot be unlinked from here")
    if state == ABSENT:
        return Rule(rid, SKIP, "STOP not present — engage it, then re-run this probe")
    if os.access(root, os.W_OK):
        return Rule(rid, FAIL, f"{root} is writable from here — STOP can be unlinked")
    return Rule(rid, PASS, f"{stop} cannot be unlinked (its directory is not writable)")


def rule_boundary_dashboard_write(ctx: Ctx) -> Rule:
    """The channel an isolated process reaches for NEXT: the board's write routes. Before
    Phase 3's prerequisite these accepted any local POST, so a contained grader could simply
    ask the dashboard to approve or resume.

    THIS PROBE MUST NOT MUTATE. The first version posted to `/api/resume` — which, if the
    boundary is broken, SUCCEEDS and clears the killswitch. It did exactly that against a
    live board while this rule was being written: a safety probe that disarms the safety it
    is testing. It now posts a deliberately INVALID `/api/settings` payload, so the outcomes
    are: 403 (refused before the body is read — contained), 400 (validation reached, i.e.
    NOT contained, but nothing was written because the key is bogus), 200 (impossible for an
    unknown key, so treated as a failure). No branch changes any state."""
    rid = "boundary-dashboard-write"
    urls = [u for u in ([ctx.dashboard_settings_url] + list(ctx.extra_dashboard_urls or ()))
            if u]
    if not urls:
        return Rule(rid, SKIP, "no dashboard URL configured")
    # Label each board by its ORIGIN, not by the URL's path: the probe now decides which
    # route to use per board, and the detail carries the one that actually answered. Keeping
    # the assumed path in the label printed
    #   "…:9787/api/settings: /api/promote: refused …"
    # — naming the route that 404s on exactly the board that does not serve it, which is the
    # confusion this rule was just fixed to remove.
    verdicts = [(_origin(url), _probe_write_route(url)) for url in urls]
    bad = [f"{u}: {d}" for u, (s, d) in verdicts if s == FAIL]
    if bad:
        return Rule(rid, FAIL, "; ".join(bad))
    good = [f"{u}: {d}" for u, (s, d) in verdicts if s == PASS]
    if good:
        skipped = [f"{u}: {d}" for u, (s, d) in verdicts if s == SKIP]
        return Rule(rid, PASS, "; ".join(good + skipped))
    return Rule(rid, SKIP, "; ".join(f"{u}: {d}" for u, (s, d) in verdicts))


# The write routes worth probing, and why the SAME bogus payload is inert against each even
# with the gate wide open. The two boards serve DIFFERENT ones — `fleet_server` owns
# `/api/settings`, while `dashboard/server.py` has exactly one write action, `/api/promote`
# — so a probe that knows only the first learns nothing about the second.
#   /api/settings  — "__boundary_probe__" is not a known key, so validation rejects it.
#   /api/promote   — `do_promote` returns 400 for a payload with no `candidate_id`/`operator`
#                    BEFORE it opens the Blackboard, so no promotion can occur.
# Anything added here must be checked the same way: reaching validation must not mutate.
_WRITE_ROUTES = ("/api/settings", "/api/promote")


def _origin(url: str) -> str:
    """scheme://host:port — how a board is named in output, since the path is now per-board."""
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else url


def _probe_write_route(url: str) -> Tuple[str, str]:
    """One board's write gate. Deliberately INVALID payload — see the caller's docstring.

    Tries every known write route rather than one, because a 404 means "this board does not
    serve THAT route", never "this board has no write channel". On the deployment the
    config-derived board (`dashboard/server.py`) answered 404 to `/api/settings` and was
    reported as unprobed, while the row read PASS off the OTHER board — leaving
    `/api/promote`, a real state change, never asked about. Found running the drill against
    the live deployment, 2026-08-17; same shape as the hardcoded-port defect before it."""
    import urllib.error
    import urllib.request
    payload = json.dumps({"key": "__boundary_probe__", "value": "x"}).encode()
    # The caller's own path first: a test (or an operator) naming an explicit route must get
    # that route probed, not a guess.
    parsed = urllib.parse.urlparse(url)
    paths = [parsed.path] + [r for r in _WRITE_ROUTES if r != parsed.path]
    unreachable = None
    for path in paths:
        target = urllib.parse.urlunparse(parsed._replace(path=path))
        req = urllib.request.Request(target, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return FAIL, f"{path}: ACCEPTED an unauthenticated write ({resp.status})"
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return PASS, f"{path}: refused unauthenticated write ({e.code})"
            if e.code == 400:
                return FAIL, (f"{path}: reached VALIDATION unauthenticated (400) — the write "
                              "gate is open; nothing was written only because the payload "
                              "cannot validate")
            if e.code == 404:
                continue                      # not this board's route; try the next
            return FAIL, f"{path}: unexpected status {e.code}"
        except OSError as e:
            unreachable = e
            break
    if unreachable is not None:
        return SKIP, "not reachable (board not running?)"
    return SKIP, ("serves none of the known write routes ("
                  + ", ".join(paths) + ") — nothing proven about this board")


# ------------------------------------------------------------------------------------------
# DRILL-2 PROBES (2026-08-16). The six probes above ask one question — can I reach the
# CONTROL PLANE — which is drill 2's "host writes" class and nothing else. Drill 2
# (roadmap Part 5) names six: network, Keychain, process escape, symlinks, dependency
# substitution, host writes. The rules below cover the other five, plus the one that makes
# the network class mean anything (a credential worth exfiltrating).
#
# Every one is INERT — reads, `access()` checks, `signal 0`, a read-only `sudo -n -l`, a
# bare TCP connect that writes nothing, and one symlink inside a directory the probe makes
# itself. Nothing here creates, deletes, or modifies a path outside its own temp dir, which
# is what makes the suite safe to run against a LIVE deployment (the property the
# `/api/resume` incident above cost us).
#
# "Nothing proven" is reported as such, never as a PASS: a probe that passes because the
# path it looks for is absent is the same failure mode as a rule that skips itself.
# ------------------------------------------------------------------------------------------

def _writable(path: str) -> bool:
    """Effective-uid write test where the platform supports it (`faccessat`); plain
    `os.access` otherwise. Never writes — a create-then-unlink canary would be a probe that
    ACTS when the boundary is broken, exactly what the section header forbids."""
    try:
        return os.access(path, os.W_OK, effective_ids=(os.access in os.supports_effective_ids))
    except (TypeError, ValueError, OSError):
        return os.access(path, os.W_OK)


def _other_user_homes(ctx: Ctx) -> List[str]:
    """Home directories belonging to identities OTHER than the one probing. On the guest
    house this is how you ask "can I read the operator's account" — the deployment's whole
    perimeter claim (guest-house.md rules table, CANNOT #1)."""
    roots = ["/Users"] if ctx.platform_name == "Darwin" else ["/home"]
    skip = {"Shared", "Guest", "root", os.path.basename(ctx.home.rstrip("/")), ctx.username}
    homes = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if name.startswith(".") or name in skip:
                continue
            path = os.path.join(root, name)
            if os.path.isdir(path) and os.path.realpath(path) != os.path.realpath(ctx.home):
                homes.append(path)
    return homes


def rule_boundary_network_egress(ctx: Ctx) -> Rule:
    """NOT a containment claim, and deliberately never a FAIL: the deployment needs egress
    (GitHub, the model API, `claude` itself), and macOS gives no per-user egress filter. It
    is reported because drill 2 names it, and because what egress is WORTH is a different
    rule: `boundary-credential-reach` asks whether there is anything here worth exfiltrating.
    The probe opens a TCP connection and writes nothing at all."""
    rid = "boundary-network-egress"
    reachable, detail = ctx.tcp_probe_fn(ctx.egress_probe_target)
    return Rule(rid, SKIP,
                f"egress {'REACHABLE' if reachable else 'not reachable'} ({detail}) — not a "
                f"containment claim: the deployment needs the network. What bounds the damage "
                f"is what a process here can READ (boundary-secrets, boundary-credential-reach) "
                f"and the PAT's repo scope, not the socket.")


def rule_boundary_credential_reach(ctx: Ctx) -> Rule:
    """The question the network probe cannot answer: is there a usable publication credential
    within reach of this identity? Each check is read-only — `gh auth status` prints whether a
    token exists, never the token (no `--show-token`)."""
    rid = "boundary-credential-reach"
    found = []
    env_file = ctx.env_file_path or ""
    if env_file and os.path.exists(env_file):
        try:
            with open(env_file, "rb") as fh:
                if fh.read(1):
                    found.append(f"{env_file} is readable")
        except OSError:
            pass
    creds = ctx.git_credentials_path or ""
    if creds and os.path.exists(creds):
        try:
            with open(creds, "rb") as fh:
                if fh.read(1):
                    found.append(f"{creds} is readable")
        except OSError:
            pass
    try:
        r = ctx.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=15)
        blob = f"{getattr(r, 'stdout', '') or ''}{getattr(r, 'stderr', '') or ''}"
        if getattr(r, "returncode", 1) == 0 and "ogged in" in blob:
            # `.split()[-1]` grabbed the trailing credential-source note, so the live
            # deployment reported "a usable credential for (GH_TOKEN)" — the token's ORIGIN
            # where the HOST belongs. gh prints "Logged in to github.com account x
            # (GH_TOKEN)"; take the field that follows "to".
            hosts = sorted({m.group(1) for m in
                            (re.search(r"ogged in to\s+(\S+)", ln) for ln in blob.splitlines())
                            if m}) or ["(unnamed host)"]
            found.append(f"`gh` holds a usable credential for {', '.join(hosts)}")
    except (OSError, subprocess.SubprocessError):
        pass
    if found:
        return Rule(rid, FAIL, "; ".join(found)
                    + " — a process here can publish, or exfiltrate what publishes")
    return Rule(rid, PASS,
                "no readable env file, no git-credentials store, no logged-in `gh` — nothing "
                "here can publish (when armed, the broker holds the only push credential)")


def rule_boundary_keychain(ctx: Ctx) -> Rule:
    """guest-house.md rules table, CANNOT #1. Probes only whether ANOTHER identity's keychain
    is reachable — this identity's own keychain is explicitly allowed. Never dumps or unlocks
    anything: `security dump-keychain` would print secrets on success, which is the shape of
    probe this repo forbids."""
    rid = "boundary-keychain"
    if ctx.platform_name != "Darwin":
        return Rule(rid, SKIP, f"no macOS Keychain on {ctx.platform_name}")
    homes = _other_user_homes(ctx)
    if not homes:
        return Rule(rid, SKIP, "no other user's home on this machine (nothing proven)")
    reached, seen = [], 0
    for home in homes:
        kc = os.path.join(home, "Library", "Keychains")
        # os.path.isdir() is False both when the directory is absent AND when this identity
        # cannot traverse the home to see it. Only the SECOND is containment, so count what
        # was actually observable and refuse to call an empty search a PASS.
        if not os.path.exists(kc) and not os.access(home, os.X_OK):
            continue
        if not os.path.isdir(kc):
            continue
        seen += 1
        try:
            entries = os.listdir(kc)
        except OSError:
            continue
        reached.append(f"{kc} ({len(entries)} entr(y/ies) listable)")
    if reached:
        return Rule(rid, FAIL, "another identity's keychain is in reach: " + "; ".join(reached))
    if not seen:
        return Rule(rid, SKIP,
                    f"no other identity's keychain directory was visible at all "
                    f"({len(homes)} home(s) checked) — consistent with containment, but "
                    f"nothing proven: the directory may simply not exist")
    return Rule(rid, PASS,
                f"{seen} other keychain director(y/ies) exist and none is listable")


def rule_boundary_other_homes(ctx: Ctx) -> Rule:
    """The rest of CANNOT #1 — the operator's home itself, not just the keychain inside it."""
    rid = "boundary-other-homes"
    homes = _other_user_homes(ctx)
    if not homes:
        return Rule(rid, SKIP, "no other user's home on this machine (nothing proven)")
    listable = []
    for home in homes:
        try:
            os.listdir(home)
            listable.append(home)
        except OSError:
            continue
    if listable:
        return Rule(rid, FAIL, "another identity's home is listable: " + ", ".join(listable))
    return Rule(rid, PASS, f"no other identity's home is listable (checked {len(homes)})")


def rule_boundary_process_escape(ctx: Ctx) -> Rule:
    """Two escapes, one verdict: becoming another identity (`sudo`), and reaching into another
    identity's running processes. `sudo -n -l` never prompts and executes nothing; `signal 0`
    asks the kernel for permission without delivering a signal.

    A grading identity is ALLOWED exactly one grant — the root-owned `run-target-code`
    wrapper the factory user invokes — so a grant naming only that is a PASS, and anything
    else is not."""
    rid = "boundary-process-escape"
    problems, notes = [], []
    try:
        r = ctx.run(["sudo", "-n", "-l"], capture_output=True, text=True, timeout=15)
        if getattr(r, "returncode", 1) == 0:
            out = (getattr(r, "stdout", "") or "")
            grants = [ln.strip() for ln in out.splitlines()
                      if "NOPASSWD" in ln or ln.strip().startswith("(")]
            unexpected = [g for g in grants if ctx.grader_wrapper_path not in g]
            if not grants:
                problems.append("passwordless sudo is available (no explicit grant listed)")
            elif unexpected:
                problems.append("sudo grants beyond the grading wrapper: "
                                + " | ".join(unexpected)[:300])
            else:
                notes.append(f"exactly the grading grant ({ctx.grader_wrapper_path})")
        else:
            notes.append("no passwordless sudo")
    except (OSError, subprocess.SubprocessError) as e:
        notes.append(f"sudo unavailable ({e})")

    signalled = []
    for pid, owner in ctx.foreign_processes_fn(ctx):
        try:
            os.kill(pid, 0)                      # permission check; delivers nothing
            signalled.append(f"{pid} ({owner})")
        except (PermissionError, ProcessLookupError, OSError):
            continue
        if len(signalled) >= 3:
            break
    if signalled:
        problems.append("can signal processes owned by another identity: "
                        + ", ".join(signalled))
    elif not ctx.foreign_processes_fn(ctx):
        notes.append("no foreign-owned processes visible to compare against (nothing proven)")
    else:
        notes.append("cannot signal any foreign-owned process")
    if problems:
        return Rule(rid, FAIL, "; ".join(problems))
    return Rule(rid, PASS, "; ".join(notes) or "no escape route found")


def rule_boundary_symlink_escape(ctx: Ctx) -> Rule:
    """A symlink must not launder access: reading a refused path THROUGH a link the probing
    identity owns has to fail exactly as the direct read did. This is the class that already
    bit this phase once from the other direction — `chmod -R +a` followed candidate symlinks
    and stamped ACLs on their targets (worker-isolation.md, STATUS note), which is why the
    export ACL grant is a no-op today.

    Anchors on a path that is ALREADY refused from here; with no such path there is nothing
    to launder and the probe says so rather than passing."""
    rid = "boundary-symlink-escape"
    candidates = [ctx.env_file_path or "",
                  os.path.join(ctx.factory_root or "", "store", "blackboard.db"),
                  _config_path(ctx)]
    # Another identity's home, and the keychain inside it. The HOME ITSELF matters: on a
    # correctly closed deployment it is often the ONLY refused path this identity can see —
    # everything under it becomes invisible rather than refused — and without a directory
    # anchor the probe skipped itself exactly where containment was tightest (drill 2's
    # perimeter run: "nothing is refused to this identity in the first place", on an account
    # that could not enter the operator's home at all).
    for home in _other_user_homes(ctx):
        candidates.append(os.path.join(home, "Library", "Keychains"))
        candidates.append(home)
    refused, is_dir = "", False
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        if os.path.isdir(path):
            try:
                os.listdir(path)
            except OSError:
                refused, is_dir = path, True
                break
            continue
        ok, _ = _cannot_read(path)
        if ok:
            refused = path
            break
    if not refused:
        return Rule(rid, SKIP,
                    "no path is refused to this identity in the first place, so there is "
                    "nothing a symlink could launder (nothing proven — see the read probes)")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        link = os.path.join(tmp, "laundered")
        try:
            os.symlink(refused, link)
        except OSError as e:
            return Rule(rid, SKIP, f"cannot create a symlink to probe with ({e})")
        try:
            if is_dir:
                os.listdir(link)
            else:
                with open(link, "rb") as fh:
                    fh.read(1)
        except OSError as e:
            return Rule(rid, PASS,
                        f"{refused} stays refused through a symlink ({e.__class__.__name__})")
    return Rule(rid, FAIL, f"reading {refused} THROUGH a symlink succeeded — access laundered")


def rule_boundary_dependency_substitution(ctx: Ctx) -> Rule:
    """The quietest escape in drill 2's list: never touch the control plane directly, just
    drop a module where the factory's own interpreter will import it. Any WRITABLE directory
    on the import path is a full compromise of everything the factory later runs — no
    permission on the blackboard required, because the factory opens it for you.

    Checks the import path the factory actually resolves from (`python3 -m factory.*` runs
    from FACTORY_ROOT's parent) plus PYTHONPATH, site-packages and the user site dir, and
    flags an existing sitecustomize/usercustomize, which Python imports with no import
    statement anywhere."""
    rid = "boundary-dependency-substitution"
    root = ctx.factory_root or ""
    dirs = []
    if root:
        dirs += [os.path.dirname(root.rstrip("/")), root]
    dirs += [p for p in (ctx.environ.get("PYTHONPATH", "").split(os.pathsep)) if p]
    dirs += ctx.import_path_dirs_fn()
    seen, writable, hooks, refused = set(), [], [], []
    for d in dirs:
        d = os.path.abspath(d) if d else ""
        if not d or d in seen:
            continue
        # A refused directory dropped out of the count silently, so a contained grader was
        # told "N checked" by a probe that had skipped FACTORY_ROOT and its parent — the two
        # entries this rule exists for. Unreachable is the right verdict (nothing can be
        # dropped into a directory you cannot enter) but it has to be said out loud.
        if _reachability(d)[0] == REFUSED:
            seen.add(d)
            refused.append(d)
            continue
        if not os.path.isdir(d):
            continue
        seen.add(d)
        if _writable(d):
            writable.append(d)
        for hook in ("sitecustomize.py", "usercustomize.py"):
            p = os.path.join(d, hook)
            if os.path.isfile(p):
                hooks.append(p + (" (WRITABLE)" if _writable(p) else ""))
    if not seen:
        return Rule(rid, SKIP, "no import-path directory resolved (nothing proven)")
    if writable or hooks:
        detail = ""
        if writable:
            detail += ("writable on the factory's import path: " + ", ".join(writable[:6])
                       + (f" (+{len(writable) - 6} more)" if len(writable) > 6 else ""))
        if hooks:
            detail += ("; " if detail else "") + "auto-imported hook present: " + ", ".join(hooks)
        return Rule(rid, FAIL, detail + " — a module dropped here is imported by the factory itself")
    note = ""
    if refused:
        note = (f"; {len(refused)} unreachable from here (" + ", ".join(refused[:3])
                + (f" +{len(refused) - 3} more" if len(refused) > 3 else "")
                + ") — nothing can be dropped into a directory this identity cannot enter")
    return Rule(rid, PASS,
                f"no writable directory on the factory's import path ({len(seen)} checked), "
                f"no sitecustomize/usercustomize hook" + note)


def rule_boundary_host_writes(ctx: Ctx) -> Rule:
    """Persistence and authority paths OUTSIDE the factory tree: the root-owned grading
    wrapper (writable ⇒ the confinement is forgeable), machine- and other-user launchd
    directories (writable ⇒ code that outlives the drill), the operator-owned bare repo the
    deployment updates from, and the usual `/usr/local/bin` shim path."""
    rid = "boundary-host-writes"
    targets = list(ctx.host_write_paths)
    targets.append(ctx.grader_wrapper_path)
    for home in _other_user_homes(ctx):
        targets.append(os.path.join(home, "Library", "LaunchAgents"))
    checked, writable = 0, []
    for path in targets:
        if not path or not os.path.exists(path):
            continue
        checked += 1
        if _writable(path):
            writable.append(path)
    if not checked:
        return Rule(rid, SKIP, "none of the host paths exist on this machine (nothing proven)")
    if writable:
        return Rule(rid, FAIL, f"writable from here: {', '.join(writable)} "
                               f"({checked} host path(s) checked)")
    return Rule(rid, PASS, f"no host path is writable from here ({checked} checked)")


BOUNDARY_RULES: List[Tuple[str, Callable[[Ctx], Rule]]] = [
    ("boundary-blackboard", rule_boundary_blackboard),
    ("boundary-config", rule_boundary_config),
    ("boundary-secrets", rule_boundary_secrets),
    ("boundary-factory-root", rule_boundary_factory_root),
    ("boundary-killswitch", rule_boundary_killswitch),
    ("boundary-dashboard-write", rule_boundary_dashboard_write),
    # drill 2's remaining classes (2026-08-16)
    ("boundary-network-egress", rule_boundary_network_egress),
    ("boundary-credential-reach", rule_boundary_credential_reach),
    ("boundary-keychain", rule_boundary_keychain),
    ("boundary-other-homes", rule_boundary_other_homes),
    ("boundary-process-escape", rule_boundary_process_escape),
    ("boundary-symlink-escape", rule_boundary_symlink_escape),
    ("boundary-dependency-substitution", rule_boundary_dependency_substitution),
    ("boundary-host-writes", rule_boundary_host_writes),
]


def audit_boundary(ctx: Optional[Ctx] = None) -> AuditResult:
    """Every boundary probe, with NO context gate — see the section comment."""
    ctx = ctx or Ctx()
    return AuditResult(rules=[fn(ctx) for _, fn in BOUNDARY_RULES], deployed=True)


def boundary_banner(ctx: Optional[Ctx] = None) -> str:
    """Who is probing, and what a FAIL here means. Without this the table is ambiguous in
    the one way that matters: run AS THE GRADER, a FAIL is a broken boundary; run as the
    identity that owns the tree (an operator checking the probes themselves), the FAILs are
    the CORRECT answer — there is no boundary between you and your own files, and a probe
    suite that reported PASS in that situation would be the broken thing."""
    ctx = ctx or Ctx()
    root = ctx.factory_root or "(unset)"
    try:
        owner_uid = os.stat(root).st_uid
        try:
            import pwd
            owner = pwd.getpwuid(owner_uid).pw_name
        except Exception:  # noqa: BLE001
            owner = str(owner_uid)
    except OSError:
        owner = "(unknown)"
    self_probe = owner in (ctx.username, "(unknown)")
    lines = [
        "BOUNDARY PROBES — polarity is INVERTED: PASS means \"I could NOT do this\".",
        f"  probing as : {ctx.username}",
        f"  FACTORY_ROOT: {root} (owned by {owner})",
    ]
    if self_probe:
        lines.append(
            "  NOTE: you are probing as the identity that OWNS the tree, so most rules "
            "SHOULD fail — there is no boundary between an account and its own files. That "
            "is the negative control, not a defect. For the real proof run this as the "
            "grading identity: sudo -u factory-grader python3 scripts/guesthouse_check.py "
            "--boundary")
    else:
        lines.append("  every rule must PASS; a FAIL is a reachable control-plane path.")
    return "\n".join(lines)


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
    parser.add_argument("--factory-root", default=None,
                        help="the deployment to ask about (default: $FACTORY_ROOT, else the "
                             "tree this file lives in). Needed when running an installed "
                             "COPY of this doctor — e.g. the grading identity running "
                             "/opt/factory/guesthouse_check.py, which cannot read the "
                             "guest-house home the original lives in.")
    parser.add_argument("--boundary", action="store_true",
                        help="run the Phase 3 BOUNDARY probes instead: execute this AS THE "
                             "GRADING IDENTITY and every rule must report that it could NOT "
                             "reach the control plane. No context gate — a proof that skips "
                             "itself is worse than no proof.")
    args = parser.parse_args(argv)

    # None is the documented "no override" value — __post_init__ falls through to
    # _default_factory_root() ($FACTORY_ROOT, else this file's tree).
    ctx = Ctx(factory_root=args.factory_root)
    result = audit_boundary(ctx) if args.boundary else audit(ctx)
    mismatch = home_env_mismatch(ctx)
    if args.json:
        print(json.dumps({"deployed": result.deployed, "auditing": ctx.username,
                           "home": ctx.home, "home_env_mismatch": mismatch,
                           "rules": [r._asdict() for r in result.rules]}, indent=2))
    else:
        if mismatch:
            print(mismatch)
            print()
        if args.boundary:
            print(boundary_banner(ctx))
            print()
        elif not result.deployed:
            print(NON_GUEST_HOUSE_BANNER)
            print()
        print(render_table(result.rules))
    if not result.deployed:
        return 0
    return 1 if any(r.status == FAIL for r in result.rules) else 0


if __name__ == "__main__":
    sys.exit(main())
