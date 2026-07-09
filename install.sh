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
TARGET_DIR_NAME=""
CMD="install"

if [ "${1:-}" = "list" ]; then
    CMD="list"
    shift
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --target-dir) TARGET_DIR_NAME="$2"; shift 2 ;;
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

# Sanitize to [A-Za-z0-9._-] — a raw URL/path basename or an operator-typed --name/--target-dir
# can carry characters that are unsafe in a directory or launcher-file name (spaces split the
# launcher path; '..' escapes the instances root and hides the instance from `list`).
sanitize() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-'
}

# Strip a trailing slash (a local-path target) before basename so "/tmp/x/" doesn't yield "".
TARGET="${TARGET%/}"
# The repo's own name (drives identity decisions: the clive base-branch default, the
# adapter note) stays distinct from the sibling DIR name (which --target-dir may override).
TARGET_REPO_NAME="$(sanitize "$(basename "${TARGET%.git}")")"
TARGET_BASENAME="$TARGET_REPO_NAME"
# --target-dir overrides the sibling dir name (e.g. for a target repo literally named
# 'factory', which would otherwise collide with the factory clone dir).
if [ -n "$TARGET_DIR_NAME" ]; then
    TARGET_BASENAME="$(sanitize "$TARGET_DIR_NAME")"
fi
if [ -n "$NAME" ]; then
    NAME="$(sanitize "$NAME")"
    if [ -z "$NAME" ] || [ "$(printf '%s' "$NAME" | tr -d -- '-')" = "" ]; then
        echo "ERROR: --name '$NAME' is empty after sanitizing to [A-Za-z0-9._-]" >&2
        exit 2
    fi
fi

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

# A target sibling dir literally named `factory` would collide with the factory clone itself
# (both <root>/<name>/factory) — the layout cannot represent it, but --target-dir renames the
# clone dir without needing any control over the upstream repo's name.
if [ "$TARGET_BASENAME" = "factory" ]; then
    echo "ERROR: a target dir named 'factory' collides with the factory clone dir itself" >&2
    echo "  (the layout is <root>/<name>/{factory,<target-dir>}) — re-run with" >&2
    echo "  --target-dir <other-name> to clone the target under a different dir name" >&2
    exit 1
fi

if [ -z "$NAME" ]; then
    NAME="$TARGET_BASENAME"
fi
if [ -z "$BASE_BRANCH" ]; then
    # clive is the reference target (its factory-extraction work lives on this branch
    # upstream); any other target gets a fresh factory/base the installer owns end to end.
    # Keyed on the repo's NAME, not the --target-dir override — it's an identity decision.
    if [ "$TARGET_REPO_NAME" = "clive" ]; then
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
# the step-6 overlay commit): a partially-provisioned machine may lack either half of the
# identity, and git dies on "empty ident name" when the OS can't derive one. Env vars, not
# `git -c`, so one guard covers both call sites bash-3.2-safely (macOS /bin/bash chokes on
# empty-array expansion under `set -u`); a fully configured identity always wins because
# the fallback is only exported when a half is missing.
if [ -z "$(git -C "$FACTORY_DIR" config user.email || true)" ] \
        || [ -z "$(git -C "$FACTORY_DIR" config user.name || true)" ]; then
    export GIT_AUTHOR_NAME="factory installer" GIT_AUTHOR_EMAIL="installer@factory.local"
    export GIT_COMMITTER_NAME="factory installer" GIT_COMMITTER_EMAIL="installer@factory.local"
fi
RERUN=false
if git -C "$FACTORY_DIR" show-ref --verify --quiet "refs/heads/instance/$NAME"; then
    RERUN=true
    git -C "$FACTORY_DIR" checkout "instance/$NAME"
    # A prior run that crashed between the step-5 config patch and the step-6 commit leaves
    # config.yaml dirty, which makes git REFUSE the merge before any merge state exists.
    # config.yaml is regenerated by step 5 anyway, so an uncommitted copy is safe to discard;
    # any OTHER dirty tracked file is real local work and aborts loudly.
    DIRTY="$(git -C "$FACTORY_DIR" status --porcelain --untracked-files=no)"
    if [ -n "$DIRTY" ]; then
        if [ -z "$(printf '%s\n' "$DIRTY" | grep -v ' config\.yaml$' || true)" ]; then
            echo "  discarding an uncommitted config.yaml from an interrupted prior run (step 5 re-applies the overlay)"
            git -C "$FACTORY_DIR" checkout -- config.yaml
        else
            echo "ERROR: $FACTORY_DIR has uncommitted changes beyond config.yaml — commit or stash them, then re-run:" >&2
            printf '%s\n' "$DIRTY" >&2
            exit 1
        fi
    fi
    if ! git -C "$FACTORY_DIR" merge "origin/$BRANCH" --no-edit; then
        # The overlay commit and upstream both touch config.yaml, and git coalesces nearby
        # hunks, so config.yaml conflicts are EXPECTED on updates. Resolution is mechanical:
        # take upstream's version wholesale (--theirs = origin/$BRANCH in a merge) — step 5
        # re-applies every instance value onto it right after. Anything else conflicting is
        # real divergence and stays a loud manual stop.
        CONFLICTS="$(git -C "$FACTORY_DIR" diff --name-only --diff-filter=U)"
        if [ "$CONFLICTS" = "config.yaml" ]; then
            echo "  config.yaml merge conflict auto-resolved: took origin/$BRANCH's version; the instance overlay re-applies in step 5"
            git -C "$FACTORY_DIR" checkout --theirs config.yaml
            git -C "$FACTORY_DIR" add config.yaml
            git -C "$FACTORY_DIR" commit --no-edit
        else
            echo "ERROR: merge conflict updating instance/$NAME — resolve by hand in $FACTORY_DIR" >&2
            exit 1
        fi
    fi
else
    git -C "$FACTORY_DIR" checkout -B "instance/$NAME" "origin/$BRANCH"
fi

# --- 3. target clone + base branch ------------------------------------------------------------
echo "[3/10] target clone + base branch '$BASE_BRANCH' ..."
if [ ! -d "$TARGET_DIR/.git" ]; then
    echo "  cloning target <- $TARGET"
    git clone "$TARGET" "$TARGET_DIR"
    # The clone just fetched every ref, so branch existence is answered locally — no second
    # round-trip to the remote.
    if git -C "$TARGET_DIR" show-ref --verify --quiet "refs/remotes/origin/$BASE_BRANCH"; then
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
pip_install() {
    # user-install with the PEP-668 fallback (same idiom as the deploy kit's bootstrap)
    python3 -m pip install --user -r "$1" \
        || python3 -m pip install --user --break-system-packages -r "$1"
}
if [ "$SKIP_DEPS" = true ]; then
    echo "  --skip-deps set — skipping"
else
    pip_install "$FACTORY_DIR/requirements.txt"
    if [ -f "$TARGET_DIR/requirements.txt" ]; then
        pip_install "$TARGET_DIR/requirements.txt"
    fi
fi
# Steps 5 (configure) and 7 (init) import yaml regardless of --skip-deps; fail with a named
# dependency instead of a bare ModuleNotFoundError traceback mid-install.
if ! python3 -c "import yaml" 2>/dev/null; then
    echo "ERROR: python3 cannot import 'yaml' (pyyaml) — install it (python3 -m pip install --user pyyaml)" >&2
    echo "  or re-run without --skip-deps" >&2
    exit 1
fi

# --- 5. configure --------------------------------------------------------------------------
echo "[5/10] configuring instance ..."
# Fresh install vs update decides the auto-port semantics, and only THIS layer knows which
# is which: fresh -> "auto" (probe with real bind tests), re-run -> "keep" (never churn a
# port a live board may hold). An explicit --port passes through either way.
EFFECTIVE_PORT="$PORT"
if [ "$PORT" = "auto" ] && [ "$RERUN" = true ]; then
    EFFECTIVE_PORT="keep"
fi
PORT_LINE="$(python3 "$FACTORY_DIR/scripts/configure_instance.py" "$FACTORY_DIR/config.yaml" \
    --target-root "../$TARGET_BASENAME" \
    --provider "$PROVIDER" \
    --base-branch "$BASE_BRANCH" \
    --port "$EFFECTIVE_PORT" \
    --instances-root "$ROOT")"
case "$PORT_LINE" in
    PORT=*) ASSIGNED_PORT="${PORT_LINE#PORT=}" ;;
    *) echo "ERROR: configure_instance.py did not print a PORT= line (got: $PORT_LINE)" >&2
       exit 1 ;;
esac
# The eval-loop honesty note must fire on the DEFAULT path too: a non-clive target under the
# default clive provider is exactly the case where the scenario-eval loop will predictably
# fail (CliveAdapter expects clive's layout), and gating the note on --provider alone warned
# nobody on the installer's headline use case.
if [ "$PROVIDER" != "clive" ]; then
    cat >&2 <<NOTE
  NOTE: provider '$PROVIDER' is only usable if you have registered its adapter in
  common/config.py get_adapter (only 'clive' ships registered). The develop rail
  (briefs -> worker -> tests -> gated merge) is target-generic; the scenario-eval
  loop needs that adapter.
NOTE
elif [ "$TARGET_REPO_NAME" != "clive" ]; then
    cat >&2 <<NOTE
  NOTE: target '$TARGET_REPO_NAME' runs under the default 'clive' adapter. The develop
  rail (briefs -> worker -> tests -> gated merge) is target-generic and works as-is;
  the scenario-eval loop expects clive's layout and needs a NEW adapter under
  factory/adapters/ (wired in common/config.py get_adapter) for this target.
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
   fleet viz:  factory-$NAME viz --serve   (port $FLEET_PORT — derived from the board port)
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
