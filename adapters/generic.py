"""Generic target adapter — the second registered adapter, fully config-driven
(docs/plans/2026-07-09-generic-adapter-design.md).

Makes the scenario-eval loop runnable against ANY repo invocable as
`<interpreter> <entry> [args…]`: candidate spec knobs actuate as env vars, the goal
lands in an argv template, and the panel model maps to env via config — no code per
target. The conventions live in a `target.exec` block (see config.yaml); richer
actuation (files, flags, target-specific semantics) is exactly what a DEDICATED
adapter is for — this one never guesses.

The develop rail needs none of this: TargetAdapter's base defaults (git helpers,
config-driven frozen_paths/test_command) are already target-generic.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Optional

from .base import TargetAdapter
from ..common import clive_invoke, config
from ..common.spec_applier import AppliedSpec
from ..common.clive_invoke import CliveResult


def _exec_cfg() -> dict:
    return dict(config.target_config().get("exec") or {})


class GenericAdapter(TargetAdapter):
    name = "generic"

    # -- spec actuation -----------------------------------------------------
    def actuate(self, spec: dict, run_dir: str,
                default_toolset: str = "minimal") -> AppliedSpec:
        """Open-block SCALARS -> `<prefix><KEY upper>` env vars (bools as "1"/"0" —
        subprocess env is strings). Nested dicts/lists are DECLARED but not actuatable
        generically: recorded as pending + a note, never silently dropped, so the
        reporter's evidence trail shows exactly what a candidate asked for that the
        target never saw. `default_toolset` is a clive concept — unused here."""
        out = AppliedSpec()
        prefix = str(_exec_cfg().get("spec_env_prefix", "FACTORY_"))
        for key, value in (spec.get("open") or {}).items():
            if isinstance(value, bool):
                out.env[prefix + key.upper()] = "1" if value else "0"
            elif isinstance(value, (str, int, float)):
                out.env[prefix + key.upper()] = str(value)
            else:
                out.pending.append(key)
        if out.pending:
            out.notes.append(
                f"open keys not actuatable generically (write a dedicated adapter): "
                f"{', '.join(out.pending)}")
        return out

    # -- target invocation --------------------------------------------------
    def run(self, goal: str, *, applied_env: dict[str, str],
            applied_flags: list[str], env_vars: dict[str, str],
            model_entry: dict, max_tokens: int, timeout_s: int,
            cwd: Optional[str] = None, clive_root: Optional[str] = None,
            clive_py: Optional[str] = None) -> CliveResult:
        """argv = [interpreter, <root>/<entry>, *flags, *args template]. `clive_root`
        is the seam's name for "grade THIS candidate checkout" — it swaps the SOURCE
        the entry runs from (the real-merge-grade path depends on it); `cwd` only
        moves the working directory."""
        cfg = config.target_config()
        ex = _exec_cfg()
        root = clive_root or config.clive_entry()[0]
        entry_rel = cfg.get("entry") or ""
        entry_abs = os.path.join(root, entry_rel)
        if not entry_rel or not os.path.exists(entry_abs):
            raise FileNotFoundError(
                f"generic adapter: target.entry not found at {entry_abs!r} — set "
                f"target.entry (and optionally target.exec) in config.yaml to the "
                f"target's entry point")
        interp = clive_py or self.interpreter()

        # {goal} substitutes anywhere in the template; a template with no placeholder
        # gets the goal appended so it is never silently dropped.
        template = [str(a) for a in (ex.get("args") or ["{goal}"])]
        rendered = [a.replace("{goal}", goal) for a in template]
        if not any("{goal}" in a for a in template):
            rendered.append(goal)
        argv = [interp, entry_abs, *applied_flags, *rendered]

        env = dict(os.environ)
        self.scrub_env(env)                      # host creds never reach a candidate
        overrides: dict[str, str] = {}
        overrides.update(env_vars)               # sandbox handle (workdir/home wiring)
        overrides.update(applied_env)            # the actuated candidate spec
        overrides.update(self.panel_env(model_entry))
        tokens_env = ex.get("max_tokens_env", "FACTORY_MAX_TOKENS")
        if tokens_env:
            overrides[tokens_env] = str(max_tokens)
        env.update(overrides)

        start = time.time()
        timed_out = False
        try:
            p = subprocess.run(argv, cwd=cwd or root, env=env, capture_output=True,
                               text=True, timeout=timeout_s)
            rc, stdout, stderr = p.returncode, p.stdout or "", p.stderr or ""
        except subprocess.TimeoutExpired as e:
            timed_out = True
            rc = 124
            stdout = (e.stdout or b"").decode("utf-8", "replace") \
                if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr or b"").decode("utf-8", "replace") \
                if isinstance(e.stderr, bytes) else (e.stderr or "")
        return CliveResult(rc=rc, stdout=stdout, stderr=stderr,
                           duration_s=time.time() - start, timed_out=timed_out,
                           argv=argv, env_overrides=overrides)

    # -- session/evidence recovery ------------------------------------------
    def parse_session_dirs(self, text: str) -> list[str]:
        """Config-declared regex over the run's output; no regex -> no session dirs
        (evidence collection then scopes to the sandbox workdir, which the runner
        already captures)."""
        pattern = _exec_cfg().get("session_dir_regex") or ""
        if not pattern:
            return []
        try:
            return re.findall(pattern, text)
        except re.error:
            return []

    # -- env helpers ----------------------------------------------------------
    def scrub_env(self, env: dict[str, str]) -> None:
        """Reuse the shared host-cred scrub (AWS/GH/Docker/… tokens + dangerous target
        flags). It lives in clive_invoke for historical reasons but is not
        clive-specific in effect; sharing it keeps ONE blocklist to maintain."""
        clive_invoke._scrub_env(env)

    def panel_env(self, model_entry: dict) -> dict[str, str]:
        """Config-mapped: `target.exec.model_env` maps model-entry keys (model/
        provider/base_url/…) to the TARGET's env var names. Empty mapping = the
        panel model is not actuated (noted nowhere — a target without model env
        conventions simply runs as configured)."""
        mapping = dict(_exec_cfg().get("model_env") or {})
        return {str(target_var): str(model_entry[key])
                for key, target_var in mapping.items() if model_entry.get(key)}

    # -- paths ------------------------------------------------------------------
    def entry(self) -> tuple[str, str]:
        # config.clive_entry reads the RESOLVED target config (root/entry) — the name
        # is legacy, the behaviour is target-generic.
        return config.clive_entry()

    def interpreter(self) -> str:
        return config.clive_python()

    # -- code-development seam -----------------------------------------------
    def test_command(self) -> list[str]:
        """`target.test_command` if set, else the pytest convention (mirrors clive)."""
        cfg = config.target_config().get("test_command")
        if cfg:
            return list(cfg)
        return [self.interpreter(), "-m", "pytest", "tests/", "-q"]
