#!/bin/bash
# Provisions the GRADING identity — the account that runs code the factory did not write.
#
# WHY (docs/plans/2026-08-09-worker-isolation-design.md): the factory executes
# worker-authored code on every candidate — the test suite, the red-proof nodes, the
# acceptance test, and the post-merge re-baseline. `super_worker.require_test: true`
# MANDATES the worker ship a test, and the frozen gate never covers `tests/`. Without this
# account that code runs as the factory user, with the blackboard, the credentials and the
# killswitch in reach; a reviewer proved a candidate conftest.py writing the store.
#
# Unlike the developer worker, the grader needs NO `claude` install and NO Claude login —
# it only runs the target's own test command. That removes the entire GUI/OAuth
# provisioning burden and is why this script is short.
#
# run: sudo bash deploy/user-factory/05-create-grader-user.sh [--uninstall]
set -euo pipefail

GUSER="${FACTORY_GRADER_USER:-factory-grader}"
GHOME="/Users/$GUSER"
FUSER="${FACTORY_USER:-factory}"
WRAPPER_DEST="/opt/factory/run-target-code"
SUDOERS="/etc/sudoers.d/factory-grader"
EXPORT_ROOT="${FACTORY_EXPORT_ROOT:-/tmp/factory-grade}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "${1:-}" = "--uninstall" ]; then
    echo "== removing the grading identity =="
    rm -f "$SUDOERS" "$WRAPPER_DEST"
    echo "  removed $SUDOERS and $WRAPPER_DEST (the grant is gone; grading falls back to"
    echo "  the factory user the moment super_worker.grader_user is cleared)"
    echo "  the '$GUSER' account and $EXPORT_ROOT were left in place — remove by hand if"
    echo "  you want them gone: sudo sysadminctl -deleteUser $GUSER"
    exit 0
fi

[ "$EUID" -eq 0 ] || { echo "ERROR: must run as root (creates a user, writes /etc/sudoers.d)." >&2; exit 1; }
id "$FUSER" >/dev/null 2>&1 || { echo "ERROR: factory user '$FUSER' does not exist — run 01-create-user.sh first." >&2; exit 1; }

# --- 1. the account ----------------------------------------------------------------------
if id "$GUSER" >/dev/null 2>&1; then
    echo "[1/5] user '$GUSER' already exists — skipping"
else
    echo "[1/5] creating '$GUSER' (Standard, no admin, no login needed) ..."
    # No -admin: this account exists to be powerless. A random password it never uses —
    # nobody logs in as the grader; the factory reaches it only through the pinned sudo rule.
    sysadminctl -addUser "$GUSER" -fullName "Factory Grader" -home "$GHOME" \
                -password "$(openssl rand -base64 32)" >/dev/null 2>&1
    createhomedir -c -u "$GUSER" >/dev/null 2>&1 || true
    dscl . create "/Users/$GUSER" IsHidden 1 || true      # keep it off the login window
fi
chmod 700 "$GHOME" 2>/dev/null || true

# --- 2. the wrapper: the ONLY thing the grant allows -------------------------------------
echo "[2/5] installing the wrapper at $WRAPPER_DEST ..."
install -d -m 755 -o root -g wheel /opt/factory
install -m 755 -o root -g wheel "$HERE/run-target-code" "$WRAPPER_DEST"
# root-owned and not writable by the factory user ON PURPOSE: if the caller could edit the
# wrapper, the pinned grant would be worthless — it would run whatever the caller wrote.

# --- 3. the grant: user-to-user, never root ----------------------------------------------
echo "[3/5] writing $SUDOERS ..."
TMP_SUDO="$(mktemp)"
cat > "$TMP_SUDO" <<EOF
# Factory grading isolation (Phase 3). The factory user may run ONE command as the
# grading identity, and nothing else. Deliberately NOT a root grant: an earlier design
# needed 'sudo (root) chown -R <user> *', a privilege-escalation primitive handed to the
# same account the conductor runs as.
$FUSER ALL=($GUSER) NOPASSWD: $WRAPPER_DEST
EOF
visudo -cf "$TMP_SUDO" >/dev/null || { echo "ERROR: refusing to install a sudoers file that does not validate" >&2; rm -f "$TMP_SUDO"; exit 1; }
install -m 440 -o root -g wheel "$TMP_SUDO" "$SUDOERS"
rm -f "$TMP_SUDO"

# --- 4. the export root ------------------------------------------------------------------
echo "[4/5] export root $EXPORT_ROOT ..."
install -d -m 711 -o "$FUSER" "$EXPORT_ROOT"
# 711 = traverse-only: the grader enters its OWN export (granted per-export by an ACL the
# factory applies) and cannot list what else is being graded.

# --- 5. verify ---------------------------------------------------------------------------
echo "[5/5] verifying the grant ..."
if sudo -n -u "$GUSER" "$WRAPPER_DEST" "$EXPORT_ROOT" /usr/bin/true 2>/dev/null; then
    echo "  OK: '$FUSER' can run the wrapper as '$GUSER' without a password"
else
    echo "  NOTE: could not verify from THIS shell (you are root, not '$FUSER')."
    echo "  Verify as the factory user:"
    echo "    sudo -u $FUSER sudo -n -u $GUSER $WRAPPER_DEST $EXPORT_ROOT /usr/bin/true"
fi

cat <<EOF

============================================================
 Grading identity ready.

 ARM IT — set in the factory's own config.yaml (on the branch it runs):
   super_worker:
     grader_user: "$GUSER"

 THEN PROVE IT, as the grader — every rule must report it could NOT reach
 the control plane (docs/runbooks/worker-isolation.md):
   sudo -u $GUSER python3 <factory>/scripts/guesthouse_check.py --boundary

 Isolation stays OFF until grader_user is set. Turning it on without this
 script installed makes every grading run fail loudly (no sudo grant) —
 deliberately: a missing grant must never fall back to running untrusted
 code as the factory user.
============================================================
EOF
