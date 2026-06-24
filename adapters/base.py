"""Target Adapter interface — the seam that makes the factory repo-agnostic.

The factory optimises a TARGET program by proposing config changes, actuating a
candidate spec into the target's real runtime knobs, running the target toward a
goal in a disposable environment, and grading the world result. Everything about
*which* target that is — how a spec actuates, how the target is invoked as a
subprocess, how a session is recovered from its output — lives behind this
interface. `clive` is the first (and default) adapter; pointing the factory at a
different repo = writing a new adapter + setting `target.provider` in config.yaml.

The interface is intentionally small: it captures ONLY the seam the deterministic
runner already consumes (spec actuation + target invocation + session recovery +
the few env helpers the multi-target path needs). It returns the EXISTING types
the runner already handles (AppliedSpec, CliveResult), so wiring the runner
through an adapter is a near-no-op rename — no new behaviour, no new data shapes.
"""
from __future__ import annotations

import abc
from typing import Any, Optional

# The runner consumes these existing shapes; the adapter returns them unchanged
# so the seam is a pure indirection (YAGNI: no adapter-specific result type).
from ..common.spec_applier import AppliedSpec
from ..common.clive_invoke import CliveResult


class TargetAdapter(abc.ABC):
    """How the factory actuates a spec into, and invokes, ONE target program.

    Implementations wrap the target's invocation logic. The clive adapter
    delegates to the existing common.spec_applier / common.clive_invoke so its
    behaviour is byte-for-byte identical to the pre-adapter code path.
    """

    name = "base"

    # -- spec actuation -----------------------------------------------------
    @abc.abstractmethod
    def actuate(self, spec: dict, run_dir: str,
                default_toolset: str = "minimal") -> AppliedSpec:
        """Render a candidate spec's `open` block into the target's runtime knobs
        (env vars + CLI flags). Returns the existing AppliedSpec shape the runner
        already records (.env, .flags, .pending, .notes)."""

    # -- target invocation --------------------------------------------------
    @abc.abstractmethod
    def run(self, goal: str, *, applied_env: dict[str, str],
            applied_flags: list[str], env_vars: dict[str, str],
            model_entry: dict, max_tokens: int, timeout_s: int,
            cwd: Optional[str] = None) -> CliveResult:
        """Invoke the target as a subprocess toward `goal` under the actuated
        spec + panel model, bounded by max_tokens/timeout_s. Returns CliveResult
        (rc/stdout/stderr/duration_s/timed_out/argv/env_overrides)."""

    # -- session/evidence recovery ------------------------------------------
    @abc.abstractmethod
    def parse_session_dirs(self, text: str) -> list[str]:
        """Recover this run's target-session dir(s) from its captured output, so
        evidence collection is scoped to THIS run."""

    # -- helpers the multi-target (clive-to-clive) path needs ---------------
    @abc.abstractmethod
    def scrub_env(self, env: dict[str, str]) -> None:
        """In-place: drop host creds + dangerous target flags a candidate must
        not inherit (the LLM provider key is intentionally kept)."""

    @abc.abstractmethod
    def panel_env(self, model_entry: dict) -> dict[str, str]:
        """Map a panel model entry to the target's model-selection env vars."""

    @abc.abstractmethod
    def entry(self) -> tuple[str, str]:
        """Return (target_root, target_entry_abs_path) for direct subprocess
        spawns (the multi-target path launches the target itself)."""

    @abc.abstractmethod
    def interpreter(self) -> str:
        """Return the interpreter path used to run the target."""
