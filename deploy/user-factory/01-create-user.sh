#!/bin/bash
# Provisions the dedicated macOS Standard user `factory` that the whole factory (conductor +
# workers + dashboards) will run under, isolated from the operator's own account/credentials.
# Code reaches that user WITHOUT a network hop: this pushes the operator's local repo into a
# shared bare repo (/Users/Shared/factory.git) that the factory user clones from in step 02.
#
# run: sudo bash deploy/user-factory/01-create-user.sh
set -euo pipefail

FUSER=factory
FHOME=/Users/factory
BARE=/Users/Shared/factory.git
OPERATOR_REPO="$(cd "$(dirname "$0")/../.." && pwd)"

# --- 0. must be root (creates a macOS user, writes under /Users/Shared) -----------------
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: this script creates a macOS user and writes to /Users/Shared — it must run as root." >&2
    echo "  run: sudo bash deploy/user-factory/01-create-user.sh" >&2
    exit 1
fi

echo "== user-factory provisioning: operator repo = $OPERATOR_REPO =="

# --- 1. create the Standard user (idempotent: skip if it already exists) ----------------
if ! id "$FUSER" >/dev/null 2>&1; then
    echo "[1/7] creating standard user '$FUSER' ..."
    # NOTE: sysadminctl takes -password as a plain argv value, so it is briefly visible to
    # anyone running `ps` on this machine during the call. Acceptable on a single-operator
    # box; on a shared machine, create the user some other way and skip this prompt.
    read -rs -p "Set a login password for the new '$FUSER' user: " FPW
    echo
    # Standard user (NO -admin) — the whole point is an OS-enforced boundary around Bash.
    sysadminctl -addUser "$FUSER" -fullName "Code Factory" -home "$FHOME" -password "$FPW"
    unset FPW
    echo "  created '$FUSER' as a Standard user (no admin rights)."
    echo "  optional, LATER: hide it from the login window with:"
    echo "    dscl . create /Users/$FUSER IsHidden 1"
    echo "  (leave it visible for now — fast-user-switch into it once for 'claude login' in runbook §3)"
else
    echo "[1/7] user '$FUSER' already exists — skipping creation"
fi

# --- 2. ensure the home directory exists -------------------------------------------------
if [ ! -d "$FHOME" ]; then
    echo "[2/7] creating home directory $FHOME ..."
    createhomedir -c -u "$FUSER"
else
    echo "[2/7] home directory $FHOME already exists — skipping"
fi

# --- 3. shared bare repo: the operator pushes here, the factory user clones from here ----
INVOKER="${SUDO_USER:-$(stat -f%Su "$OPERATOR_REPO")}"
if [ ! -d "$BARE" ]; then
    echo "[3/7] initializing bare repo $BARE ..."
    git init --bare "$BARE"
else
    echo "[3/7] bare repo $BARE already exists — skipping init"
fi
echo "  setting ownership: '$INVOKER' owns (writes), '$FUSER' reads via the 'staff' group ..."
# The OPERATOR owns the bare repo (they push into it); the factory user only READS it
# (clone/fetch) via the shared 'staff' group. Root must not own it — step 4 pushes as
# the operator and needs write.
chown -R "$INVOKER":staff "$BARE"
chmod -R g+rX "$BARE"

# --- 4. push this repo into the bare repo, run as the invoking (non-root) operator user --
echo "[4/7] pushing $OPERATOR_REPO -> $BARE ..."
if sudo -u "$INVOKER" git -C "$OPERATOR_REPO" remote get-url deploy >/dev/null 2>&1; then
    sudo -u "$INVOKER" git -C "$OPERATOR_REPO" remote set-url deploy "$BARE"
else
    sudo -u "$INVOKER" git -C "$OPERATOR_REPO" remote add deploy "$BARE"
fi
sudo -u "$INVOKER" git -C "$OPERATOR_REPO" push deploy main

# --- 5. seed drop point: where an operator-side blackboard snapshot lands for step 02 ----
echo "[5/7] preparing the seed drop point /Users/Shared/factory-seed ..."
install -d -m 755 /Users/Shared/factory-seed
echo "  to carry over existing learnings/history into the deployment, run AS THE OPERATOR:"
echo "    bash scripts/backup_blackboard.sh"
echo "    cp \"\$(ls -t ~/factory-db-backups/blackboard-*.db | head -1)\" /Users/Shared/factory-seed/blackboard.db"
echo "  (optional — 02-bootstrap-as-factory.sh starts with an empty blackboard if you skip this)"

# --- 6. stage the deploy kit somewhere the factory user can read without operator access -
echo "[6/7] staging the bootstrap kit at /Users/Shared/factory-kit ..."
install -d -m 755 /Users/Shared/factory-kit
cp "$OPERATOR_REPO"/deploy/user-factory/*.sh "$OPERATOR_REPO"/deploy/user-factory/*.py /Users/Shared/factory-kit/
chmod 755 /Users/Shared/factory-kit/*

# --- 7. next steps -------------------------------------------------------------------------
echo "[7/7] done."
cat <<EOF

============================================================
 '$FUSER' user provisioned. Next steps:
============================================================
 1. Mint a fine-grained GitHub PAT for ikangai/clive — runbook §2:
      docs/runbooks/factory-user-deployment.md
 2. Bootstrap the factory user's side of the deployment (run AS '$FUSER'):
      sudo -u $FUSER -i bash /Users/Shared/factory-kit/02-bootstrap-as-factory.sh /Users/Shared/factory-seed/blackboard.db
 3. Optional, since this box should stay always-on:
      sudo pmset -c sleep 0
    (the LaunchDaemon wrapper (with-env.sh) also runs everything under
    'caffeinate -i -s', which holds off idle sleep only while a server/backup
    job is actively running — pmset covers the gaps between runs)
============================================================
EOF
