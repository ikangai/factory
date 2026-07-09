#!/usr/bin/env python3
"""Per-instance config.yaml patcher for the single-line installer
(docs/plans/2026-07-09-single-line-installer-design.md).

Every `install.sh` instance is a FULL clone of this repo (bin/factory forces the clone to be
named `factory`, so two instances can never share a parent dir — see the design doc's "why one
parent dir per instance"). Each clone needs its OWN target.root/provider/base_branch, its own
non-colliding dashboard.port, and the same fresh-machine-safe super_worker/autopilot defaults
the dedicated-user kit uses (see deploy/user-factory/apply-config-overlay.py's docstring for
the reasoning — same-user IS the isolation boundary, so the Guest-House `agent` re-isolation is
redundant per instance and left OFF until the operator opts in by hand).

This is a COMMENT-PRESERVING, block-scoped, line-targeted patch — the SAME discipline as
apply-config-overlay.py (which stays untouched: it patches a fixed 4-literal old->new overlay
for the dedicated-user deploy branch; this one SETS parameterized values per instance). Each
field is matched only within its parent top-level key's lines (never file-globally), and is
required to match EXACTLY ONCE in that block — 0 or >1 matches is drift, and fails loudly
rather than guessing. After patching, the file is re-parsed with yaml and every effective
value is asserted against what was requested.

Usage (patch mode):
  configure_instance.py <path-to-config.yaml> --target-root ../<dir> --provider <p> \\
      --base-branch <b> --port <N|auto> --instances-root <dir>

Usage (list mode):
  configure_instance.py --list --instances-root <dir>
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import glob
import os
import re
import socket
import subprocess
import sys

import yaml

# Same boundary regex as apply-config-overlay.py: a bare `key:` (optionally trailing-commented)
# starting at column 0 marks the start of the NEXT top-level block, so `find_block` never reads
# past the block it was asked for.
TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+:\s*(#.*)?$")

DEFAULT_PORT_BASE = 8787
PORT_STEP = 10
MAX_PORT_PROBES = 1000


def find_block(lines: list[str], block_key: str) -> tuple[int, int] | None:
    """Return (start, end) line-index range [start, end) for the top-level `block_key:`
    section (`start` is the header line itself, `end` the next top-level key or EOF)."""
    header_re = re.compile(rf"^{re.escape(block_key)}:\s*(#.*)?$")
    start = None
    for i, line in enumerate(lines):
        if header_re.match(line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if TOP_LEVEL_KEY_RE.match(lines[j]):
            end = j
            break
    return start, end


def _die(anchor: str, reason: str) -> None:
    print(f"ERROR: config.yaml anchor '{anchor}' drifted — {reason}.", file=sys.stderr)
    sys.exit(1)


def set_field(lines: list[str], block_key: str, field_key: str, new_value: str) -> str:
    """Set `field_key: <new_value>` within the `block_key:` block, in place on `lines`.

    Returns "changed" or "noop" (already at new_value); raises SystemExit(1) on drift (block
    missing, or the field matches zero or more-than-one times inside it)."""
    anchor = f"{block_key}.{field_key}"
    block = find_block(lines, block_key)
    if block is None:
        _die(anchor, f"top-level block '{block_key}:' not found in the file")
    start, end = block

    # Strip the trailing newline before matching so a greedy trailing `\s*` can't swallow it
    # into the "trailing" group — re-appending `\n` on reconstruction would then double it into
    # a spurious blank line (same subtlety apply-config-overlay.py works around).
    field_re = re.compile(rf"^(\s*){re.escape(field_key)}:(\s*)(.*?)(\s*(?:#.*)?)$")
    candidates = []
    for i in range(start + 1, end):
        raw = lines[i]
        has_nl = raw.endswith("\n")
        content = raw[:-1] if has_nl else raw
        m = field_re.match(content)
        if m:
            candidates.append((i, m, has_nl))

    if len(candidates) == 0:
        _die(anchor, f"no '{field_key}:' line found inside the '{block_key}:' block")
    if len(candidates) > 1:
        _die(anchor, f"'{field_key}:' appears {len(candidates)} times inside the "
                      f"'{block_key}:' block (expected exactly once)")

    idx, m, has_nl = candidates[0]
    indent, gap, value, trailing = m.group(1), m.group(2), m.group(3), m.group(4)
    if value == new_value:
        return "noop"
    lines[idx] = f"{indent}{field_key}:{gap}{new_value}{trailing}" + ("\n" if has_nl else "")
    return "changed"


def _quote(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def assert_effective(doc: dict, expected: dict[tuple[str, str], object]) -> None:
    """The actual correctness gate: the line-level patching above is just the how. Raises
    SystemExit(1) (never a silent mismatch) if the re-parsed yaml disagrees with what was
    requested for ANY anchor."""
    for (block_key, field_key), exp in expected.items():
        actual = (doc.get(block_key) or {}).get(field_key)
        if actual != exp:
            names = ", ".join(f"{k[0]}.{k[1]}={v!r}" for k, v in expected.items())
            print(f"ERROR: config.yaml drifted after patching — {block_key}.{field_key} = "
                  f"{actual!r}, expected {exp!r}. Full expected set: {names}", file=sys.stderr)
            sys.exit(1)


# --- port assignment ---------------------------------------------------------------------
def _instance_configs(instances_root: str) -> list[str]:
    """Every instance's config.yaml under instances_root — the ONE definition of the
    installer's forced layout (<root>/<name>/factory/config.yaml, because bin/factory
    requires the clone dir be named `factory`). --list and the sibling-port scan must
    always agree on what counts as an instance, so both call this."""
    return sorted(glob.glob(os.path.join(instances_root, "*", "factory", "config.yaml")))


@contextlib.contextmanager
def _port_lock(instances_root: str):
    """Serialize collect-siblings -> resolve -> write across CONCURRENT installs on this
    host (two simultaneous `install.sh` runs would otherwise both scan before either
    writes, and both walk to the same free pair). flock is advisory and same-host only —
    exactly the scope of the collision it prevents."""
    path = os.path.join(instances_root, ".ports.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def collect_sibling_ports(instances_root: str, exclude_path: str) -> set[int]:
    """dashboard.port from every OTHER instance's config under instances_root (an instance =
    <instances_root>/*/factory/config.yaml, matching how bin/factory forces the clone dir
    name). `exclude_path` is compared by resolved path so the config being patched never
    counts as its own sibling. A sibling that fails to parse is skipped, not fatal — a
    neighboring instance's drift is that instance's problem, not this patch's."""
    ports: set[int] = set()
    for cfg_path in _instance_configs(instances_root):
        if os.path.realpath(cfg_path) == exclude_path:
            continue
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            continue
        port = (doc.get("dashboard") or {}).get("port")
        if isinstance(port, int):
            ports.add(port)
    return ports


def resolve_port(port_arg: str, current_port: int | None, sibling_ports: set[int],
                 port_base: int = DEFAULT_PORT_BASE) -> int:
    """Three modes, chosen by the CALLER because only it knows install-vs-update:

    - explicit <N>: used verbatim; warns (never fails) when N or N+1 collides with a
      sibling's dashboard or fleet port.
    - "auto" (fresh install): ALWAYS probes — the config's current value is the repo
      default, which says nothing about what's free on this machine, so every candidate
      pair gets a real bind test plus the sibling check.
    - "keep" (re-run/update, passed by install.sh): keep the previously-assigned port
      unless it collides with a sibling, with NO bind test — a live dashboard legitimately
      holds its own port, and a bind test cannot tell "my own board" from "someone else's
      process", so testing here would reassign a running instance out from under itself.

    The taken-set counts every sibling's dashboard port AND its implied fleet port
    (dashboard+1 — the `viz --serve` convention), in both directions: our pair (p, p+1)
    must avoid both."""
    taken = set(sibling_ports) | {p + 1 for p in sibling_ports}
    if port_arg not in ("auto", "keep"):
        p = int(port_arg)
        if p in taken or (p + 1) in taken:
            print(f"WARNING: --port {p} (or its fleet port {p + 1}) collides with a sibling "
                  f"instance's dashboard/fleet port — proceeding anyway (explicit port always "
                  f"wins)", file=sys.stderr)
        return p

    if (port_arg == "keep" and current_port is not None
            and current_port not in taken and (current_port + 1) not in taken):
        return current_port

    p = port_base
    for _ in range(MAX_PORT_PROBES):
        if (p not in taken and (p + 1) not in taken
                and _port_free(p) and _port_free(p + 1)):
            return p
        p += PORT_STEP
    print(f"ERROR: no free dashboard-port pair found starting at {port_base} "
          f"(probed {MAX_PORT_PROBES} candidates)", file=sys.stderr)
    sys.exit(1)


# --- patch mode --------------------------------------------------------------------------
def patch_config(config_path: str, target_root: str, provider: str, base_branch: str,
                  port_arg: str, instances_root: str,
                  port_base: int = DEFAULT_PORT_BASE) -> int:
    config_path = os.path.abspath(config_path)
    with open(config_path, "r", encoding="utf-8") as fh:
        original_text = fh.read()
    lines = original_text.splitlines(keepends=True)

    try:
        doc_before = yaml.safe_load(original_text) or {}
    except yaml.YAMLError:
        doc_before = {}
    current_port = (doc_before.get("dashboard") or {}).get("port")
    if not isinstance(current_port, int):
        current_port = None

    # The lock spans collect -> resolve -> write: concurrent installs must not both scan
    # siblings before either has written its claim.
    with _port_lock(instances_root):
        sibling_ports = collect_sibling_ports(instances_root, os.path.realpath(config_path))
        assigned_port = resolve_port(port_arg, current_port, sibling_ports, port_base)
        return _write_patch(config_path, lines, target_root, provider, base_branch,
                            assigned_port)


def _write_patch(config_path: str, lines: list[str], target_root: str, provider: str,
                 base_branch: str, assigned_port: int) -> int:
    # Always also set the safe-machine defaults (fresh machines have no Guest-House `agent`
    # user; the operator's own box re-enables consciously — see the module docstring).
    ops = [
        ("target", "root", _quote(target_root)),
        ("target", "provider", _quote(provider)),
        ("target", "base_branch", _quote(base_branch)),
        ("dashboard", "port", str(assigned_port)),
        ("autopilot", "prod", "false"),
        ("super_worker", "user", '""'),
        ("super_worker", "claude_bin", _quote("claude")),
    ]
    changed_any = False
    for block_key, field_key, value in ops:
        status = set_field(lines, block_key, field_key, value)
        changed_any = changed_any or status == "changed"
        print(f"[configure] {block_key}.{field_key}: {status}", file=sys.stderr)

    patched_text = "".join(lines)

    try:
        doc = yaml.safe_load(patched_text)
    except yaml.YAMLError as e:
        print(f"ERROR: config.yaml no longer parses as YAML after patching: {e}", file=sys.stderr)
        sys.exit(1)

    # DERIVED from ops, never a second hand-maintained list: parsing each written value
    # token yields exactly the typed value the patched file must read back, so a field
    # added to ops is automatically asserted (a separate literal dict here would let an
    # 8th field be set but silently never verified).
    expected = {(block_key, field_key): yaml.safe_load(value)
                for block_key, field_key, value in ops}
    assert_effective(doc, expected)

    if changed_any:
        with open(config_path, "w", encoding="utf-8") as fh:
            fh.write(patched_text)
        print(f"[configure] wrote {config_path}", file=sys.stderr)
    else:
        print(f"[configure] {config_path} already matches — no changes written", file=sys.stderr)

    # The ONE machine-readable stdout line — install.sh captures it. Everything else above is
    # diagnostic and deliberately goes to stderr so this line is never ambiguous to a caller.
    print(f"PORT={assigned_port}")
    return assigned_port


# --- list mode -----------------------------------------------------------------------------
def _target_origin_url(factory_dir: str, target_root: str) -> str:
    """The target's upstream URL — the one identity `list` exists to surface (two forks can
    share the basename `clive`; target.root alone can't tell them apart). Failure-tolerant:
    a missing/detached target is that instance's problem, shown as '?'."""
    target_dir = os.path.normpath(os.path.join(factory_dir, target_root))
    try:
        out = subprocess.run(["git", "-C", target_dir, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        return "?"


def list_instances(instances_root: str) -> None:
    matches = _instance_configs(instances_root)
    if not matches:
        print(f"no instances under {instances_root}")
        return
    for cfg_path in matches:
        factory_dir = os.path.dirname(cfg_path)
        instance_dir = os.path.dirname(factory_dir)
        name = os.path.basename(instance_dir)
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            doc = {}
        target = doc.get("target") or {}
        root = target.get("root", "?")
        provider = target.get("provider", "?")
        port = (doc.get("dashboard") or {}).get("port", "?")
        fleet_port = port + 1 if isinstance(port, int) else "?"
        mode_path = os.path.join(factory_dir, ".factory-mode")
        mode = "-"
        if os.path.isfile(mode_path):
            with open(mode_path, "r", encoding="utf-8") as fh:
                mode = fh.read().strip() or "-"
        target_url = _target_origin_url(factory_dir, root) if root != "?" else "?"
        print(f"{name}  root={root}  target={target_url}  provider={provider}  "
              f"port={port}  fleet_port={fleet_port}  mode={mode}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Comment-preserving, block-scoped per-instance config.yaml patcher "
                     "(same discipline as deploy/user-factory/apply-config-overlay.py).")
    ap.add_argument("config_path", nargs="?", default=None,
                     help="path to the instance's config.yaml (patch mode)")
    ap.add_argument("--list", action="store_true", help="list instances under --instances-root")
    ap.add_argument("--target-root")
    ap.add_argument("--provider")
    ap.add_argument("--base-branch")
    ap.add_argument("--port", default="auto",
                     help="dashboard port: a number, 'auto' (fresh install: probe), or "
                          "'keep' (update: keep the assigned port unless it collides)")
    ap.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE,
                     help="first candidate pair for probing (tests use an uncommon base "
                          "so literal port assertions stay hermetic)")
    ap.add_argument("--instances-root", required=True)
    args = ap.parse_args(argv)

    if args.list:
        list_instances(args.instances_root)
        return 0

    missing = [flag for flag, val in [
        ("config_path", args.config_path),
        ("--target-root", args.target_root),
        ("--provider", args.provider),
        ("--base-branch", args.base_branch),
    ] if not val]
    if missing:
        print(f"ERROR: patch mode requires {', '.join(missing)}", file=sys.stderr)
        return 2

    patch_config(args.config_path, args.target_root, args.provider, args.base_branch,
                 args.port, args.instances_root, args.port_base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
