#!/bin/bash
# LaunchDaemon wrapper: /Library/LaunchDaemons plists are world-readable, so secrets can
# NEVER live in a plist's ProgramArguments/EnvironmentVariables — they live only in the 600
# env file, sourced here. `caffeinate -i -s` holds off idle/system sleep for as long as the
# wrapped `factory` command (a long-running server) is alive, so the always-on daemons don't
# get suspended mid-shift.
set -euo pipefail
FAB=/Users/factory/fab
export HOME=/Users/factory
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# shellcheck disable=SC1091
. "$HOME/.factory-secrets/env"
cd "$FAB/factory"
exec /usr/bin/caffeinate -i -s "$FAB/factory/bin/factory" "$@"
