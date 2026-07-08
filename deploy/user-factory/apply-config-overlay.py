#!/usr/bin/env python3
"""Local-only config overlay for the deploy branch.

The deployment under the dedicated `factory` macOS user needs FOUR values in config.yaml to
differ from the operator's own config (attended dev config lives on `main`; these are
committed only on the `deploy` branch — see 02-bootstrap-as-factory.sh step 4):

  autopilot.prod          true  -> false   (same-user IS the isolation boundary already —
                                             the whole factory already runs as its own OS
                                             user, so the Guest-House `agent` re-isolation
                                             the operator's box uses is redundant here)
  super_worker.user       "agent" -> ""    (workers run as `factory` itself, not a second
                                             Guest-House user — same reasoning as above)
  super_worker.claude_bin "/Users/agent/.local/bin/claude" -> "claude"  (this user's OWN
                                             claude, resolved via PATH, not the operator's
                                             Guest-House path)
  dashboard.port          8787  -> 9787    (avoid colliding with the operator's own board,
                                             which may be running on the same LAN/machine)

This is a COMMENT-PRESERVING, line-targeted patch — NOT a yaml.dump() round-trip, which would
reformat/reorder the whole file and blow away every explanatory comment config.yaml is full
of. Each substitution is BLOCK-SCOPED (matched only within its parent top-level key's lines,
not file-globally) so a coincidental same-named key elsewhere in the file can never be hit by
accident, and is required to match EXACTLY ONCE in that block — anything else (0 matches, or
more than 1) is treated as drift and fails loudly rather than guessing.

Idempotent: rerunning on an already-patched file is a no-op (each target value already being
present counts as success, not an error). Safe to call from update.sh after every merge.

Usage: apply-config-overlay.py <path-to-config.yaml>
"""
import re
import sys

import yaml

# Each entry: (top-level block key, field key within that block, old literal value token,
# new literal value token). The "value token" is compared verbatim against what appears
# after `field_key:` up to (but not including) trailing whitespace/comment — so for scalars
# this is exactly what a human would type: `true`, `"agent"`, `8787`, etc.
OVERLAY_TARGETS = [
    ("autopilot", "prod", "true", "false"),
    ("super_worker", "user", '"agent"', '""'),
    ("super_worker", "claude_bin", '"/Users/agent/.local/bin/claude"', '"claude"'),
    ("dashboard", "port", "8787", "9787"),
]

# After patching, config.yaml must parse and assert to exactly these effective values.
EXPECTED = {
    ("autopilot", "prod"): False,
    ("super_worker", "user"): "",
    ("super_worker", "claude_bin"): "claude",
    ("dashboard", "port"): 9787,
}

TOP_LEVEL_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+:\s*(#.*)?$")


def find_block(lines, block_key):
    """Return (start, end) line-index range [start, end) for the top-level `block_key:`
    section, where `start` is the header line itself and `end` is the index of the next
    top-level key (or len(lines) if `block_key` is the last section). None if not found."""
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
    return (start, end)


def patch_field(lines, block_key, field_key, old_value, new_value):
    """Patch `field_key: <value>` within the `block_key:` block, in place on `lines`.

    Returns "changed", "noop" (already at new_value), or raises SystemExit(1) on drift
    (anchor missing, ambiguous, or holding a third, unrecognized value)."""
    anchor = f"{block_key}.{field_key}"
    block = find_block(lines, block_key)
    if block is None:
        _die(anchor, f"top-level block '{block_key}:' not found in the file")
    start, end = block

    # Match against the line with its own trailing newline stripped off first — otherwise a
    # greedy `\s*` at the end of the pattern can swallow that newline into the "trailing"
    # group (since `\n` is whitespace), and re-appending `\n` on reconstruction then doubles
    # it into a spurious blank line. Stripping first makes `$` anchor unambiguously.
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
        _die(anchor, f"'{field_key}:' appears {len(candidates)} times inside the '{block_key}:' "
                      f"block (expected exactly once)")

    idx, m, has_nl = candidates[0]
    indent, gap, value, trailing = m.group(1), m.group(2), m.group(3), m.group(4)

    if value == new_value:
        return "noop"
    if value != old_value:
        _die(anchor, f"unexpected value {value!r} for '{field_key}:' "
                      f"(expected {old_value!r} or already-patched {new_value!r})")

    lines[idx] = f"{indent}{field_key}:{gap}{new_value}{trailing}" + ("\n" if has_nl else "")
    return "changed"


def _die(anchor, reason):
    targets = ", ".join(f"{k[0]}.{k[1]}={v!r}" for k, v in EXPECTED.items())
    print(f"ERROR: config.yaml anchor '{anchor}' drifted — {reason}.", file=sys.stderr)
    print(f"  apply the overlay by hand: {targets}", file=sys.stderr)
    sys.exit(1)


def main():
    if len(sys.argv) != 2:
        print("usage: apply-config-overlay.py <path-to-config.yaml>", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]

    with open(path, "r") as f:
        original_text = f.read()
    lines = original_text.splitlines(keepends=True)
    # splitlines(keepends=True) can drop a trailing empty "line" if the file doesn't end in a
    # newline; rejoin at the end from `lines` directly rather than re-deriving line count.

    results = {}
    for block_key, field_key, old_value, new_value in OVERLAY_TARGETS:
        anchor = f"{block_key}.{field_key}"
        status = patch_field(lines, block_key, field_key, old_value, new_value)
        results[anchor] = status
        verb = "already set" if status == "noop" else f"{old_value} -> {new_value}"
        print(f"[overlay] {anchor}: {verb}")

    patched_text = "".join(lines)
    changed = patched_text != original_text

    # Verify the patched text still parses AND resolves to exactly the expected values —
    # this is the actual correctness gate; the line-level patching above is just the how.
    try:
        doc = yaml.safe_load(patched_text)
    except yaml.YAMLError as e:
        print(f"ERROR: config.yaml no longer parses as YAML after patching: {e}", file=sys.stderr)
        sys.exit(1)

    for (block_key, field_key), expected_value in EXPECTED.items():
        actual = (doc.get(block_key) or {}).get(field_key)
        if actual != expected_value:
            targets = ", ".join(f"{k[0]}.{k[1]}={v!r}" for k, v in EXPECTED.items())
            print(f"ERROR: config.yaml anchors drifted — apply the overlay by hand: {targets}",
                  file=sys.stderr)
            print(f"  ({block_key}.{field_key} = {actual!r}, expected {expected_value!r})",
                  file=sys.stderr)
            sys.exit(1)

    if changed:
        with open(path, "w") as f:
            f.write(patched_text)
        print(f"[overlay] wrote {path} "
              f"({sum(1 for s in results.values() if s == 'changed')} changed, "
              f"{sum(1 for s in results.values() if s == 'noop')} already-applied)")
    else:
        print(f"[overlay] {path} already matches the deploy overlay — no changes written")


if __name__ == "__main__":
    main()
