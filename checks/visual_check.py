"""Deterministic dashboard self-check (roadmap Task 0.6, pattern P9 inverted).

The one recorded visual failure class — a syntax error in the board's inline
<script> silently freezes the page while the server stays green — is fully
catchable WITHOUT a browser or an LLM: extract the inline JS and `node --check`
it, then scan for raw `{PLACEHOLDER}` braces (an unfilled template seam renders
as literal text) and for the named tab sections the page's own JS drives.

Zero tokens, zero network. `node` absent is a REPORTED skip of the JS gate, not
a failure — the placeholder/section scans still run and still gate. Exposed on
the CLI as `factory viz --selfcheck` (exit 1 on failure).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from ..common import paths

# The board's navigational skeleton: the tab <section id="…"> nodes the inline JS
# activates. A missing one means a whole surface silently vanished from the page.
REQUIRED_SECTIONS = (
    "tab-queue", "tab-plan", "tab-execution", "tab-resources",
    "tab-timesheets", "tab-finance", "tab-research", "tab-report",
)

DEFAULT_PAGE = os.path.join(paths.FACTORY_ROOT, "dashboard", "static", "fleet.html")

_SCRIPT_RE = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
                        re.IGNORECASE | re.DOTALL)
# A raw uppercase template seam like {MISSION} left unfilled in the served HTML.
# The (?<!\$) guard excludes JS template literals (`${LIVE_OK}`), which are code.
_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{[A-Z][A-Z0-9_]{2,}\}")


def extract_scripts(html: str) -> list[tuple[int, str]]:
    """Every INLINE <script> body as (1-indexed html line of the tag, body).

    External `<script src=…>` blocks are skipped — not ours to syntax-check."""
    out = []
    for m in _SCRIPT_RE.finditer(html):
        if re.search(r"\bsrc\s*=", m.group("attrs"), re.IGNORECASE):
            continue
        line = html.count("\n", 0, m.start()) + 1
        out.append((line, m.group("body")))
    return out


def find_placeholders(html: str) -> list[str]:
    """Raw {PLACEHOLDER} braces in the page, deduped, first-seen order."""
    seen: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(html):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


def missing_sections(html: str, required=REQUIRED_SECTIONS) -> list[str]:
    return [sec for sec in required if f'id="{sec}"' not in html]


def node_check(js: str, node_bin: str = "node") -> tuple[bool, str]:
    """`node --check` one script body → (ok, error). Caller ensures node exists."""
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(js)
        proc = subprocess.run([node_bin, "--check", path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode == 0:
        return True, ""
    err = (proc.stderr or proc.stdout or "").strip()
    m = re.search(re.escape(path) + r":(\d+)", err)
    js_line = int(m.group(1)) if m else 0
    detail = next((ln.strip() for ln in err.splitlines() if "Error" in ln),
                  err.splitlines()[-1].strip() if err else "node --check failed")
    return False, f"js line {js_line or '?'}: {detail}"


def check_dashboard(html_path: str | None = None, node_bin: str = "node") -> dict:
    """The whole gate over one page. Returns a report dict; report['ok'] is the verdict.

    node absent → `node_available: False`, the JS gate is skipped-and-reported while
    the deterministic scans still decide `ok` (pytest mirrors this with a skip)."""
    path = html_path or DEFAULT_PAGE
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    scripts = extract_scripts(html)
    placeholders = find_placeholders(html)
    missing = missing_sections(html)
    node_available = shutil.which(node_bin) is not None
    js_errors: list[str] = []
    if node_available:
        for tag_line, body in scripts:
            ok, err = node_check(body, node_bin=node_bin)
            if not ok:
                m = re.match(r"js line (\d+):", err)
                if m:  # translate the js line into the page's own line numbering
                    err += f" (html line ~{tag_line + int(m.group(1)) - 1})"
                js_errors.append(f"<script> at html line {tag_line}: {err}")
    return {
        "ok": not js_errors and not placeholders and not missing,
        "path": path,
        "scripts": len(scripts),
        "node_available": node_available,
        "js_errors": js_errors,
        "placeholders": placeholders,
        "missing_sections": missing,
    }


def format_report(rep: dict) -> str:
    lines = [f"[selfcheck] {rep['path']}",
             f"[selfcheck] inline <script> blocks: {rep['scripts']}"]
    if rep["node_available"]:
        lines.append("[selfcheck] node --check: "
                     + ("OK" if not rep["js_errors"] else "FAILED"))
        lines.extend(f"  ! {e}" for e in rep["js_errors"])
    else:
        lines.append("[selfcheck] node --check: SKIPPED "
                     "(node not found — JS syntax NOT verified)")
    lines.append("[selfcheck] raw {PLACEHOLDER} braces: "
                 + (", ".join(rep["placeholders"]) if rep["placeholders"] else "none"))
    lines.append("[selfcheck] required sections: "
                 + ("all present" if not rep["missing_sections"]
                    else "MISSING " + ", ".join(rep["missing_sections"])))
    lines.append(f"[selfcheck] verdict: {'PASS' if rep['ok'] else 'FAIL'}")
    return "\n".join(lines)
