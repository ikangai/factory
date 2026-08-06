#!/bin/bash
# Installs the publication broker as an OPERATOR LaunchAgent (Component G,
# docs/plans/2026-08-06-publication-broker-design.md): the operator-side half of the
# publication broker — it re-verifies and pushes envelopes the factory PREPARES, using
# the operator's OWN GitHub credential. Never a system LaunchDaemon, never the factory
# user, and NO ROOT is required — everything here runs as the invoking (operator) account.
#
# Creates the spool (outbox/receipts) + the local bare "publish" repo under
# /Users/Shared/factory-broker (the /Users/Shared/factory.git ownership-split precedent —
# see 01-create-user.sh step 3): the factory user writes envelopes / pushes candidate tips
# there, the operator (this script's own account) reads/pushes from there. Cross-user
# access rides the shared 'staff' group every regular macOS user account belongs to by
# default (same mechanism 01-create-user.sh already uses for the code-handoff bare repo) —
# NOT a chown to a literal 'factory:staff' owner, which would require root and defeats the
# "operator, no root" point of this script. `git init --bare --shared=group` is git's own
# native mechanism for exactly this: every object/ref it writes stays group-writable.
#
# Writes ~/.factory-broker.yaml (operator home, 600) if absent — the allowlist that is the
# actual authority on what the broker may push where (see orchestrator/broker.py). Installs
# com.factory.broker.plist as a LaunchAgent under gui/$UID, idempotently (bootout-then-
# bootstrap, mirroring 03-install-daemons.sh's own loop).
#
# run (as the OPERATOR, from their own factory checkout):
#   bash deploy/user-factory/04-install-broker-agent.sh [--dry-run] [--spool DIR]
#
# --dry-run prints every action without creating/chmod'ing/installing anything — the
# honest way to review this on a host where `launchctl bootstrap` can't be exercised for
# real (e.g. CI, or a review pass with no GUI session).
set -euo pipefail

DRY_RUN=false
SPOOL_ROOT="/Users/Shared/factory-broker"
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --spool)   SPOOL_ROOT="$2"; shift 2 ;;
        *)         echo "ERROR: unknown argument: $1" >&2
                   echo "  usage: $0 [--dry-run] [--spool DIR]" >&2
                   exit 2 ;;
    esac
done

# --- 0. identity: must be the OPERATOR — not root, not the factory user -----------------
if [ "$EUID" -eq 0 ]; then
    echo "ERROR: run this as the OPERATOR's own account, not root (no sudo needed)." >&2
    echo "  run: bash deploy/user-factory/04-install-broker-agent.sh" >&2
    exit 1
fi
if [ "$(id -un)" = "factory" ]; then
    echo "ERROR: run this as the OPERATOR, not the 'factory' user — the whole point of the" >&2
    echo "  broker is a credential the factory user never holds." >&2
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
FACTORY_CHECKOUT="$(cd "$HERE/../.." && pwd)"
OPERATOR_HOME="$HOME"
OUTBOX_DIR="$SPOOL_ROOT/outbox"
RECEIPTS_DIR="$SPOOL_ROOT/receipts"
BARE_REPO="$SPOOL_ROOT/clive-publish.git"
ALLOWLIST="$OPERATOR_HOME/.factory-broker.yaml"
AGENTS_DIR="$OPERATOR_HOME/Library/LaunchAgents"
PLIST_DEST="$AGENTS_DIR/com.factory.broker.plist"
LABEL="com.factory.broker"

_run() {
    # A single choke point for every mutating action, so --dry-run prints EXACTLY what
    # would run without a second "if dry_run" at every call site.
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] $*"
    else
        "$@"
    fi
}

echo "== publication broker install: checkout=$FACTORY_CHECKOUT operator=$(id -un) =="
[ "$DRY_RUN" = true ] && echo "   (--dry-run: no changes will be made)"

# --- 1. the spool: outbox + receipts + receipts/done, group-shared with 'staff' ----------
echo "[1/5] spool at $SPOOL_ROOT ..."
for d in "$SPOOL_ROOT" "$OUTBOX_DIR" "$OUTBOX_DIR/done" "$RECEIPTS_DIR" "$RECEIPTS_DIR/done"; do
    if [ -d "$d" ]; then
        echo "  $d already exists — skipping"
    else
        # 2775: setgid so files/dirs CREATED inside inherit the 'staff' group, +
        # group-writable — the factory user (also 'staff' by default) can then write the
        # outbox and read receipts/done without ever owning this directory tree.
        _run install -d -m 2775 -g staff "$d"
    fi
done

# --- 2. the local bare "publish" repo: factory pushes candidate tips here, broker pushes
#        FROM here to the real remote. --shared=group is git's own mechanism for exactly
#        this cross-user layout (every object it writes stays group-writable). ------------
echo "[2/5] bare repo $BARE_REPO ..."
if [ -d "$BARE_REPO" ]; then
    echo "  already initialized — skipping"
else
    _run git init --bare --shared=group -q "$BARE_REPO"
    _run chmod -R g+rwX "$BARE_REPO"
fi

# --- 3. the allowlist — ~/.factory-broker.yaml, 600, operator home. THE authority: the
#        envelope only ever REQUESTS; this file (never any envelope field) says what may
#        actually be pushed where. Best-effort pre-fill from the factory checkout's own
#        config.yaml; the operator MUST review/complete remote_url by hand before arming. --
echo "[3/5] allowlist $ALLOWLIST ..."
if [ -f "$ALLOWLIST" ]; then
    echo "  already exists — leaving it alone (edit by hand to add/change entries)"
else
    REPO_SLUG=""
    BASE_BRANCH="chore/extract-factory"
    if command -v python3 >/dev/null 2>&1; then
        READ_CFG="$(python3 - "$FACTORY_CHECKOUT/config.yaml" <<'PYEOF' 2>/dev/null || true
import sys
try:
    import yaml
except ImportError:
    sys.exit(0)
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
except OSError:
    sys.exit(0)
target = dict(cfg.get("target") or {})
legacy = dict(cfg.get("clive") or {})
merged = dict(legacy); merged.update(target)
print(merged.get("repo", ""))
print(merged.get("base_branch", "chore/extract-factory"))
PYEOF
)"
        REPO_SLUG="$(echo "$READ_CFG" | sed -n '1p')"
        BASE_BRANCH="$(echo "$READ_CFG" | sed -n '2p')"
        [ -n "$BASE_BRANCH" ] || BASE_BRANCH="chore/extract-factory"
    fi
    ALLOWLIST_BODY="$(cat <<YAMLEOF
# ~/.factory-broker.yaml — THE authority on what orchestrator/broker.py may push where.
# The factory's envelope only ever REQUESTS a publication; every field is cross-checked
# against the matching entry below (repo_slug + base_branch), live git state, and a
# fast-forward check — no entry here => the broker refuses the envelope outright.
# 600 permissions, operator home only. See docs/runbooks/publication-broker.md.
publications:
  - repo_slug: "${REPO_SLUG:-CHANGE-ME/repo}"       # e.g. ikangai/clive
    remote_url: "CHANGE-ME"                          # e.g. git@github.com:ikangai/clive.git
                                                       # (the broker pushes here with YOUR
                                                       # own git credential — never the
                                                       # factory user's)
    base_branch: "${BASE_BRANCH}"
    bare_path: "${BARE_REPO}"
    allow_issue_ops: true
#  - repo_slug: "CHANGE-ME/repo"                     # a second entry for target.release_branch
#    remote_url: "CHANGE-ME"                         # (promotion envelopes, action=promote)
#    base_branch: "main"
#    bare_path: "${BARE_REPO}"
#    allow_issue_ops: false
YAMLEOF
)"
    if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] would write $ALLOWLIST (600):"
        while IFS= read -r line; do echo "    $line"; done <<< "$ALLOWLIST_BODY"
    else
        umask 077
        printf '%s\n' "$ALLOWLIST_BODY" > "$ALLOWLIST"
        chmod 600 "$ALLOWLIST"
        echo "  wrote $ALLOWLIST — REVIEW remote_url before arming (still says CHANGE-ME)"
    fi
fi

# --- 4. the LaunchAgent: gui/$UID, bootout-then-bootstrap (idempotent, mirrors
#        03-install-daemons.sh's own loop) — never a system LaunchDaemon. --------------
echo "[4/5] LaunchAgent $LABEL ..."
_run install -d -m 755 "$AGENTS_DIR"
UID_N="$(id -u)"
if launchctl print "gui/$UID_N/$LABEL" >/dev/null 2>&1; then
    echo "  $LABEL is loaded — booting out ..."
    _run launchctl bootout "gui/$UID_N/$LABEL"
else
    echo "  $LABEL is not currently loaded"
fi
if [ "$DRY_RUN" = true ]; then
    echo "  [dry-run] would render $HERE/com.factory.broker.plist -> $PLIST_DEST"
    echo "  [dry-run]   __FACTORY_CHECKOUT__ -> $FACTORY_CHECKOUT"
    echo "  [dry-run]   __OPERATOR_HOME__    -> $OPERATOR_HOME"
    echo "  [dry-run]   __OUTBOX_DIR__       -> $OUTBOX_DIR"
    echo "  [dry-run] would: launchctl bootstrap gui/$UID_N $PLIST_DEST"
else
    sed -e "s#__FACTORY_CHECKOUT__#$FACTORY_CHECKOUT#g" \
        -e "s#__OPERATOR_HOME__#$OPERATOR_HOME#g" \
        -e "s#__OUTBOX_DIR__#$OUTBOX_DIR#g" \
        "$HERE/com.factory.broker.plist" > "$PLIST_DEST"
    chmod 644 "$PLIST_DEST"
    launchctl bootstrap "gui/$UID_N" "$PLIST_DEST"
    echo "  installed + bootstrapped $LABEL"
fi

# --- 5. next steps ------------------------------------------------------------------------
echo "[5/5] done."
cat <<EOF

============================================================
 Broker spool + LaunchAgent installed (or previewed, with --dry-run).

 BEFORE arming (autonomy.publication_broker: true in the factory's config.yaml):
   1. Edit $ALLOWLIST — set the REAL remote_url(s) (still CHANGE-ME by default).
   2. Verify: bin/factory broker status   (from $FACTORY_CHECKOUT)
   3. Remove GH_TOKEN from the factory user's secrets (runbook §4 — this is the actual
      credential-removal step; this script does NOT touch the factory user's account):
        ~factory/.factory-secrets/env  — comment out/remove the GH_TOKEN line
        sudo -u factory -i gh auth logout   (drops the git credential helper too)
   4. Run the drill-3 procedure (runbook §6) before trusting this in production.

 Full topology, arming steps, receipt semantics, and teardown:
   docs/runbooks/publication-broker.md
============================================================
EOF
