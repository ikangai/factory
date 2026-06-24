"""Ingest human-supplied research material from MISSION.md.

The operator drops arXiv ids, arXiv URLs, or GitHub repo URLs under MISSION.md's
`## Material from the human` section (one bullet per line). This module parses and
*classifies* those lines, then fetches them via the EXISTING deterministic
retrieval (search_arxiv by id, search_repos by `repo:owner/name`).

Security (no SSRF): we NEVER fetch an arbitrary URL. Only lines that classify as an
arXiv id / arxiv.org URL or a github.com repository URL are fetched; anything else
is recorded as `unfetched` for the human to handle, never dereferenced. Network
failures degrade to skipping that one source (the line is marked `unfetched`),
never crashing the loop."""
from __future__ import annotations

import re

from .focus import _section_body

# arXiv ids look like 2606.24820 or 2606.24820v3 (modern) or hep-th/9901001 (legacy).
_ARXIV_ID = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)$",
                       re.IGNORECASE)
# arXiv URLs: http(s)://arxiv.org/abs/<id> (or /pdf/<id>).
_ARXIV_URL = re.compile(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:\.pdf)?/?$",
                        re.IGNORECASE)
# GitHub repo URLs: http(s)://github.com/<owner>/<name> (ignore deeper paths/.git).
_GITHUB_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE)


def _strip_bullet(line: str) -> str:
    """Strip a leading markdown list marker (`- `, `* `, `+ `, `1. `) and inline
    link/code wrapping so a bare id/URL remains."""
    s = line.strip()
    s = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", s)          # list marker
    # `<url>` or `[text](url)` or `` `id` `` wrappers → keep the address/id
    md = re.match(r"^\[[^\]]*\]\((.+?)\)$", s)
    if md:
        s = md.group(1)
    s = s.strip("<>`").strip()
    return s


def classify_line(line: str) -> dict | None:
    """Classify a single material line. Returns a dict with `kind` in
    {arxiv, repo, unfetched} plus the parsed query, or None for an empty/non-item
    line (so the caller can skip it). We classify only — no network here."""
    raw = line.strip()
    if not raw:
        return None
    item = _strip_bullet(line)
    if not item:
        return None

    m = _ARXIV_URL.match(item)
    if m:
        aid = m.group(1).strip("/")
        return {"kind": "arxiv", "arxiv_id": aid, "source": raw}
    if _ARXIV_ID.match(item):
        aid = re.sub(r"(?i)^arxiv:", "", item)
        return {"kind": "arxiv", "arxiv_id": aid, "source": raw}
    m = _GITHUB_URL.match(item)
    if m:
        full = f"{m.group(1)}/{m.group(2)}"
        return {"kind": "repo", "full_name": full, "source": raw}
    # Not an arXiv id/URL or a github.com repo URL → DO NOT fetch (no SSRF).
    return {"kind": "unfetched", "value": item, "source": raw,
            "reason": "not an arXiv id/URL or a github.com repo URL — not fetched"}


def parse_material(mission_path: str) -> list[dict]:
    """Read MISSION.md's `## Material from the human` section and FETCH the human's
    arXiv / GitHub items via the existing deterministic retrieval. Returns a list of
    dicts, one per material line:

      {"kind": "arxiv", "arxiv_id": ..., "paper": Paper|None, ...}
      {"kind": "repo",  "full_name": ..., "repo": Repo|None, ...}
      {"kind": "unfetched", "value": ..., "reason": ...}

    A fetched-but-empty result (id not found / network failure) keeps the entry with
    its object set to None and an `error` note, so the human sees it was attempted.
    Never raises — missing file / network failure / parse error all degrade."""
    # Local imports keep this module importable without touching the network and
    # mirror research_cli_agents' lazy-import style (testable via monkeypatch).
    from .arxiv import search_arxiv
    from .git_repos import search_repos

    try:
        with open(mission_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []

    body = _section_body(text, "Material from the human")
    if not body:
        return []

    out: list[dict] = []
    for line in body.splitlines():
        item = classify_line(line)
        if item is None:
            continue
        if item["kind"] == "arxiv":
            try:
                # `id_list:`-style query: pass the bare id as the search query.
                papers = search_arxiv(f"id:{item['arxiv_id']}", max_results=1)
                item["paper"] = papers[0] if papers else None
                if not papers:
                    item["error"] = "arXiv returned no paper for this id"
            except Exception as e:  # noqa: BLE001 — degrade per-source
                item["paper"] = None
                item["error"] = f"arXiv fetch failed: {e}"
        elif item["kind"] == "repo":
            try:
                repos = search_repos(f"repo:{item['full_name']}", max_results=1)
                item["repo"] = repos[0] if repos else None
                if not repos:
                    item["error"] = "GitHub returned no repo for this name"
            except Exception as e:  # noqa: BLE001 — degrade per-source
                item["repo"] = None
                item["error"] = f"GitHub fetch failed: {e}"
        out.append(item)
    return out
