#!/usr/bin/env bash
# Single-line installer (docs/plans/2026-07-09-single-line-installer-design.md). Fetched via:
#
#   curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | bash
#
# and, for FURTHER instances bound to OTHER target repos on the same machine:
#
#   curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh | \
#     bash -s -- --target https://github.com/me/myrepo.git
#
# This file is fetched standalone (no repo checkout exists yet when curl pipes it into bash),
# so it must be fully self-contained: it clones the factory repo FIRST, then delegates the
# per-instance config patch to scripts/configure_instance.py from that fresh clone.
#
# Why one parent dir PER instance (the load-bearing constraint): bin/factory runs
# `python3 -m factory.<module>` from the repo's PARENT dir, so the clone MUST be named
# exactly `factory` — two instances therefore cannot share a parent. Layout:
#   <root>/<name>/factory/   clone of the factory repo, on local branch instance/<name>
#   <root>/<name>/<target>/  clone of the target repo (dir name = its basename)
#
# Every step is idempotent: re-running with the same args updates the existing instance
# (fetch + merge for the factory clone, re-run configure/init/smoke) instead of erroring.
set -euo pipefail

TARGET="https://github.com/ikangai/clive.git"
NAME=""
ROOT="$HOME/factories"
FACTORY_REPO="https://github.com/ikangai/factory.git"
BRANCH="main"
PROVIDER="clive"
BASE_BRANCH=""
PORT="auto"
SKIP_DEPS=false
CMD="install"

if [ "${1:-}" = "list" ]; then
    CMD="list"
    shift
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --name) NAME="$2"; shift 2 ;;
        --root) ROOT="$2"; shift 2 ;;
        --factory-repo) FACTORY_REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --provider) PROVIDER="$2"; shift 2 ;;
        --base-branch) BASE_BRANCH="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --skip-deps) SKIP_DEPS=true; shift ;;
        *) echo "ERROR: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

# Strip a trailing slash (a local-path target) before basename so "/tmp/x/" doesn't yield "".
TARGET="${TARGET%/}"
TARGET_BASENAME="$(basename "${TARGET%.git}")"
# Sanitize to [A-Za-z0-9._-] — a raw URL/path basename can carry characters that are unsafe in
# a directory or launcher-file name.
TARGET_BASENAME="$(printf '%s' "$TARGET_BASENAME" | tr -c 'A-Za-z0-9._-' '-')"

if [ "$CMD" = "list" ]; then
    echo "== instances under $ROOT =="
    FIRST_CONFIGURE=""
    for cfg in "$ROOT"/*/factory/scripts/configure_instance.py; do
        [ -e "$cfg" ] || continue
        FIRST_CONFIGURE="$cfg"
        break
    done
    if [ -z "$FIRST_CONFIGURE" ]; then
        echo "no instances under $ROOT"
        exit 0
    fi
    exec python3 "$FIRST_CONFIGURE" --list --instances-root "$ROOT"
fi

# A target repo whose basename is literally `factory` would make TARGET_DIR collide with the
# factory clone itself (both <root>/<name>/factory) — the sibling layout cannot represent it.
if [ "$TARGET_BASENAME" = "factory" ]; then
    echo "ERROR: a target repo named 'factory' collides with the factory clone dir itself" >&2
    echo "  (the layout is <root>/<name>/{factory,<target-basename>}) — rename the target" >&2
    echo "  repo/dir and re-run" >&2
    exit 1
fi

if [ -z "$NAME" ]; then
    NAME="$TARGET_BASENAME"
fi
if [ -z "$BASE_BRANCH" ]; then
    # clive is the reference target (its factory-extraction work lives on this branch
    # upstream); any other target gets a fresh factory/base the installer owns end to end.
    if [ "$TARGET_BASENAME" = "clive" ]; then
        BASE_BRANCH="chore/extract-factory"
    else
        BASE_BRANCH="factory/base"
    fi
fi

INSTANCE_DIR="$ROOT/$NAME"
echo "== installing factory instance '$NAME' -> $INSTANCE_DIR =="

# --- 1. preflight ---------------------------------------------------------------------------
echo "[1/10] preflight ..."
command -v git >/dev/null 2>&1 || { echo "ERROR: git is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required" >&2; exit 1; }
for cmd in claude gh tmux; do
    command -v "$cmd" >/dev/null 2>&1 \
        || echo "  WARNING: '$cmd' not found on PATH — install it before first real use"
done

mkdir -p "$INSTANCE_DIR"
# Canonicalize now so every path built from here (launcher, printed summary) is absolute —
# bin/factory resolves \$0 without following symlinks, so a relative path baked into the
# launcher would break the moment the caller's cwd changes.
INSTANCE_DIR="$(cd "$INSTANCE_DIR" && pwd)"
FACTORY_DIR="$INSTANCE_DIR/factory"
TARGET_DIR="$INSTANCE_DIR/$TARGET_BASENAME"

# --- 2. factory clone + instance/<name> branch -----------------------------------------------
echo "[2/10] factory clone + instance/$NAME branch ..."
if [ ! -d "$FACTORY_DIR/.git" ]; then
    echo "  cloning factory <- $FACTORY_REPO"
    git clone "$FACTORY_REPO" "$FACTORY_DIR"
else
    echo "  $FACTORY_DIR already cloned — fetching origin"
    git -C "$FACTORY_DIR" fetch origin
fi
# Identity fallback for EVERY commit this script may make (the update-path merge below AND
# the step-6 overlay commit): a fresh machine has no git identity, and "Please tell me who
# you are" would abort mid-install. Env vars, not `git -c`, so one guard covers both call
# sites bash-3.2-safely (macOS /bin/bash chokes on empty-array expansion under `set -u`);
# a configured identity always wins because the fallback is only exported when none exists.
if [ -z "$(git -C "$FACTORY_DIR" config user.email || true)" ]; then
    export GIT_AUTHOR_NAME="factory installer" GIT_AUTHOR_EMAIL="installer@factory.local"
    export GIT_COMMITTER_NAME="factory installer" GIT_COMMITTER_EMAIL="installer@factory.local"
fi
if git -C "$FACTORY_DIR" show-ref --verify --quiet "refs/heads/instance/$NAME"; then
    git -C "$FACTORY_DIR" checkout "instance/$NAME"
    if ! git -C "$FACTORY_DIR" merge "origin/$BRANCH" --no-edit; then
        echo "ERROR: merge conflict updating instance/$NAME — resolve by hand in $FACTORY_DIR" >&2
        exit 1
    fi
else
    git -C "$FACTORY_DIR" checkout -B "instance/$NAME" "origin/$BRANCH"
fi

# --- 3. target clone + base branch ------------------------------------------------------------
echo "[3/10] target clone + base branch '$BASE_BRANCH' ..."
if [ ! -d "$TARGET_DIR/.git" ]; then
    echo "  cloning target <- $TARGET"
    git clone "$TARGET" "$TARGET_DIR"
    if [ -n "$(git ls-remote --heads "$TARGET" "$BASE_BRANCH" 2>/dev/null)" ]; then
        git -C "$TARGET_DIR" checkout -B "$BASE_BRANCH" "origin/$BASE_BRANCH"
    else
        git -C "$TARGET_DIR" checkout -B "$BASE_BRANCH"
    fi
else
    # A re-run must NOT force-reset the target's checked-out branch — the factory's own
    # graduation flow moves it forward between installer runs, and clobbering that here would
    # silently discard real work. Fetch only; leave whatever is checked out as-is.
    echo "  $TARGET_DIR already cloned — fetching origin"
    git -C "$TARGET_DIR" fetch origin
    echo "  leaving the currently checked-out branch as-is ($(git -C "$TARGET_DIR" branch --show-current))"
fi

# --- 4. python deps ----------------------------------------------------------------------------
echo "[4/10] python dependencies ..."
if [ "$SKIP_DEPS" = true ]; then
    echo "  --skip-deps set — skipping"
else
    python3 -m pip install --user -r "$FACTORY_DIR/requirements.txt" \
        || python3 -m pip install --user --break-system-packages -r "$FACTORY_DIR/requirements.txt"
    if [ -f "$TARGET_DIR/requirements.txt" ]; then
        python3 -m pip install --user -r "$TARGET_DIR/requirements.txt" \
            || python3 -m pip install --user --break-system-packages -r "$TARGET_DIR/requirements.txt"
    fi
fi

# --- 5. configure --------------------------------------------------------------------------
echo "[5/10] configuring instance ..."
PORT_LINE="$(python3 "$FACTORY_DIR/scripts/configure_instance.py" "$FACTORY_DIR/config.yaml" \
    --target-root "../$TARGET_BASENAME" \
    --provider "$PROVIDER" \
    --base-branch "$BASE_BRANCH" \
    --port "$PORT" \
    --instances-root "$ROOT")"
case "$PORT_LINE" in
    PORT=*) ASSIGNED_PORT="${PORT_LINE#PORT=}" ;;
    *) echo "ERROR: configure_instance.py did not print a PORT= line (got: $PORT_LINE)" >&2
       exit 1 ;;
esac
if [ "$PROVIDER" != "clive" ]; then
    cat >&2 <<NOTE
  NOTE: only the 'clive' adapter is registered (common/config.py get_adapter). The develop
  rail (briefs -> worker -> tests -> gated merge) is target-generic and will run against
  '$PROVIDER' as-is, but the scenario-eval loop needs a NEW adapter under factory/adapters/
  before it will run for this target.
NOTE
fi

# --- 6. commit the patched config.yaml if changed --------------------------------------------
echo "[6/10] committing config overlay ..."
if [ -n "$(git -C "$FACTORY_DIR" status --porcelain -- config.yaml)" ]; then
    # Identity for this commit is guaranteed by the step-2 env fallback.
    git -C "$FACTORY_DIR" add config.yaml
    git -C "$FACTORY_DIR" commit -m "instance/$NAME: configure target=$TARGET_BASENAME provider=$PROVIDER port=$ASSIGNED_PORT"
else
    echo "  config.yaml already matches — nothing to commit"
fi

# --- 7. init + runtime mode ------------------------------------------------------------------
echo "[7/10] factory init + runtime mode ..."
"$FACTORY_DIR/bin/factory" init
if [ ! -f "$FACTORY_DIR/.factory-mode" ]; then
    printf 'shift\n' > "$FACTORY_DIR/.factory-mode"   # SHIFT = safe default; AUTO is a conscious flip
else
    echo "  .factory-mode already set — leaving as-is"
fi

# --- 8. launcher -------------------------------------------------------------------------------
echo "[8/10] launcher ..."
LOCAL_BIN="$HOME/.local/bin"
LAUNCHER="$LOCAL_BIN/factory-$NAME"
if [ -w "$HOME" ]; then
    mkdir -p "$LOCAL_BIN"
    # A 2-line exec wrapper, NOT a symlink — bin/factory resolves $0 without following links,
    # so a symlink would compute the wrong MODULE_ROOT.
    {
        printf '#!/usr/bin/env bash\n'
        printf 'exec "%s/bin/factory" "$@"\n' "$FACTORY_DIR"
    } > "$LAUNCHER"
    chmod +x "$LAUNCHER"
    case ":$PATH:" in
        *":$LOCAL_BIN:"*) : ;;
        *) echo "  WARNING: $LOCAL_BIN is not on PATH — add it to use 'factory-$NAME' directly" ;;
    esac
else
    echo "  WARNING: HOME ($HOME) is not writable — skipping launcher creation"
    LAUNCHER=""
fi

# --- 9. smoke check ------------------------------------------------------------------------
echo "[9/10] smoke check ..."
"$FACTORY_DIR/bin/factory" status >/dev/null
echo "  bin/factory status: OK"

# --- 10. summary -----------------------------------------------------------------------------
echo "[10/10] done"
FLEET_PORT=$((ASSIGNED_PORT + 1))
# The uninstall line must only name the launcher when one was actually created.
UNINSTALL="rm -rf \"$INSTANCE_DIR\""
if [ -n "$LAUNCHER" ]; then
    UNINSTALL="$UNINSTALL \"$LAUNCHER\""
else
    LAUNCHER="(skipped — HOME not writable)"
fi
cat <<SUMMARY

============================================================
 Instance '$NAME' ready.
   path:       $INSTANCE_DIR
   factory:    $FACTORY_DIR
   target:     $TARGET_DIR
   board:      http://127.0.0.1:$ASSIGNED_PORT   (factory-$NAME board)
   fleet viz:  factory-$NAME viz --serve --port $FLEET_PORT
   launcher:   $LAUNCHER

 Manual next steps (never touched by this installer):
   - claude login    (Claude Code auth for the workers)
   - gh auth login    (GitHub auth for graduation pushes)
   - mode stays 'shift' — flip to 'auto' consciously when ready

 Uninstall: $UNINSTALL
 List all instances: install.sh list --root "$ROOT"
 Hardened unattended deploys: docs/runbooks/factory-user-deployment.md
============================================================
SUMMARY
