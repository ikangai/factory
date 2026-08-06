"""Canonical filesystem paths for the factory. Everything is files + a SQLite
store, so path resolution is centralized here. No hidden state."""
from __future__ import annotations

import os
from typing import Optional

# factory/common/paths.py -> factory/
FACTORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# NOTE: vestigial. The target repo root comes from config.clive.root
# (resolve_clive_root), NOT this constant — the factory is repo-agnostic now.
CLIVE_ROOT = os.path.dirname(FACTORY_ROOT)


def factory(*parts: str) -> str:
    return os.path.join(FACTORY_ROOT, *parts)


# Core locations
CONFIG_YAML = factory("config.yaml")
PANEL_YAML = factory("panel.yaml")
STORE_DIR = factory("store")
DB_PATH = factory("store", "blackboard.db")
SCHEMA_SQL = factory("store", "schema.sql")
SPECS_DIR = factory("specs")
CHAMPION_YAML = factory("specs", "champion.yaml")
CANDIDATES_DIR = factory("specs", "candidates")
SCENARIOS_DIR = factory("scenarios")
WORKING_DIR = factory("scenarios", "working")
HELD_OUT_DIR = factory("scenarios", "held-out")
STAGING_DIR = factory("scenarios", "staging")
CHECKS_DIR = factory("checks")
LOGS_DIR = factory("logs")
RUNS_DIR = factory("logs", "runs")
ROLES_DIR = factory("roles")
# Researcher role: grounded literature briefs, staged for operator vetting.
RESEARCH_DIR = factory("research")
RESEARCH_STAGING_DIR = factory("research", "staging")


def run_evidence_dir(run_id: str) -> str:
    """Per-run evidence directory under logs/runs/<run_id>/."""
    d = os.path.join(RUNS_DIR, run_id)
    os.makedirs(d, exist_ok=True)
    return d


def resolve_clive_root(config_root: str) -> str:
    """Resolve the clive repo root from config (relative to factory/)."""
    if os.path.isabs(config_root):
        return os.path.normpath(config_root)
    return os.path.normpath(os.path.join(FACTORY_ROOT, config_root))


# -- publication broker spool (docs/plans/2026-08-06-publication-broker-design.md,
# Component B) — the FIRST out-of-tree paths in this module. The broker runs as the
# OPERATOR, a different OS user than the factory in production, so its spool cannot live
# under FACTORY_ROOT there (`/Users/Shared/factory-broker/` — the `/Users/Shared/
# factory.git` ownership-split precedent). Every function below resolves to the
# dev/single-user default (state/broker/** inside FACTORY_ROOT — hermetically testable
# with file:// remotes and tmp dirs) UNLESS an explicit `root` override or the matching
# `FACTORY_BROKER_*` env var says otherwise, so production deploys point both the
# factory-side writer and the operator-side broker at the shared split with an env var,
# never a code change. Functions (not module-level constants): a constant frozen at
# import time can't be overridden per-test by an env var set after import; a function
# reads it fresh every call, and a test can also just pass `root=str(tmp_path)` directly.
def broker_spool_root(root: Optional[str] = None) -> str:
    if root:
        return root
    return os.environ.get("FACTORY_BROKER_SPOOL") or factory("state", "broker")


def broker_outbox_dir(root: Optional[str] = None) -> str:
    return os.path.join(broker_spool_root(root), "outbox")


def broker_receipts_dir(root: Optional[str] = None) -> str:
    return os.path.join(broker_spool_root(root), "receipts")


def broker_receipts_done_dir(root: Optional[str] = None) -> str:
    return os.path.join(broker_receipts_dir(root), "done")


def broker_bare_repo(root: Optional[str] = None) -> str:
    """The bare repo the factory pushes its candidate tip to (a local file:// remote — no
    credential) and the broker pushes FROM to the real origin. `FACTORY_BROKER_BARE`
    overrides independently of the spool root (Component B: in production it is its own
    top-level path, `clive-publish.git`, sibling to — not nested under — outbox/receipts)."""
    override = os.environ.get("FACTORY_BROKER_BARE")
    if override:
        return override
    return os.path.join(broker_spool_root(root), "clive-publish.git")


def broker_allowlist_path(path: Optional[str] = None) -> str:
    """The operator's own allowlist (`~/.factory-broker.yaml`, 600) — the sole authority
    on what the broker may push where (the envelope only ever REQUESTS). Never under
    FACTORY_ROOT: it must survive/differ independent of the factory checkout."""
    if path:
        return path
    return os.environ.get("FACTORY_BROKER_ALLOWLIST") or os.path.expanduser(
        "~/.factory-broker.yaml")
