"""OpenAlex works search, the search agent's second web source (D29).

API: GET https://api.openalex.org/works?search=...&per_page=...&select=...
returns {"meta": {...}, "results": [{"id": "https://openalex.org/W...", ...}]}.
Basic queries need no key (10 requests per second); an optional free key
(api_key parameter) raises the daily budget. Abstracts come as an inverted index
(word -> positions) and are rebuilt here.

Used when Semantic Scholar is not configured with a key or refuses a request:
without a key, Semantic Scholar's shared pool answered HTTP 429 on the team PC.
"""

import logging
import re
import time

import httpx

from .semantic_scholar import ExternalPaper

log = logging.getLogger("pp_tools.openalex")

SELECT = "id,doi,title,publication_year,abstract_inverted_index,cited_by_count,primary_location,ids"
ARXIV_DOI = re.compile(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", re.I)
ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.I)


class OpenAlexError(Exception):
    pass


class OpenAlex:
    def __init__(
        self, base_url: str = "https://api.openalex.org", api_key: str = "", timeout_s: float = 20.0, retries: int = 3
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.retries = retries
        self.http = httpx.Client(timeout=timeout_s, headers={"User-Agent": "PaperPilot/0.1 (student research project)"})

    def close(self) -> None:
        self.http.close()

    def search(self, query: str, limit: int = 10) -> list[ExternalPaper]:
        params = {"search": query, "per_page": limit, "select": SELECT}
        if self.api_key:
            params["api_key"] = self.api_key
        last = ""
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.http.get(f"{self.base_url}/works", params=params)
                if resp.status_code == 200:
                    return [p for p in (_paper(w) for w in resp.json().get("results") or []) if p is not None]
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
            except (httpx.HTTPError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            log.warning("OpenAlex request failed, retrying", extra={"attempt": attempt, "error": last})
            time.sleep(0.5 * 2**attempt)
        raise OpenAlexError(f"search failed: {last}")


def abstract_from_index(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positions = sorted((pos, word) for word, where in index.items() for pos in where)
    return " ".join(word for _, word in positions)


def _paper(work: dict) -> ExternalPaper | None:
    title = (work.get("title") or "").strip()
    abstract = abstract_from_index(work.get("abstract_inverted_index")).strip()
    oa_id = (work.get("id") or "").rsplit("/", 1)[-1]
    if not (title and abstract and oa_id):
        return None
    location = work.get("primary_location") or {}
    landing = location.get("landing_page_url") or ""
    doi = work.get("doi") or (work.get("ids") or {}).get("doi") or ""
    m = ARXIV_DOI.search(doi) or ARXIV_URL.search(landing)
    arxiv = m.group(1) if m else ""
    venue = ((location.get("source") or {}).get("display_name")) or ""
    return ExternalPaper(
        paper_id=f"arxiv:{arxiv}" if arxiv else f"openalex:{oa_id}",
        s2_id=oa_id,
        title=title,
        abstract=abstract,
        year=int(work.get("publication_year") or 0),
        venue=venue,
        url=f"https://arxiv.org/abs/{arxiv}" if arxiv else (landing or doi or f"https://openalex.org/{oa_id}"),
        citation_count=int(work.get("cited_by_count") or 0),
        source="openalex",
    )
