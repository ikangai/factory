#!/usr/bin/env bash
# WAL-safe rolling backup of the factory blackboard — the single source of truth (tasks,
# shifts, learnings, budgets, EVM baseline). The live DB is gitignored, has no other copy, and
# sits under ~/Documents (an iCloud-synced tree), so one bad sync or an errant `git clean -fdx`
# loses ALL factory state unrecoverably. Schedule this (launchd/cron) to keep timestamped
# snapshots OUTSIDE the synced dir. Uses sqlite `.backup` (consistent across the -wal/-shm set),
# NOT cp (which can catch a torn WAL).
#
#   Schedule (every 6h) via launchd:  launchctl load ~/Library/LaunchAgents/com.harness-factory.backup.plist
#   Override dest/retention:          FACTORY_BACKUP_DIR=/path FACTORY_BACKUP_KEEP=50 scripts/backup_blackboard.sh
set -euo pipefail

FACTORY_DIR="${FACTORY_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DB="$FACTORY_DIR/store/blackboard.db"
DEST="${FACTORY_BACKUP_DIR:-$HOME/factory-db-backups}"
KEEP="${FACTORY_BACKUP_KEEP:-30}"

[ -f "$DB" ] || { echo "no blackboard at $DB" >&2; exit 1; }
mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/blackboard-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"
if [ "$(sqlite3 "$OUT" 'PRAGMA integrity_check;')" != "ok" ]; then
    echo "integrity check FAILED for $OUT" >&2
    rm -f "$OUT"
    exit 1
fi

# Prune: keep the newest $KEEP snapshots (drop the -wal/-shm siblings a snapshot may leave too).
ls -1t "$DEST"/blackboard-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f "$old" "$old-wal" "$old-shm"
done
echo "backed up -> $OUT (kept newest $KEEP in $DEST)"
