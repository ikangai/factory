#!/bin/bash
# Installs (or reinstalls) the three always-on LaunchDaemons for the factory deployment:
# board (9787), fleet/autopilot-supervisor (9788), and the 3h blackboard backup.
# Idempotent — safe to rerun after every deploy/update.sh to pick up plist changes; each
# daemon is booted out first if already loaded, so this also doubles as a "reload" command.
#
# run: sudo bash deploy/user-factory/03-install-daemons.sh [--uninstall]
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: installing LaunchDaemons requires root." >&2
    echo "  run: sudo bash deploy/user-factory/03-install-daemons.sh" >&2
    exit 1
fi

KIT="$(cd "$(dirname "$0")" && pwd)"
LABELS=(com.factory.board com.factory.fleet com.factory.backup)
MODE="install"
if [ "${1:-}" = "--uninstall" ]; then
    MODE="uninstall"
fi

echo "== ${MODE} LaunchDaemons from $KIT =="

for LABEL in "${LABELS[@]}"; do
    PLIST="$KIT/$LABEL.plist"
    DEST="/Library/LaunchDaemons/$LABEL.plist"

    if launchctl print "system/$LABEL" >/dev/null 2>&1; then
        echo "  $LABEL is loaded — booting out ..."
        launchctl bootout "system/$LABEL" || true
    else
        echo "  $LABEL is not currently loaded"
    fi

    if [ "$MODE" = "uninstall" ]; then
        rm -f "$DEST"
        echo "  removed $DEST"
        continue
    fi

    if [ ! -f "$PLIST" ]; then
        echo "ERROR: missing $PLIST — is this kit complete?" >&2
        exit 1
    fi
    cp "$PLIST" "$DEST"
    chown root:wheel "$DEST"
    chmod 644 "$DEST"
    launchctl bootstrap system "$DEST"
    echo "  installed + bootstrapped $LABEL"
done

if [ "$MODE" = "uninstall" ]; then
    cat <<'EOF'

============================================================
 Daemons uninstalled. To also remove the deployment itself, see
 runbook §8 (Teardown/rollback).
============================================================
EOF
    exit 0
fi

cat <<'EOF'

============================================================
 Daemons installed. Verify:
   curl -sS http://127.0.0.1:9787/ | head -1     # board
   curl -sS http://127.0.0.1:9788/ | head -1     # fleet / mission control
   tail -f /Users/factory/fab/factory/logs/daemon-board.log
   tail -f /Users/factory/fab/factory/logs/daemon-fleet.log
   tail -f /Users/factory/fab/factory/logs/daemon-backup.log
 Reload after a plist change: rerun this script (it boots out + re-bootstraps each daemon).
 Uninstall: sudo bash deploy/user-factory/03-install-daemons.sh --uninstall
============================================================
EOF
