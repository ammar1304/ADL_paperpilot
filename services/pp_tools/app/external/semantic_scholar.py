"""Semantic Scholar paper search, the search agent's tool for papers outside the local library.

API: GET https://api.semanticscholar.org/graph/v1/paper/search?query=...&limit=...&fields=...
returns {"total": n, "offset": 0, "data": [{"paperId": ..., "title": ..., ...}]}.
An API key (header x-api-key) gives 1 request per second; without one, requests
share a public pool and are often refused with HTTP 429, so every request is
spaced and 429 answers are retried with a growing pause.

Only titles and abstracts are used. They go to the reranker and, for the papers
that reach the judge, into the judge prompt as evidence labelled
"s2:<paperId>#abs" (or the arXiv id when Semantic Scholar knows it, so a paper
already in the local library is not counted twice).
"""

import logging
import threading
import time
from dataclasses import dataclass

import httpx

from ..retrieval.store import Hit

log = logging.getLogger("pp_tools.semantic_scholar")

FIELDS = "paperId,externalIds,title,abstract,year,venue,url,citationCount"


class SemanticScholarError(Exception):
    pass


@dataclass
class ExternalPaper:
    paper_id: str  # "arxiv:2310.11511" when Semantic Scholar knows the arXiv id, else "s2:<paperId>"
    s2_id: str
    title: str
    abstract: str
    year: int
    venue: str
    url: str
    citation_count: int

    def as_hit(self) -> Hit:
        """The abstract as a chunk, so ranking and evidence treat it like a local abstract."""
        text = f"{self.title}. Abstract.\n{self.abstract}"
        return Hit(f"{self.paper_id}#abs", self.paper_id, 0.0, "abstract", "Abstract", self.title, self.year, text)

    def as_dict(self) -> dict:
        return {
            "paper_id": self.paper_id, "s2_id": self.s2_id, "title": self.title, "abstract": self.abstract,
            "year": self.year, "venue": self.venue, "url": self.url, "citation_count": self.citation_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExternalPaper":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


class SemanticScholar:
    def __init__(
        self,
        base_url: str = "https://api.semanticscholar.org/graph/v1",
        api_key: str = "",
        min_interval_s: float = 1.1,
        timeout_s: float = 20.0,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_interval_s = min_interval_s
        self.retries = retries
        headers = {"User-Agent": "PaperPilot/0.1 (student research project)"}
        if api_key:
            headers["x-api-key"] = api_key
        self.http = httpx.Client(timeout=timeout_s, headers=headers)
        self._lock = threading.Lock()
        self._last = 0.0

    def close(self) -> None:
        self.http.close()

    def _wait_turn(self) -> None:
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def search(self, query: str, limit: int = 10) -> list[ExternalPaper]:
        """Papers for a query, most relevant first. Papers without an abstract are skipped."""
        last = ""
        for attempt in range(1, self.retries + 1):
            self._wait_turn()
            try:
                resp = self.http.get(f"{self.base_url}/paper/search", params={"query": query, "limit": limit, "fields": FIELDS})
                if resp.status_code == 200:
                    return [p for p in (_paper(item) for item in resp.json().get("data") or []) if p is not None]
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
            except (httpx.HTTPError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            log.warning("Semantic Scholar request failed, retrying", extra={"attempt": attempt, "error": last})
            time.sleep(self.min_interval_s * 2**attempt)
        raise SemanticScholarError(f"search failed: {last}")


def _paper(item: dict) -> ExternalPaper | None:
    abstract = (item.get("abstract") or "").strip()
    title = (item.get("title") or "").strip()
    s2_id = item.get("paperId") or ""
    if not (abstract and title and s2_id):
        return None
    arxiv = ((item.get("externalIds") or {}).get("ArXiv") or "").strip()
    paper_id = f"arxiv:{arxiv}" if arxiv else f"s2:{s2_id}"
    url = f"https://arxiv.org/abs/{arxiv}" if arxiv else (item.get("url") or f"https://www.semanticscholar.org/paper/{s2_id}")
    return ExternalPaper(
        paper_id=paper_id,
        s2_id=s2_id,
        title=title,
        abstract=abstract,
        year=int(item.get("year") or 0),
        venue=item.get("venue") or "",
        url=url,
        citation_count=int(item.get("citationCount") or 0),
    )
