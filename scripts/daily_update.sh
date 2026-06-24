#!/usr/bin/env bash
# Daily factory update (the 09:00 human report).
#
# Runs a BOUNDED autonomous loop, then leaves an executive summary in updates/.
# Reads the standing mission from MISSION.md. Promotion stays a HUMAN action — this
# job never promotes; it only surfaces what's awaiting the human at the board.
#
# Activate (macOS, runs 09:00 daily):
#   cp deploy/com.clive-harness-factory.daily.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.clive-harness-factory.daily.plist
# Deactivate:  launchctl unload ~/Library/LaunchAgents/com.clive-harness-factory.daily.plist
# Run once now:  scripts/daily_update.sh
#
# Tunables (env or edit here): MAX_ROUNDS, TOKEN_BUDGET.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
FACTORY="$(cd "$HERE/.." && pwd)"
cd "$FACTORY"

MAX_ROUNDS="${FACTORY_DAILY_MAX_ROUNDS:-3}"
TOKEN_BUDGET="${FACTORY_DAILY_TOKEN_BUDGET:-200000}"

# Mission = the full "## Mission" section of MISSION.md (human-editable), flattened.
MISSION="$(awk '/^## Mission/{f=1;next} /^## /{f=0} f' MISSION.md 2>/dev/null \
           | tr '\n' ' ' | sed 's/  */ /g; s/^ *//; s/ *$//')"
[ -z "$MISSION" ] && MISSION="improve how clive drives a CLI"

echo "[daily_update] $(date '+%Y-%m-%d %H:%M')  mission: $MISSION"
echo "[daily_update] bounded loop: max_rounds=$MAX_ROUNDS token_budget=$TOKEN_BUDGET (never promotes)"

# The autonomous loop emits + saves the executive summary at the end of a real run.
bin/factory autonomous --mission "$MISSION" --max-rounds "$MAX_ROUNDS" --token-budget "$TOKEN_BUDGET"

LATEST="$(ls -t updates/*.md 2>/dev/null | head -1 || true)"
echo "[daily_update] summary: ${LATEST:-<none written>}"
