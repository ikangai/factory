"""Clive target adapter — the default, and the reference implementation.

This wraps the EXISTING common.spec_applier + common.clive_invoke logic so the
clive code path is byte-for-byte identical to the pre-adapter factory: every
method delegates straight to the original function. The adapter adds indirection,
not behaviour. Notably it does NOT touch the claude-cli isolation — that is built
inside clive_invoke.build (the isolated `claude -p` env), and delegating preserves
it exactly.
"""
from __future__ import annotations

from typing import Optional

from .base import TargetAdapter
from ..common import clive_invoke, config, spec_applier
from ..common.spec_applier import AppliedSpec
from ..common.clive_invoke import CliveResult


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
