"""Mission-driven research focus (genericity).

The researcher's topic is target/mission-driven, not hardcoded to clive's CLI-agent
focus. The operator steers it by editing MISSION.md's `## Research focus` section;
clive's CLI-agent default (arxiv.DEFAULT_QUERY) is only the fallback.

Parsing is deterministic, dependency-free, and never crashes — a missing or
unreadable MISSION.md just yields None (caller falls back to the default query)."""
from __future__ import annotations

import re


def _section_body(text: str, heading: str) -> str | None:
    """Return the body text under a markdown `## heading` (case-insensitive,
    matching the leading words of the heading line), up to the next `##`/`#`
    heading or end of file. None if the heading is absent or its body is empty."""
    # Match `## Heading...` then capture everything until the next top/2nd-level
    # heading. The heading line may carry a parenthetical (e.g. "Research focus
    # (overrides the default query)"), so anchor on the heading words only.
    pat = re.compile(
        rf"^\#{{1,3}}\s*{re.escape(heading)}\b[^\n]*\n(.*?)(?=^\#{{1,3}}\s|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pat.search(text)
    if not m:
        return None
    # Drop HTML comments (the template uses <!-- ... --> hints) and blank lines.
    body = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.DOTALL)
    body = body.strip()
    return body or None


def read_research_focus(mission_path: str) -> str | None:
    """Parse MISSION.md and return the research focus string, or None.

    Preference: `## Research focus` (the operator's explicit topic), then falling
    back to `## Mission`. Returns None if neither has usable content or the file
    can't be read — the caller then uses the deterministic DEFAULT_QUERY."""
    try:
        with open(mission_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None

    for heading in ("Research focus", "Mission"):
        body = _section_body(text, heading)
        if body:
            # Collapse to a single line of search-friendly focus text.
            return " ".join(body.split())
    return None
