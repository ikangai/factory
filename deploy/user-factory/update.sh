#!/bin/bash
# Pulls the latest factory code (pushed by the operator via `git push deploy main`) into the
# deployment's `deploy` branch, keeping the local-only config overlay intact.
#
# run (as factory): bash $HOME/fab/factory/deploy/user-factory/update.sh
set -euo pipefail

if [ "$(id -un)" != "factory" ]; then
    echo "ERROR: this script must run AS the 'factory' user (not $(id -un))." >&2
    exit 1
fi

cd "$HOME/fab/factory"
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
