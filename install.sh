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
PROVIDER=""
BASE_BRANCH=""
PORT="auto"
SKIP_DEPS=false
TARGET_DIR_NAME=""
CMD="install"
# --guest-house (docs/plans/2026-08-06-production-hardening-roadmap.md Phase 0): an
# interactive wizard mode, ADDITIVE to the normal 10-phase flow above — it never changes
# that flow's default behavior. --wsl selects the Linux-inside-WSL variant (called by
# install.ps1). --yes ONLY auto-answers the WIZARD'S OWN confirmation prompts — it can
# NEVER make a macOS guest-house install (--guest-house without --wsl) fully unattended:
# creating the account (sudo), the new account's own login password, and pasting a GitHub
# token are three genuinely interactive secrets, always. See the guest-house block below
# for the full --yes contract.
GUEST_HOUSE=false
WSL_MODE=false
YES=false

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
        --guest-house) GUEST_HOUSE=true; shift ;;
        --wsl) WSL_MODE=true; shift ;;
        --yes) YES=true; shift ;;
        *) echo "ERROR: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

# Sanitize to [A-Za-z0-9._-] — a raw URL/path basename or an operator-typed --name/--target-dir
# can carry characters that are unsafe in a directory or launcher-file name (spaces split the
# launcher path; '..' escapes the instances root and hides the instance from `list`).
sanitize() {
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-'
}

# =============================================================================================
# --guest-house wizard mode (docs/plans/2026-08-06-production-hardening-roadmap.md, Phase 0).
# ORCHESTRATES deploy/user-factory/01..03 + scripts/guesthouse_check.py — none of them are
# rewritten here, only called. Runs BEFORE the normal 10-phase flow below and exits when done;
# every default flag/behavior of plain install.sh is untouched by this block's presence.
# docs/runbooks/guest-house.md documents what each step does and why, in the same words used
# in the prompts below (so a reader following along the runbook recognizes every prompt).
#
# --yes ONLY auto-answers the wizard's OWN confirmation prompts below (gh_confirm). It can
# NEVER make a macOS guest-house install (--guest-house without --wsl) fully unattended:
# creating the account needs an admin (sudo) password, the new account needs its OWN login
# password, and the bootstrap step needs a GitHub token pasted in — three genuinely
# interactive secrets, always. A real terminal (/dev/tty) is therefore ALWAYS required for
# macOS guest-house installs, --yes or not (run_guest_house_wizard enforces this before any
# secret-touching step runs). The --wsl variant has no such secrets in its own flow, so there
# --yes can make the whole thing non-interactive.
# =============================================================================================

GH_DOCS_BASE_URL="https://github.com/ikangai/factory/blob/main"

# `curl | bash` pipes the SCRIPT itself over stdin, so /dev/tty (not stdin) is the only place
# an interactive prompt — or a secret a kit script reads via `read -rs` — can come from. This
# probes readability once without leaving a stray fd open.
gh_have_tty() {
    { : < /dev/tty; } 2>/dev/null
}

# Prints an explanation (already echoed by the caller) then asks y/N. --yes auto-confirms the
# WIZARD'S OWN prompts only — see the block comment above; it never substitutes for a real
# terminal a kit script needs to read a secret from.
gh_confirm() {
    local prompt="${1:-Proceed?}"
    if [ "$YES" = true ]; then
        echo "  (--yes) $prompt -> yes"
        return 0
    fi
    if ! gh_have_tty; then
        echo "ERROR: no terminal available to ask '$prompt' (stdin is not a terminal, e.g. curl | bash)." >&2
        echo "  re-run with --yes to skip the wizard's OWN confirmations (a real terminal is still" >&2
        echo "  required for any step that reads an interactive secret), or run from a real terminal:" >&2
        echo "    bash install.sh --guest-house --yes" >&2
        exit 1
    fi
    local reply=""
    printf '%s [y/N] ' "$prompt" > /dev/tty
    IFS= read -r reply < /dev/tty || reply=""
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# Clones (or fast-forwards) the factory repo the wizard orchestrates deploy/user-factory/*
# scripts FROM — a curl-piped install.sh has no on-disk checkout of its own, exactly like the
# normal flow's own step 2, and reuses the SAME --factory-repo/--branch flags for hermetic tests.
gh_ensure_factory_checkout() {
    GH_ROOT="$HOME/factories/guest-house"
    GH_FACTORY_DIR="$GH_ROOT/factory"
    mkdir -p "$GH_ROOT"
    if [ ! -d "$GH_FACTORY_DIR/.git" ]; then
        echo "  cloning factory <- $FACTORY_REPO ($BRANCH) into $GH_FACTORY_DIR ..."
        git clone --branch "$BRANCH" "$FACTORY_REPO" "$GH_FACTORY_DIR"
    else
        echo "  $GH_FACTORY_DIR already cloned — fetching + fast-forwarding $BRANCH ..."
        git -C "$GH_FACTORY_DIR" fetch origin "$BRANCH"
        git -C "$GH_FACTORY_DIR" checkout "$BRANCH"
        if ! git -C "$GH_FACTORY_DIR" merge --ff-only "origin/$BRANCH"; then
            echo "  WARNING: local $BRANCH has diverged from origin/$BRANCH in $GH_FACTORY_DIR — leaving as-is"
        fi
    fi
}

gh_preflight() {
    echo "== guest-house preflight =="
    if [ "$WSL_MODE" = true ]; then
        if [ "$(uname -s)" != "Linux" ]; then
            echo "ERROR: --guest-house --wsl only runs inside a Linux (WSL) distro." >&2
            exit 1
        fi
        command -v git >/dev/null 2>&1 || { echo "ERROR: git is required" >&2; exit 1; }
    else
        if [ "$(uname -s)" != "Darwin" ]; then
            echo "ERROR: --guest-house (without --wsl) only runs on macOS." >&2
            echo "  On Windows, use install.ps1 instead (EXPERIMENTAL — see its header):" >&2
            echo "    irm https://raw.githubusercontent.com/ikangai/factory/main/install.ps1 | iex" >&2
            exit 1
        fi
        if [ "$(id -u)" -eq 0 ]; then
            echo "ERROR: do not run this wizard itself as root/sudo — it only sudo's individual steps" >&2
            echo "  (creating the dedicated user), and tells you why right before each password prompt." >&2
            echo "  run as your normal (admin) user: bash install.sh --guest-house" >&2
            exit 1
        fi
        command -v git >/dev/null 2>&1 || { echo "ERROR: git is required" >&2; exit 1; }
        if ! xcode-select -p >/dev/null 2>&1; then
            echo "ERROR: Xcode Command Line Tools not found — run: xcode-select --install" >&2
            exit 1
        fi
        command -v sudo >/dev/null 2>&1 || { echo "ERROR: sudo is required (it provisions the dedicated user) but is not available on this machine." >&2; exit 1; }
        AVAIL_KB="$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
        if [ -n "$AVAIL_KB" ] && [ "$AVAIL_KB" -lt 2097152 ] 2>/dev/null; then
            echo "  WARNING: less than 2GB free under $HOME ($((AVAIL_KB / 1024))MB) — the deployment clones two repos + installs dependencies" >&2
        fi
    fi
    echo "  preflight OK"
}

gh_print_rules_pointer() {
    echo "  Full rules table (what this boundary gives, what it does NOT give yet): $GH_DOCS_BASE_URL/docs/runbooks/guest-house.md"
}

# Best-effort state summary printed by the exit/interrupt traps below — every step it reports
# on is independently idempotent, so "re-run the wizard" is always valid, honest resume
# guidance, never a lie about what got lost.
gh_state_summary() {
    echo "== guest-house wizard — state as of this exit =="
    if id factory >/dev/null 2>&1; then
        echo "  'factory' user: exists"
    else
        echo "  'factory' user: NOT created"
    fi
    if [ "$WSL_MODE" != true ] && [ -f /Users/Shared/factory-kit/02-bootstrap-as-factory.sh ]; then
        echo "  deploy kit staged at /Users/Shared/factory-kit: yes"
    elif [ "$WSL_MODE" != true ]; then
        echo "  deploy kit staged at /Users/Shared/factory-kit: no"
    fi
    GH_RESUME_CMD="bash install.sh --guest-house"
    if [ "$WSL_MODE" = true ]; then
        GH_RESUME_CMD="$GH_RESUME_CMD --wsl"
    fi
    echo "  resume: re-run this wizard ($GH_RESUME_CMD) — every step detects what's already"
    echo "  done and skips it; nothing here is destroyed by re-running."
}

# EXIT fires for every non-zero `exit N` elsewhere in the wizard (a real failure); INT/TERM
# (Ctrl-C or a kill) is inherently an abort regardless of the last command's own exit status,
# so it gets its own unconditional handler that clears the traps before re-exiting (avoids a
# double print through the EXIT trap).
gh_on_exit() {
    local rc=$?
    if [ "$rc" -ne 0 ]; then
        echo >&2
        echo "== guest-house wizard stopped (exit $rc) ==" >&2
        gh_state_summary >&2
    fi
}
gh_on_interrupt() {
    trap - EXIT INT TERM
    echo >&2
    echo "== guest-house wizard interrupted ==" >&2
    gh_state_summary >&2
    exit 130
}

# The 'factory' account must never hold admin rights before secrets (a GitHub token, its own
# login) are bootstrapped into it — whether freshly created or adopted from a prior run.
# Fails CLOSED: an unresolvable/ambiguous membership check aborts rather than proceeding.
gh_verify_factory_non_admin() {
    local out rc
    out="$(dseditgroup -o checkmember -m factory admin 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qi '^yes'; then
        echo "ERROR: the 'factory' user is an ADMIN account." >&2
        echo "  Guest-house secrets (a GitHub token, the account's own login) must never be" >&2
        echo "  bootstrapped into an admin account — that defeats the whole boundary." >&2
        echo "  Fix: sudo dseditgroup -o edit -d factory -t user admin" >&2
        echo "  (or delete and recreate 'factory' as a Standard user), then re-run this wizard." >&2
        exit 1
    fi
    if ! printf '%s' "$out" | grep -qi '^no'; then
        echo "ERROR: could not determine whether 'factory' is an admin (dseditgroup: rc=$rc, $out) — failing closed." >&2
        echo "  Verify by hand: dseditgroup -o checkmember -m factory admin" >&2
        exit 1
    fi
}

# --- macOS wizard --------------------------------------------------------------------------
gh_wizard_mac() {
    echo
    echo "============================================================"
    echo " Guest-house install (macOS)"
    echo " A dedicated, NON-ADMIN 'factory' user runs the factory,"
    echo " isolated from your own account, files, keychain, and SSH"
    echo " keys — the OS enforces the boundary, not a config setting."
    echo " This install ALWAYS needs a real terminal: creating the"
    echo " account (sudo), the account's own password, and pasting a"
    echo " GitHub token are three unavoidable interactive secrets."
    echo "============================================================"
    gh_print_rules_pointer

    gh_ensure_factory_checkout

    echo
    echo "[1/6] Create/refresh the dedicated 'factory' user"
    echo "  This is a separate, non-admin macOS account — anything that runs as it (the"
    echo "  factory, its workers) cannot read your files, your keychain, or your SSH keys."
    echo "  01-create-user.sh is internally idempotent — it re-syncs current code into the"
    echo "  shared bare repo and re-stages the deploy kit even when 'factory' already exists,"
    echo "  so it is always safe (and recommended) to run again. Creating/refreshing needs an"
    echo "  administrator password ONCE, right now, via sudo — the wizard itself is NOT root."
    if gh_confirm "Run 01-create-user.sh now (sudo)?"; then
        # shellcheck disable=SC2024  # deliberate: relays the CALLER's real tty into the
        # sudo'd child's stdin (fixes the secrets-eat-the-pipe bug), not "read as root".
        sudo bash "$GH_FACTORY_DIR/deploy/user-factory/01-create-user.sh" < /dev/tty
    elif id factory >/dev/null 2>&1; then
        echo "  skipped — 'factory' already exists; continuing with what's already staged."
    else
        echo "ERROR: the 'factory' user is required for a guest-house install — aborting." >&2
        exit 1
    fi
    if id factory >/dev/null 2>&1; then
        gh_verify_factory_non_admin
        echo "  tightening /Users/factory to mode 700 (no group/other access) ..."
        sudo chmod 700 /Users/factory \
            || echo "  WARNING: could not chmod /Users/factory to 700 — do this by hand: sudo chmod 700 /Users/factory"
    fi

    echo
    echo "[2/6] Sign in to Claude as the 'factory' user"
    echo "  The factory user needs its OWN Claude Code login — it must never reuse your"
    echo "  session or credentials. Use Fast User Switching (Apple menu, or the login-window"
    echo "  shortcut) to switch into 'factory', open a terminal there, and run: claude login"
    echo "  Full steps: $GH_DOCS_BASE_URL/docs/runbooks/factory-user-deployment.md (section 3)."
    if [ "$YES" = true ]; then
        echo "  (--yes) not waiting — do this manually before step 4 (daemons) or step 5 (doctor) will show it missing."
    else
        gh_confirm "Done — has 'claude login' been completed as the 'factory' user?" || \
            echo "  continuing anyway — the doctor (step 5) and the runbook's supervised smoke shift (§4) will catch a missing login."
    fi

    echo
    KIT=/Users/Shared/factory-kit
    GH_BOOTSTRAP_OK=false
    if [ ! -f "$KIT/02-bootstrap-as-factory.sh" ]; then
        echo "[3/6] $KIT/02-bootstrap-as-factory.sh not found yet — step 1 should have staged it there."
        echo "  Re-run this wizard after step 1 completes."
    else
        echo "[3/6] Bootstrap the deployment as the 'factory' user"
        echo "  This clones the factory + target repos, installs dependencies, and prompts for a"
        echo "  GitHub token scoped to the target repo only — all of it running AS 'factory', never"
        echo "  reading your own credentials. Pasting the token needs this same real terminal."
        if gh_confirm "Run the bootstrap now (sudo -u factory -i bash 02-bootstrap-as-factory.sh)?"; then
            # shellcheck disable=SC2024  # deliberate: relays the CALLER's real tty into the
            # sudo'd child's stdin (fixes the secrets-eat-the-pipe bug), not "read as root".
            if sudo -u factory -i bash "$KIT/02-bootstrap-as-factory.sh" < /dev/tty; then
                GH_BOOTSTRAP_OK=true
                echo "  engaging the safety brake (STOP) in the new deployment ..."
                if ! sudo -u factory -i bash -lc 'touch "$HOME/fab/factory/STOP"'; then
                    echo "  WARNING: could not create STOP — do this by hand before enabling anything:"
                    echo "    sudo -u factory -i bash -lc 'touch \$HOME/fab/factory/STOP'"
                fi
            else
                echo "ERROR: bootstrap failed (see output above) — fix the issue and re-run this wizard." >&2
            fi
        else
            echo "  skipped — run later by hand:"
            echo "    sudo -u factory -i bash $KIT/02-bootstrap-as-factory.sh"
        fi
    fi

    echo
    echo "[4/6] Always-on daemons (OPTIONAL — default: not yet)"
    echo "  Daemons make the factory run unattended, all the time. The runbook's supervised"
    echo "  smoke shift ($GH_DOCS_BASE_URL/docs/runbooks/factory-user-deployment.md, §4) should"
    echo "  pass FIRST, watched, before anything runs unattended — installing daemons now would"
    echo "  skip that check."
    if [ "$YES" = true ]; then
        echo "  (--yes) leaving daemons OFF (the safe default). Install later with:"
        echo "    sudo bash $GH_FACTORY_DIR/deploy/user-factory/03-install-daemons.sh"
    elif gh_confirm "Install the always-on daemons now anyway? (default: No — recommended to wait)"; then
        sudo bash "$GH_FACTORY_DIR/deploy/user-factory/03-install-daemons.sh"
    else
        echo "  skipped (recommended) — install later, after the supervised smoke shift, with:"
        echo "    sudo bash $GH_FACTORY_DIR/deploy/user-factory/03-install-daemons.sh"
    fi

    echo
    echo "[5/6] Guest-house doctor"
    if ! id factory >/dev/null 2>&1; then
        echo "  'factory' user not present — doctor cannot run yet."
    else
        if ! sudo -u factory -i bash -lc '
            if [ ! -f "$HOME/fab/factory/scripts/guesthouse_check.py" ]; then
                echo "  bootstrap (step 3) has not run yet — doctor cannot run yet."
                exit 0
            fi
            cd "$HOME/fab/factory" && python3 scripts/guesthouse_check.py
        '; then
            echo "  doctor reported at least one FAIL above (a real run, non-zero exit) — see $GH_DOCS_BASE_URL/docs/runbooks/guest-house.md for fixes."
        fi
    fi

    echo
    echo "[6/6] Summary"
    if [ "$GH_BOOTSTRAP_OK" = true ]; then
        GH_BRAKES_LINE="   brakes:    mode stays 'shift' (never auto); STOP was dropped by this wizard right"
        GH_BRAKES_LINE2="              after bootstrap (step 3) and stays engaged until removed on purpose."
    else
        GH_BRAKES_LINE="   brakes:    bootstrap did not complete this run, so STOP has NOT been set yet —"
        GH_BRAKES_LINE2="              re-run this wizard, or set it by hand once 'factory' exists:"
        GH_BRAKES_LINE2="$GH_BRAKES_LINE2 sudo -u factory -i bash -lc 'touch \$HOME/fab/factory/STOP'"
    fi
    cat <<SUMMARY
============================================================
 Guest-house install (macOS) — done.
$GH_BRAKES_LINE
$GH_BRAKES_LINE2
   next:      1. $GH_DOCS_BASE_URL/docs/runbooks/factory-user-deployment.md §4 — run ONE
                 supervised smoke shift, watched, before any daemon/always-on step.
              2. Only after that: sudo bash $GH_FACTORY_DIR/deploy/user-factory/03-install-daemons.sh
   re-run the doctor any time:
     sudo -u factory -i bash -lc 'cd \$HOME/fab/factory && python3 scripts/guesthouse_check.py'
   rules table:     $GH_DOCS_BASE_URL/docs/runbooks/guest-house.md
   teardown:        $GH_DOCS_BASE_URL/docs/runbooks/factory-user-deployment.md §8
============================================================
SUMMARY
}

# --- WSL variant (called by install.ps1; assumes Linux) ------------------------------------
gh_self_path() {
    if [ -f "$0" ]; then
        echo "$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    else
        echo "ERROR: --guest-house --wsl needs install.sh to be a real file on disk (not piped via" >&2
        echo "  stdin) so it can re-invoke itself as the 'factory' user. Download it first (as root" >&2
        echo "  inside the distro, /tmp is writable and already what install.ps1 itself uses):" >&2
        echo "    curl -fsSL https://raw.githubusercontent.com/ikangai/factory/main/install.sh -o /tmp/install.sh" >&2
        echo "  then: bash /tmp/install.sh --guest-house --wsl" >&2
        exit 1
    fi
}

gh_wizard_wsl() {
    echo
    echo "============================================================"
    echo " Guest-house install (WSL)"
    echo " A dedicated, non-admin Linux user runs the factory inside"
    echo " this distro. Combined with the /etc/wsl.conf hardening"
    echo " install.ps1 applies (no Windows drives, no Windows exec,"
    echo " no host PATH), this bounds the factory to the distro."
    echo "============================================================"
    gh_print_rules_pointer
    SELF_PATH="$(gh_self_path)"

    # Unify the install root/name so every doctor reference below (and install.ps1's own
    # closing summary) points at ONE true path: $HOME/factories/guest-house/factory — unless
    # the caller explicitly gave --name/--root, in which case honor that and print the ACTUAL
    # resulting path instead of guessing. --target/--target-dir/--name/--port/--provider/
    # --base-branch/--skip-deps are all forwarded to the re-exec below (M4) — dropping them
    # silently used to always install the default target regardless of what was asked for.
    GH_WSL_NAME="$NAME"
    if [ -z "$GH_WSL_NAME" ]; then
        GH_WSL_NAME="guest-house"
    fi
    GH_WSL_ROOT="$ROOT"
    GH_WSL_INSTALL_DIR="$GH_WSL_ROOT/$GH_WSL_NAME/factory"

    echo
    if id factory >/dev/null 2>&1; then
        echo "[1/4] 'factory' user already exists — skipping creation"
    else
        echo "[1/4] Create the dedicated 'factory' Linux user"
        echo "  A separate, non-admin (no sudo grant) account inside this distro. Creating it"
        echo "  needs root ONCE, right now, for this one step only."
        if gh_confirm "Create the 'factory' user now (useradd -m factory)?"; then
            if [ "$(id -u)" -eq 0 ]; then
                useradd -m -s /bin/bash factory
            else
                sudo useradd -m -s /bin/bash factory
            fi
        else
            echo "ERROR: the 'factory' user is required for a guest-house install — aborting." >&2
            exit 1
        fi
    fi
    if id factory >/dev/null 2>&1; then
        GH_FACTORY_GROUPS=" $(id -nG factory 2>/dev/null || true) "
        case "$GH_FACTORY_GROUPS" in
            *" sudo "*|*" wheel "*)
                echo "ERROR: the 'factory' user is in the sudo/wheel group — guest-house secrets must" >&2
                echo "  never be bootstrapped into an account that can sudo." >&2
                echo "  Fix: sudo deluser factory sudo   (or: sudo gpasswd -d factory wheel)" >&2
                exit 1
                ;;
        esac
        GH_FACTORY_HOME="$(getent passwd factory | cut -d: -f6)"
        if [ -n "$GH_FACTORY_HOME" ]; then
            echo "  tightening $GH_FACTORY_HOME to mode 700 (no group/other access) ..."
            if [ "$(id -u)" -eq 0 ]; then
                chmod 700 "$GH_FACTORY_HOME" || echo "  WARNING: could not chmod $GH_FACTORY_HOME to 700"
            else
                sudo chmod 700 "$GH_FACTORY_HOME" || echo "  WARNING: could not chmod $GH_FACTORY_HOME to 700"
            fi
        fi
    fi

    echo
    echo "[2/4] Install the factory as 'factory' (brakes on: mode stays 'shift')"
    echo "  This runs the NORMAL installer (the 10-phase flow above), but AS 'factory', into the"
    echo "  unified guest-house path: $GH_WSL_INSTALL_DIR"
    echo "  'claude login' / 'gh auth login' are still manual, run as 'factory' (su - factory),"
    echo "  same as the runbook's fast-user-switch step on macOS."
    GH_WSL_INSTALL_OK=false
    if gh_confirm "Run the installer as 'factory' now?"; then
        GH_REEXEC_ARGS=(--factory-repo "$FACTORY_REPO" --branch "$BRANCH" --target "$TARGET"
                        --name "$GH_WSL_NAME" --root "$GH_WSL_ROOT" --port "$PORT")
        [ -n "$TARGET_DIR_NAME" ] && GH_REEXEC_ARGS+=(--target-dir "$TARGET_DIR_NAME")
        [ -n "$PROVIDER" ] && GH_REEXEC_ARGS+=(--provider "$PROVIDER")
        [ -n "$BASE_BRANCH" ] && GH_REEXEC_ARGS+=(--base-branch "$BASE_BRANCH")
        if [ "$SKIP_DEPS" = true ]; then
            GH_REEXEC_ARGS+=(--skip-deps)
        fi
        GH_INSTALL_RC=0
        if [ "$(id -un)" = "factory" ]; then
            bash "$SELF_PATH" "${GH_REEXEC_ARGS[@]}" || GH_INSTALL_RC=$?
        else
            sudo -u factory -H bash "$SELF_PATH" "${GH_REEXEC_ARGS[@]}" || GH_INSTALL_RC=$?
        fi
        if [ "$GH_INSTALL_RC" -eq 0 ]; then
            GH_WSL_INSTALL_OK=true
            echo "  engaging the safety brake (STOP) in the new deployment ..."
            if [ "$(id -un)" = "factory" ]; then
                touch "$GH_WSL_INSTALL_DIR/STOP" || echo "  WARNING: could not create STOP at $GH_WSL_INSTALL_DIR/STOP"
            else
                sudo -u factory -H bash -lc "touch '$GH_WSL_INSTALL_DIR/STOP'" || echo "  WARNING: could not create STOP at $GH_WSL_INSTALL_DIR/STOP"
            fi
        else
            echo "ERROR: the installer reported a failure (exit $GH_INSTALL_RC, see output above)." >&2
        fi
    else
        echo "  skipped — re-run this wizard when ready, or run the installer directly:"
        echo "    sudo -u factory -H bash $SELF_PATH --factory-repo $FACTORY_REPO --branch $BRANCH --target $TARGET --name $GH_WSL_NAME --root $GH_WSL_ROOT --port $PORT"
    fi

    echo
    echo "[3/4] Guest-house doctor"
    GH_WSL_DOCTOR="$GH_WSL_INSTALL_DIR/scripts/guesthouse_check.py"
    if [ "$(id -un)" = "factory" ]; then
        if [ ! -f "$GH_WSL_DOCTOR" ]; then
            echo "  doctor script not found at $GH_WSL_DOCTOR — install (step 2) has not completed yet."
            echo "  doctor could not run — this is NOT a FAIL, there is simply nothing installed yet."
        elif ! python3 "$GH_WSL_DOCTOR"; then
            echo "  doctor reported at least one FAIL above (a real run, non-zero exit) — see $GH_DOCS_BASE_URL/docs/runbooks/guest-house.md for fixes."
        fi
    else
        if ! sudo -u factory -H bash -lc "
            if [ ! -f '$GH_WSL_DOCTOR' ]; then
                echo '  doctor script not found at $GH_WSL_DOCTOR — install (step 2) has not completed yet.'
                echo '  doctor could not run — this is NOT a FAIL, there is simply nothing installed yet.'
                exit 0
            fi
            python3 '$GH_WSL_DOCTOR'
        "; then
            echo "  doctor reported at least one FAIL above (a real run, non-zero exit) — see $GH_DOCS_BASE_URL/docs/runbooks/guest-house.md for fixes."
        fi
    fi

    echo
    echo "[4/4] Summary"
    if [ "$GH_WSL_INSTALL_OK" = true ]; then
        GH_WSL_BRAKES_LINE="   brakes:  mode stays 'shift' (never auto); STOP was dropped by this wizard after install and stays engaged until removed on purpose."
    else
        GH_WSL_BRAKES_LINE="   brakes:  install did not complete this run, so STOP has NOT been set yet — re-run this wizard."
    fi
    cat <<SUMMARY
============================================================
 Guest-house install (WSL) — done.
$GH_WSL_BRAKES_LINE
   path:    $GH_WSL_INSTALL_DIR
   next:    $GH_DOCS_BASE_URL/docs/runbooks/factory-user-deployment.md §4 (supervised smoke
            shift) before any always-on/daemon step, then
            $GH_DOCS_BASE_URL/docs/runbooks/guest-house.md for the rules table.
   re-run the doctor any time:
     sudo -u factory -H bash -lc "python3 '$GH_WSL_DOCTOR'"
   EXPERIMENTAL: the Windows/WSL2 route has not yet been drill-tested on real Windows
            hardware — see install.ps1's header banner.
============================================================
SUMMARY
}

run_guest_house_wizard() {
    gh_preflight
    if [ "$WSL_MODE" != true ]; then
        # macOS guest-house install ALWAYS needs a real terminal — three unavoidable secrets
        # (sudo password, the new account's own password, a pasted GitHub token). --yes only
        # answers the wizard's OWN confirmations (block comment above); it cannot make this
        # path unattended.
        if ! gh_have_tty; then
            echo "ERROR: a guest-house macOS install always needs a real terminal (--yes cannot skip this)." >&2
            echo "  Three steps are genuinely interactive secrets: the sudo admin password, the new" >&2
            echo "  'factory' account's own login password, and pasting a GitHub token. None of them" >&2
            echo "  can safely come from a piped stdin (e.g. curl | bash)." >&2
            echo "  Run this from a real terminal: bash install.sh --guest-house" >&2
            exit 1
        fi
    else
        if ! gh_have_tty && [ "$YES" != true ]; then
            echo "ERROR: no terminal to prompt from (stdin is not a terminal, e.g. curl | bash) and --yes was not given." >&2
            echo "  re-run with --yes for a non-interactive install, or run this script from a real terminal." >&2
            exit 1
        fi
    fi
    trap gh_on_exit EXIT
    trap gh_on_interrupt INT TERM
    if [ "$WSL_MODE" = true ]; then
        gh_wizard_wsl
    else
        gh_wizard_mac
    fi
}

# Only when the (default) install command is what's being run — `install.sh list --guest-house`
# must run `list`, never hijack it into the wizard.
if [ "$GUEST_HOUSE" = true ] && [ "$CMD" = "install" ]; then
    run_guest_house_wizard
    exit 0
fi
# =============================================================================================
# end --guest-house wizard mode — the normal 10-phase flow below is BYTE-UNCHANGED by it.
# =============================================================================================

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
# Provider default follows the target's identity: clive gets its dedicated adapter; any
# other repo gets the config-driven GENERIC adapter (adapters/generic.py) so the eval loop
# is wired, not dormant. --provider overrides either way.
if [ -z "$PROVIDER" ]; then
    if [ "$TARGET_REPO_NAME" = "clive" ]; then
        PROVIDER="clive"
    else
        PROVIDER="generic"
    fi
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
# Eval-loop honesty notes. The develop rail (briefs -> worker -> tests -> gated merge) is
# target-generic either way; these are about the scenario-eval loop only.
case "$PROVIDER" in
    clive) if [ "$TARGET_REPO_NAME" != "clive" ]; then
        # Explicitly forcing the clive adapter onto a non-clive target (old scripts/CI
        # muscle memory) is exactly the predictably-unwired case — keep warning.
        cat >&2 <<NOTE
  NOTE: target '$TARGET_REPO_NAME' under the CLIVE adapter: the scenario-eval loop
  expects clive's layout and will predictably fail — use the default 'generic'
  provider (drop --provider) or write a dedicated adapter.
NOTE
    fi ;;
    generic) cat >&2 <<NOTE
  NOTE: target '$TARGET_REPO_NAME' runs under the GENERIC adapter: the eval loop
  invokes '<target.python> <target.entry> [target.exec.args]' with spec knobs as
  env vars. Set target.entry (and optionally the target.exec block) in the
  instance's config.yaml before the first eval run — the adapter never guesses an
  entry point. Write a dedicated adapter under factory/adapters/ (wired in
  common/config.py get_adapter) for richer, target-specific actuation.
NOTE
        ;;
    *) cat >&2 <<NOTE
  NOTE: provider '$PROVIDER' is only usable if you have registered its adapter in
  common/config.py get_adapter (shipped: 'clive', 'generic'). The scenario-eval
  loop needs that adapter.
NOTE
        ;;
esac

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
