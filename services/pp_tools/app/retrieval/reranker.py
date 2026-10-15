"""Client for a Text Embeddings Inference (TEI) server running a cross-encoder reranker.

The embedding model scores a query and a passage separately (two vectors, one
cosine). A cross-encoder reads them together, so it can tell "decides whether to
retrieve" from "always retrieves" even when both passages share most words. It is
too slow to score the whole library, so it re-orders the few dozen candidates
the vector search returns (docs/DECISIONS.md D28).

API used: POST /rerank {"query": "...", "texts": [...], "raw_scores": false,
"truncate": true} returns [{"index": i, "score": s}, ...], best first; GET /health.
"""

import logging
import time

import httpx

log = logging.getLogger("pp_tools.reranker")


class RerankerError(Exception):
    pass


class Reranker:
    def __init__(self, url: str, batch_size: int = 32, timeout_s: float = 120.0, retries: int = 2) -> None:
        self.url = url.rstrip("/")
        self.batch_size = batch_size
        self.retries = retries
        self.http = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self.http.close()

    def healthy(self) -> bool:
        try:
            return self.http.get(f"{self.url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def model_id(self) -> str | None:
        try:
            resp = self.http.get(f"{self.url}/info", timeout=10)
            return resp.json().get("model_id") if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    def scores(self, query: str, texts: list[str]) -> list[float]:
        """One relevance score in [0, 1] per text, in the order the texts were given."""
        out: list[float] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._batch(query, texts[i : i + self.batch_size]))
        return out

    def _batch(self, query: str, texts: list[str]) -> list[float]:
        last = ""
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.http.post(
                    f"{self.url}/rerank", json={"query": query, "texts": texts, "raw_scores": False, "truncate": True}
                )
                if resp.status_code == 200:
                    ranked = resp.json()
                    scores = [0.0] * len(texts)
                    seen = set()
                    for item in ranked:
                        idx = int(item["index"])
                        scores[idx] = float(item["score"])
                        seen.add(idx)
                    if seen != set(range(len(texts))):
                        raise RerankerError(f"expected {len(texts)} scores, got {len(seen)}")
                    return scores
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code in (400, 413, 422):
                    break
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            log.warning("rerank request failed, retrying", extra={"attempt": attempt, "error": last})
            time.sleep(attempt)
        raise RerankerError(f"rerank failed: {last}")
