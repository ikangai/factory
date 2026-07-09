"""Task 8 (design: docs/plans/2026-07-08-factory-owned-bus-human-queue.md): every prompt that
tells a super-worker to post on the team bus must carry the vendored send command EXPLICITLY —
the deployed factory user has no agora-plugin SessionStart hook to draw a `send` command/handle
from (that briefing only exists when the plugin is installed). Hermetic: reads the prompt.md
files directly off disk, no live agent."""
import os

from factory.common import paths

_BUS_ROLES = ("developer", "research_feed", "conductor")


def _read_prompt(role: str) -> str:
    with open(os.path.join(paths.ROLES_DIR, role, "prompt.md"), encoding="utf-8") as fh:
        return fh.read()


def test_bus_posting_prompts_state_the_vendored_send_command():
    for role in _BUS_ROLES:
        text = _read_prompt(role)
        assert "vendor/agora/chat.py send" in text, (
            f"{role}/prompt.md must state the vendored send command explicitly")


def test_bus_posting_prompts_no_longer_claim_a_sessionstart_briefing():
    for role in _BUS_ROLES:
        text = _read_prompt(role)
        assert "SessionStart briefing has the" not in text, (
            f"{role}/prompt.md still claims a SessionStart briefing carries the send command — "
            "the deployed factory user has no agora-plugin hook to provide one")
