#!/bin/bash
# Sets up the deployment tree, secrets, and dependencies from INSIDE the dedicated `factory`
# macOS user (created by 01-create-user.sh, run by the operator). Everything here runs as
# `factory` — no operator credentials are ever read or copied.
#
# run (as factory): bash /Users/Shared/factory-kit/02-bootstrap-as-factory.sh [snapshot.db]
#   snapshot.db defaults to /Users/Shared/factory-seed/blackboard.db (optional).
set -euo pipefail

if [ "$(id -un)" != "factory" ]; then
    echo "ERROR: this script must run AS the 'factory' user (not $(id -un))." >&2
    echo "  e.g.: sudo -u factory -i bash /Users/Shared/factory-kit/02-bootstrap-as-factory.sh" >&2
    exit 1
fi

FAB="$HOME/fab"
BARE=/Users/Shared/factory.git
SNAPSHOT="${1:-/Users/Shared/factory-seed/blackboard.db}"

echo "== bootstrapping factory deployment in $FAB =="

# --- 1. secrets scaffold: a 600 env file, never a plist, never argv to a daemon ----------
SECRETS_DIR="$HOME/.factory-secrets"
ENV_FILE="$SECRETS_DIR/env"
install -d -m 700 "$SECRETS_DIR"
if [ ! -f "$ENV_FILE" ]; then
    echo "[1/10] creating $ENV_FILE ..."
    read -rs -p "Paste the GitHub fine-grained PAT for ikangai/clive (runbook §2): " GH_PAT
    echo
    # Sanitize the paste: clipboards routinely smuggle \r or stray whitespace into the
    # hidden prompt, and one control char makes every Authorization header invalid
    # ("invalid header field value"). PATs are [A-Za-z0-9_]+ — stripping all whitespace
    # is always safe. %q writes the value shell-safe into the sourced env file.
    GH_PAT="$(printf %s "$GH_PAT" | tr -d '[:space:]')"
    if [ -z "$GH_PAT" ]; then
        echo "ERROR: empty token — copy it fresh from the GitHub token page and rerun." >&2
        exit 1
    fi
    case "$GH_PAT" in
        *[!A-Za-z0-9_]*) echo "ERROR: token contains unexpected characters after sanitizing — paste it fresh (a PAT is letters/digits/underscores only)." >&2; exit 1 ;;
    esac
    ( umask 077
      { printf '%s\n' \
          "# factory secrets — sourced by the LaunchDaemon wrapper (with-env.sh) and this user's shell." \
          "# 600 by construction (umask 077 at creation). Never commit, never put in a plist."
        printf 'export GH_TOKEN=%q\n' "$GH_PAT"
        printf '%s\n' "# export CLAUDE_CODE_OAUTH_TOKEN=...  # filled by runbook §3 (claude setup-token)"
      } > "$ENV_FILE"
    )
    unset GH_PAT
    chmod 600 "$ENV_FILE"
else
    echo "[1/10] $ENV_FILE already exists — skipping creation"
fi
ZPROFILE_LINE='source "$HOME/.factory-secrets/env"'
if ! grep -qF "$ZPROFILE_LINE" "$HOME/.zprofile" 2>/dev/null; then
    echo "  wiring $ENV_FILE into ~/.zprofile ..."
    printf '\n%s\n' "$ZPROFILE_LINE" >> "$HOME/.zprofile"
else
    echo "  ~/.zprofile already sources the secrets file — skipping"
fi

# --- 2. gh auth: gh's git credential helper honors GH_TOKEN, no keychain needed ----------
echo "[2/10] gh auth ..."
# shellcheck disable=SC1090
source "$ENV_FILE"
gh auth setup-git
LOGIN="$(gh api user -q .login)"
if [ -z "$LOGIN" ]; then
    echo "ERROR: 'gh api user' returned nothing — check GH_TOKEN in $ENV_FILE" >&2
    exit 1
fi
echo "  authenticated to GitHub as: $LOGIN"
if ! git ls-remote https://github.com/ikangai/clive.git HEAD >/dev/null; then
    echo "ERROR: cannot reach ikangai/clive via HTTPS with this token — check PAT repo scope (runbook §2)" >&2
    exit 1
fi
echo "  ikangai/clive is reachable"

# --- 3. clones: factory from the shared bare repo, clive from GitHub --------------------
echo "[3/10] clones ..."
install -d -m 755 "$FAB"
# The bare repo is OPERATOR-owned by design (one-way code handoff), so git's
# dubious-ownership guard blocks this cross-user read until it's whitelisted —
# consciously, for exactly this one path. Covers clone now and update.sh fetches later.
if ! git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$BARE"; then
    git config --global --add safe.directory "$BARE"
    echo "  whitelisted $BARE as a git safe.directory for this user"
fi
if [ ! -d "$FAB/factory/.git" ]; then
    echo "  cloning factory <- $BARE"
    git clone "$BARE" "$FAB/factory"
else
    echo "  $FAB/factory already cloned — fetching origin"
    git -C "$FAB/factory" fetch origin
fi
if [ ! -d "$FAB/clive/.git" ]; then
    echo "  cloning clive <- https://github.com/ikangai/clive.git"
    git clone https://github.com/ikangai/clive.git "$FAB/clive"
else
    echo "  $FAB/clive already cloned — skipping"
fi
# Sibling layout ($FAB/factory next to $FAB/clive) preserves config's target.root: "../clive".
# The clive.factory-auto worktree is created automatically on the first real shift.
git -C "$FAB/clive" checkout chore/extract-factory

# --- 4. python deps (BEFORE the overlay — apply-config-overlay.py imports yaml) -----------
echo "[4/10] python dependencies ..."
python3 -m pip install --user -r "$FAB/factory/requirements.txt" \
    || python3 -m pip install --user --break-system-packages -r "$FAB/factory/requirements.txt"
if [ -f "$FAB/clive/requirements.txt" ]; then
    python3 -m pip install --user -r "$FAB/clive/requirements.txt" \
        || python3 -m pip install --user --break-system-packages -r "$FAB/clive/requirements.txt"
fi

# --- 5. deploy branch + local-only config overlay ----------------------------------------
echo "[5/10] deploy branch + config overlay ..."
cd "$FAB/factory"
if git rev-parse --verify deploy >/dev/null 2>&1; then
    git checkout deploy
    git merge origin/main --no-edit
else
    git checkout -B deploy origin/main
fi
# When run from /Users/Shared/factory-kit the overlay script sits right beside this one; after
# the first bootstrap it also lives in the clone itself at deploy/user-factory/.
OVERLAY="$(dirname "$0")/apply-config-overlay.py"
python3 "$OVERLAY" "$FAB/factory/config.yaml"
if [ -n "$(git status --porcelain)" ]; then
    git add config.yaml
    git commit -m "deploy: local config overlay (prod=false, same-user workers, board port 9787)"
else
    echo "  config.yaml already matches the deploy overlay — nothing to commit"
fi

# --- 6. git identity (commits made by this user's factory bot) ---------------------------
echo "[6/10] git identity ..."
if [ -z "$(git config --global user.name || true)" ]; then
    git config --global user.name "factory bot"
fi
if [ -z "$(git config --global user.email || true)" ]; then
    git config --global user.email "martin.treiber+factory@gmail.com"
fi

# --- 7. claude CLI -------------------------------------------------------------------------
echo "[7/10] claude CLI ..."
if ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ]; then
    echo "  installing claude CLI ..."
    curl -fsSL https://claude.ai/install.sh | bash
else
    echo "  claude CLI already present — skipping install"
fi
echo "  MANUAL next (runbook §3, via fast user switching): 'claude login', 'claude setup-token', agora plugin install."

# --- 8. blackboard seed (optional) --------------------------------------------------------
echo "[8/10] blackboard seed ..."
DB="$FAB/factory/store/blackboard.db"
if [ -f "$DB" ]; then
    echo "  $DB already present — skipping seed"
elif [ -f "$SNAPSHOT" ]; then
    echo "  seeding from $SNAPSHOT (DB only — STOP/.autopilot.pid* are never carried over)"
    install -d -m 755 "$(dirname "$DB")"
    # The snapshot is a QUIESCED backup artifact (made by sqlite .backup, no live writers,
    # no -wal sidecar), so a plain cp is safe — and necessary: sqlite opening a WAL-mode
    # DB even read-only must create -shm next to it, which this user can't do in the
    # operator-owned seed dir ("attempt to write a readonly database"). The integrity
    # check below runs on OUR copy, where WAL sidecars are creatable.
    cp "$SNAPSHOT" "$DB"
    chmod 600 "$DB"
    if [ "$(sqlite3 "$DB" 'PRAGMA integrity_check;')" != "ok" ]; then
        echo "ERROR: integrity check failed for seeded $DB" >&2
        exit 1
    fi
    echo "  seed OK (integrity check passed)"
else
    echo "  no snapshot at $SNAPSHOT — starting with an EMPTY blackboard (no learnings/history)"
fi

# --- 9. runtime state ----------------------------------------------------------------------
echo "[9/10] runtime state ..."
mkdir -p "$FAB/factory/.groupchat" "$FAB/factory/logs" "$FAB/factory/updates"
if [ ! -f "$FAB/factory/.factory-mode" ]; then
    printf 'shift\n' > "$FAB/factory/.factory-mode"   # SHIFT = safe default until the supervised smoke passes
else
    echo "  .factory-mode already set — leaving as-is"
fi
if [ -f "$FAB/factory/STOP" ]; then
    echo "  WARNING: STOP is present at $FAB/factory/STOP — remove it CONSCIOUSLY when ready (not silently by this script)"
fi

# --- 10. smoke checks (loud fail — every one of these must pass) --------------------------
echo "[10/10] smoke checks ..."
python3 -c "import yaml, defusedxml"
"$FAB/factory/bin/factory" status
gh auth status
claude --version || echo "  claude not on PATH yet (ok if the installer just ran — open a new shell)"

cat <<'EOF'

============================================================
 Bootstrap complete. Next:
   - runbook §3: manual logins (claude login, claude setup-token, agora plugin)
   - runbook §4: supervised smoke shift
   docs/runbooks/factory-user-deployment.md
============================================================
EOF
