"""Deterministic arXiv retrieval for the researcher role.

The arXiv Atom API is public, free, and key-less. We fetch recent papers matching a
query and return structured records; the LLM never touches the network — it only
distills the text we hand it (keeps the researcher role isolated like other roles)."""
from __future__ import annotations

import urllib.parse
import urllib.request
from dataclasses import dataclass

# defusedxml: stdlib XML parsers are vulnerable to XXE / billion-laughs on
# untrusted input (we parse network responses). fromstring() is a safe drop-in.
import defusedxml.ElementTree as ET

_API = "http://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"

# Default search: agents that drive a command line / terminal / shell, tool-use,
# and multi-agent CLI coordination — the factory's domain (clive and kin).
DEFAULT_QUERY = (
    'abs:("command line" OR terminal OR shell OR "tool use" OR "tool-use" '
    'OR "computer use") AND abs:(agent OR LLM OR "language model")'
)


@dataclass
class Paper:
    arxiv_id: str
    title: str
    summary: str
    url: str
    published: str
    authors: list[str]

    def brief(self, max_summary: int = 1200) -> str:
        s = " ".join(self.summary.split())
        if len(s) > max_summary:
            s = s[:max_summary] + "…"
        who = ", ".join(self.authors[:4]) + ("  et al." if len(self.authors) > 4 else "")
        return (f"### {self.title}\n"
                f"- arXiv: {self.arxiv_id} ({self.published[:10]})  {self.url}\n"
                f"- authors: {who}\n"
                f"- abstract: {s}")


def search_arxiv(query: str = DEFAULT_QUERY, max_results: int = 8,
                 timeout: int = 30) -> list[Paper]:
    """Return up to `max_results` recent papers matching `query`. Empty list on any
    network/parse failure (the factory must degrade gracefully, never crash)."""
    params = urllib.parse.urlencode({
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    req = urllib.request.Request(f"{_API}?{params}",
                                 headers={"User-Agent": "clive-harness-factory/research"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as e:  # noqa: BLE001 — degrade, don't crash the loop
        raise RuntimeError(f"arXiv fetch failed: {e}") from e

    root = ET.fromstring(raw)
    papers: list[Paper] = []
    for e in root.findall(f"{_ATOM}entry"):
        def _text(tag: str) -> str:
            el = e.find(f"{_ATOM}{tag}")
            return (el.text or "").strip() if el is not None else ""
        url = _text("id")
        # the <id> is the abs URL; the bare id is its tail (strip any version)
        aid = url.rsplit("/", 1)[-1]
        authors = [(a.find(f"{_ATOM}name").text or "").strip()
                   for a in e.findall(f"{_ATOM}author") if a.find(f"{_ATOM}name") is not None]
        papers.append(Paper(arxiv_id=aid, title=" ".join(_text("title").split()),
                            summary=_text("summary"), url=url,
                            published=_text("published"), authors=authors))
    return papers
