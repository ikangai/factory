"""Shared sanitation for untrusted/free text embedded into structured surfaces."""
from __future__ import annotations


def clean_line(text: str, cap: int = 140) -> str:
    """Normalize untrusted/free text to inert single-line data: printable chars only,
    whitespace collapsed, length-capped — used for prompt-injected issue titles and
    git-trailer values. Semantic injection is handled by the surface's own contract
    (prompt framing / trailer parsing); this kills the mechanical vectors (control &
    format chars, embedded newlines, walls of text) that could restructure the surface
    the text is embedded in."""
    t = "".join(ch for ch in str(text) if ch.isprintable())
    return " ".join(t.split())[:cap]
