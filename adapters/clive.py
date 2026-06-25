"""Clive target adapter — the default, and the reference implementation.

This wraps the EXISTING common.spec_applier + common.clive_invoke logic so the
clive code path is byte-for-byte identical to the pre-adapter factory: every
method delegates straight to the original function. The adapter adds indirection,
not behaviour. Notably it does NOT touch the claude-cli isolation — that is built
inside clive_invoke.build (the isolated `claude -p` env), and delegating preserves
it exactly.
"""
from __future__ import annotations

import ast
import os
from typing import Optional

from .base import TargetAdapter
from ..common import clive_invoke, config, spec_applier
from ..common.spec_applier import AppliedSpec
from ..common.clive_invoke import CliveResult

# clive source lives under <repo>/src/clive/; .clive/ governance + .env are at the root.
_SRC_PREFIX = "src/clive"
# ALWAYS frozen, regardless of clive's tier table — the command-safety INVOCATION
# surface, not just its definition (review 2026-06-25 blocker: freezing runtime.py
# alone let the factory keep the gate but delete the `_check_command_safety()` CALL in
# an editable runner). So freeze the whole execution/ dir (every runner that invokes
# the gate), the sandbox, the discovery credential guard, the bare-name import shims,
# and the safety TESTS (the worker can't delete them to keep the suite green).
_SAFETY_EXTRAS = (
    f"{_SRC_PREFIX}/execution/",                 # runtime.py + executor.py + all runners
    f"{_SRC_PREFIX}/sandbox/run.sh",
    f"{_SRC_PREFIX}/discovery/explorer.py",      # aws/gh/ssh exploration block
    f"{_SRC_PREFIX}/runtime.py",                 # bare-name import shim → execution/runtime
    f"{_SRC_PREFIX}/executor.py",                # bare-name import shim → execution/executor
    "tests/test_command_safety.py",
    "tests/test_runner_safety_parity.py",
    "tests/test_sandbox.py",
    "tests/test_selfmod_gate.py",
    "tests/test_selfmod_gate_ast.py",
)
# Tiers the factory may never touch — the rest (CORE/STANDARD/OPEN) it may develop.
_FROZEN_TIERS = ("IMMUTABLE", "GOVERNANCE")


def _extract_file_tiers(constitution_src: str) -> list:
    """Pull the FILE_TIERS literal out of clive's selfmod/constitution.py by AST (no
    importing clive). Returns [] if it can't be parsed."""
    try:
        tree = ast.parse(constitution_src)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        # Handle both `FILE_TIERS = [...]` (Assign) and the annotated
        # `FILE_TIERS: list[...] = [...]` (AnnAssign — clive's real form).
        value = None
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "FILE_TIERS" for t in node.targets):
            value = node.value
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
              and node.target.id == "FILE_TIERS"):
            value = node.value
        if value is not None:
            try:
                return list(ast.literal_eval(value))
            except (ValueError, SyntaxError):
                return []
    return []


def _resolve_clive_pattern(pattern: str) -> str:
    """clive's FILE_TIERS patterns are relative to PROJECT_ROOT (src/clive/), except
    repo-root dotfiles/dirs (.clive, .env, .github). Resolve to repo-root-relative."""
    if pattern.startswith("."):
        return pattern
    return f"{_SRC_PREFIX}/{pattern}"


def _frozen_from_constitution(constitution_src: str) -> list[str]:
    """clive's frozen surface = its IMMUTABLE + GOVERNANCE FILE_TIERS (resolved to
    repo-root paths) + the always-frozen command-blocklist/sandbox. Even if the
    constitution can't be parsed, the hard safety files are still frozen."""
    frozen = set(_SAFETY_EXTRAS)
    for entry in _extract_file_tiers(constitution_src):
        try:
            pattern, tier = entry
        except (ValueError, TypeError):
            continue
        if tier in _FROZEN_TIERS:
            frozen.add(_resolve_clive_pattern(pattern))
    return sorted(frozen)


class CliveAdapter(TargetAdapter):
    name = "clive"

    def actuate(self, spec: dict, run_dir: str,
                default_toolset: str = "minimal") -> AppliedSpec:
        return spec_applier.apply_spec(spec, run_dir, default_toolset)

    def run(self, goal: str, *, applied_env, applied_flags, env_vars,
            model_entry, max_tokens: int, timeout_s: int,
            cwd: Optional[str] = None) -> CliveResult:
        return clive_invoke.run(
            goal, applied_env=applied_env, applied_flags=applied_flags,
            env_vars=env_vars, model_entry=model_entry,
            max_tokens=max_tokens, timeout_s=timeout_s, cwd=cwd)

    def parse_session_dirs(self, text: str) -> list[str]:
        return clive_invoke.parse_session_dirs(text)

    def scrub_env(self, env: dict) -> None:
        clive_invoke._scrub_env(env)

    def panel_env(self, model_entry: dict) -> dict:
        return clive_invoke.panel_env(model_entry)

    def entry(self) -> tuple[str, str]:
        return config.clive_entry()

    def interpreter(self) -> str:
        return config.clive_python()

    # -- code-development seam: derive the frozen surface from clive's constitution --
    def frozen_paths(self) -> list[str]:
        """Read clive's OWN constitution (selfmod/constitution.py's FILE_TIERS) and
        freeze its IMMUTABLE + GOVERNANCE tiers + the command-blocklist/sandbox. Stays
        in sync with clive's governance; falls back to the config list if unreadable."""
        root, _ = config.clive_entry()
        const = os.path.join(root, _SRC_PREFIX, "selfmod", "constitution.py")
        try:
            with open(const, "r", encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            return super().frozen_paths()
        return _frozen_from_constitution(src)

    def test_command(self) -> list[str]:
        """clive's own suite: `<python> -m pytest tests/ -q` (conftest puts src/ on the
        path). Overridable via `target.test_command`."""
        cfg = config.target_config().get("test_command")
        if cfg:
            return list(cfg)
        return [config.clive_python(), "-m", "pytest", "tests/", "-q"]
