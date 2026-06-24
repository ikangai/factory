"""Deterministic GitHub-repo retrieval for the researcher role.

A second research source alongside arXiv: real repositories implementing relevant
techniques. Uses the public GitHub search API (key-less, rate-limited) over HTTPS,
parsed as JSON (stdlib — no XML attack surface). The LLM never touches the network;
it only distils the records we fetch, so the researcher role stays isolated.

The query is caller-supplied (mission/target-driven). DEFAULT_QUERY targets clive's
focus — agents that drive a CLI — but a different target passes its own focus."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

_API = "https://api.github.com/search/repositories"

# clive's default focus; override per target/mission (genericity).
DEFAULT_QUERY = "LLM agent CLI terminal shell tool-use in:name,description,readme"


@dataclass
class Repo:
    full_name: str
    description: str
    url: str
    stars: int
    language: str
    topics: list[str] = field(default_factory=list)
    pushed_at: str = ""

    def brief(self, max_desc: int = 400) -> str:
        d = " ".join((self.description or "").split())
        if len(d) > max_desc:
            d = d[:max_desc] + "…"
        tp = (", ".join(self.topics[:8])) if self.topics else "—"
        return (f"### {self.full_name}  ★{self.stars}  [{self.language or '?'}]\n"
                f"- {self.url}  (pushed {self.pushed_at[:10]})\n"
                f"- topics: {tp}\n"
                f"- description: {d or '(none)'}")


def search_repos(query: str = DEFAULT_QUERY, max_results: int = 8,
                 timeout: int = 30) -> list[Repo]:
    """Return up to `max_results` repos matching `query`, most-starred first. Raises
    RuntimeError on any network/parse failure (caller degrades gracefully)."""
    params = urllib.parse.urlencode({
        "q": query, "sort": "stars", "order": "desc",
        "per_page": max(1, min(max_results, 50)),
    })
    req = urllib.request.Request(
        f"{_API}?{params}",
        headers={
            "User-Agent": "clive-harness-factory/research",   # GitHub requires a UA
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # noqa: BLE001 — degrade, don't crash the loop
        raise RuntimeError(f"GitHub repo search failed: {e}") from e

    repos: list[Repo] = []
    for it in (data.get("items") or [])[:max_results]:
        repos.append(Repo(
            full_name=it.get("full_name", ""),
            description=it.get("description") or "",
            url=it.get("html_url", ""),
            stars=int(it.get("stargazers_count", 0) or 0),
            language=it.get("language") or "",
            topics=list(it.get("topics") or []),
            pushed_at=it.get("pushed_at", "") or "",
        ))
    return repos
