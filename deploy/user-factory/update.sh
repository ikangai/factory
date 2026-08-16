#!/bin/bash
# Pulls the latest factory code (pushed by the operator via `git push deploy main`) into the
# deployment's `deploy` branch, keeping the local-only config overlay intact.
#
# run, from the operator's shell:
#   sudo -u factory -i bash /Users/factory/fab/factory/deploy/user-factory/update.sh
#
# The `-i` matters, and so does not running it from a directory the factory user cannot
# reach. Once the guest-house home is 0700 (as it must be — see docs/runbooks/guest-house.md),
# a plain `sudo -u factory bash …` launched from inside the OPERATOR's home dies before this
# script's first line, in bash's own startup:
#   shell-init: error retrieving current directory: getcwd: cannot access parent directories
# `-i` starts a login shell in the target account's home, which fixes both the cwd and $HOME.
set -euo pipefail

if [ "$(id -un)" != "factory" ]; then
    echo "ERROR: this script must run AS the 'factory' user (not $(id -un))." >&2
    exit 1
fi

# Locate the deployment from THIS SCRIPT's own path, never from $HOME. `sudo -u factory`
# without -i/-H leaves HOME pointing at the INVOKING operator, so `cd "$HOME/fab/factory"`
# either failed outright or — before the home was tightened — could have resolved into the
# wrong tree entirely. Same class as the doctor auditing the wrong account because it trusted
# an inherited $HOME (drill 2, 2026-08-16, docs/runbooks/worker-isolation.md §Drill 2).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

ACCOUNT_HOME="$(dscl . -read "/Users/$(id -un)" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
if [ -n "$ACCOUNT_HOME" ] && [ "${HOME:-}" != "$ACCOUNT_HOME" ]; then
    echo "WARNING: \$HOME is ${HOME:-(unset)} but $(id -un)'s home is $ACCOUNT_HOME." >&2
    echo "         You are probably running without 'sudo -i'. This script uses its own" >&2
    echo "         location ($ROOT) and is unaffected, but anything it calls that reads" >&2
    echo "         \$HOME would be pointed at the wrong account. Prefer:" >&2
    echo "           sudo -u factory -i bash $ROOT/deploy/user-factory/update.sh" >&2
    export HOME="$ACCOUNT_HOME"
fi

cd "$ROOT"
echo "[update] fetching origin ..."
git fetch origin
git checkout deploy

echo "[update] merging origin/main into deploy ..."
if ! git merge origin/main --no-edit; then
    echo "MERGE CONFLICT."
    echo "Conflict-resolution rule for config.yaml (the file most likely to conflict, since"
    echo "deploy carries 4 local overlay values on top of lines upstream may also touch):"
    echo "  keep the DEPLOY values for the 4 overlay knobs —"
    echo "    autopilot.prod=false, super_worker.user=\"\", super_worker.claude_bin=\"claude\","
    echo "    dashboard.port=9787 —"
    echo "  and take UPSTREAM for everything else in the file."
    echo "attempting that automatically for config.yaml ..."
    git show origin/main:config.yaml > config.yaml
    python3 deploy/user-factory/apply-config-overlay.py config.yaml
    git add config.yaml
    git commit --no-edit
    echo "config.yaml auto-resolved and committed."
    echo "If OTHER files are STILL conflicted (git status), resolve them by hand, then:"
    echo "  git add <file> ...  &&  git commit"
    exit 1
fi

cat <<'EOF'

============================================================
 Update landed on deploy. Restart, as the OPERATOR (sudo), for a graceful brake:
   1. Ask the deployment to stop cleanly first:
        sudo -u factory -i bash -lc 'touch $HOME/fab/factory/STOP && $HOME/fab/factory/bin/factory mode shift'
   2. Then kick the daemons to pick up the new code:
        sudo launchctl kickstart -k system/com.factory.board
        sudo launchctl kickstart -k system/com.factory.fleet
      ("Could not find service" = the daemons were never installed — run step 03 once:
        sudo bash /Users/factory/fab/factory/deploy/user-factory/03-install-daemons.sh
      it is idempotent and doubles as the reload command.)
============================================================
EOF
