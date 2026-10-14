"""Minimal arXiv client: search, metadata by id, PDF download.

Follows arXiv's API terms: one request at a time, at least `delay_s` seconds
between requests (3 s by default), and a User-Agent that identifies the client.
https://info.arxiv.org/help/api/tou.html
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("pp_tools.arxiv")

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}

# New-style ids (2005.11401v4) and old-style ids (hep-th/9901001v2).
_ID_RE = re.compile(r"(?P<base>(\d{4}\.\d{4,5})|([a-z\-]+(\.[A-Z]{2})?/\d{7}))(v(?P<version>\d+))?$")


class ArxivError(Exception):
    pass


@dataclass(frozen=True)
class ArxivPaper:
    arxiv_id: str  # without version, e.g. 2005.11401
    version: int
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    published: str = ""  # ISO date of v1
    primary_category: str = ""
    categories: list[str] = field(default_factory=list)

    @property
    def paper_id(self) -> str:
        """The id used everywhere in PaperPilot (chunks, citations, verifier)."""
        return f"arxiv:{self.arxiv_id}"

    @property
    def versioned_id(self) -> str:
        return f"{self.arxiv_id}v{self.version}"

    @property
    def year(self) -> int:
        return int(self.published[:4]) if self.published[:4].isdigit() else 0


def split_id(raw: str) -> tuple[str, int | None]:
    """'http://arxiv.org/abs/2005.11401v4' -> ('2005.11401', 4)."""
    tail = raw.strip().rstrip("/")
    for prefix in ("http://arxiv.org/abs/", "https://arxiv.org/abs/", "arxiv:"):
        if tail.startswith(prefix):
            tail = tail[len(prefix) :]
    m = _ID_RE.search(tail)
    if not m:
        raise ArxivError(f"not an arXiv id: {raw!r}")
    version = m.group("version")
    return m.group("base"), int(version) if version else None


def _text(el: ET.Element | None) -> str:
    return re.sub(r"\s+", " ", el.text or "").strip() if el is not None else ""


def parse_feed(xml_text: str) -> tuple[int, list[ArxivPaper]]:
    """Parse an arXiv Atom response into (total results, papers)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ArxivError(f"arXiv returned invalid XML: {exc}") from exc

    total_el = root.find("opensearch:totalResults", NS)
    total = int(total_el.text) if total_el is not None and (total_el.text or "").isdigit() else 0

    papers = []
    for entry in root.findall("atom:entry", NS):
        raw_id = _text(entry.find("atom:id", NS))
        title = _text(entry.find("atom:title", NS))
        # arXiv reports query errors as a single entry titled "Error".
        if title == "Error" and "api/errors" in raw_id:
            raise ArxivError(f"arXiv API error: {_text(entry.find('atom:summary', NS))}")
        base, version = split_id(raw_id)
        primary = entry.find("arxiv:primary_category", NS)
        papers.append(
            ArxivPaper(
                arxiv_id=base,
                version=version or 1,
                title=title,
                abstract=_text(entry.find("atom:summary", NS)),
                authors=[_text(a.find("atom:name", NS)) for a in entry.findall("atom:author", NS)],
                published=_text(entry.find("atom:published", NS)),
                primary_category=primary.get("term", "") if primary is not None else "",
                categories=[c.get("term", "") for c in entry.findall("atom:category", NS)],
            )
        )
    return total, papers


class ArxivClient:
    def __init__(
        self,
        api_url: str,
        pdf_url: str,
        delay_s: float = 3.0,
        contact: str = "",
        timeout_s: float = 60.0,
        retries: int = 3,
    ) -> None:
        self.api_url = api_url
        self.pdf_url = pdf_url.rstrip("/")
        self.delay_s = delay_s
        self.retries = retries
        agent = "PaperPilot/0.1 (student research project"
        agent += f"; mailto:{contact})" if contact else ")"
        self.http = httpx.Client(timeout=timeout_s, headers={"User-Agent": agent}, follow_redirects=True)
        self._last_request = 0.0

    def close(self) -> None:
        self.http.close()

    def _wait_turn(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay_s:
            time.sleep(self.delay_s - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._wait_turn()
            try:
                resp = self.http.get(url, params=params)
                if resp.status_code == 200:
                    return resp
                # 429 and 5xx are worth retrying with a longer pause; 4xx are not.
                if resp.status_code != 429 and resp.status_code < 500:
                    raise ArxivError(f"GET {url} returned HTTP {resp.status_code}")
                last_exc = ArxivError(f"GET {url} returned HTTP {resp.status_code}")
            except httpx.HTTPError as exc:
                last_exc = exc
            backoff = self.delay_s * (2**attempt)
            log.warning("arXiv request failed, backing off", extra={"url": url, "attempt": attempt, "sleep_s": backoff})
            time.sleep(backoff)
        raise ArxivError(f"GET {url} failed after {self.retries} attempts: {last_exc}")

    def search(self, query: str, max_results: int, page_size: int = 100, sort_by: str = "relevance") -> list[ArxivPaper]:
        """Run a search_query and return up to max_results papers, in arXiv's order."""
        papers: list[ArxivPaper] = []
        start = 0
        while len(papers) < max_results:
            size = min(page_size, max_results - len(papers))
            params = {
                "search_query": query,
                "start": start,
                "max_results": size,
                "sortBy": sort_by,
                "sortOrder": "descending",
            }
            total, page = parse_feed(self._get(self.api_url, params).text)
            log.info("arXiv search page", extra={"start": start, "got": len(page), "total": total})
            if not page:
                break
            papers.extend(page)
            start += len(page)
            if start >= total:
                break
        return papers[:max_results]

    def fetch(self, versioned_ids: list[str], batch_size: int = 100) -> dict[str, ArxivPaper]:
        """Metadata for exact versions, keyed by versioned id (e.g. '2005.11401v4')."""
        found: dict[str, ArxivPaper] = {}
        for i in range(0, len(versioned_ids), batch_size):
            batch = versioned_ids[i : i + batch_size]
            params = {"id_list": ",".join(batch), "max_results": len(batch)}
            _, papers = parse_feed(self._get(self.api_url, params).text)
            for p in papers:
                found[p.versioned_id] = p
        return found

    def download_pdf(self, versioned_id: str, dest: Path) -> Path:
        """Download a PDF unless it is already cached. Returns its path."""
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        resp = self._get(f"{self.pdf_url}/{versioned_id}")
        if not resp.content.startswith(b"%PDF"):
            raise ArxivError(f"{versioned_id}: response is not a PDF")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(resp.content)
        tmp.replace(dest)
        return dest
