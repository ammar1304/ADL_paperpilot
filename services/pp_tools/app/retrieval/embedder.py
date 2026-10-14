"""Client for a Text Embeddings Inference (TEI) server.

TEI serves the embedding model (BGE-M3, docs/DECISIONS.md D14) over HTTP, so
pp-tools stays a small image without PyTorch or model weights.
API used: POST /embed {"inputs": [...], "truncate": true, "normalize": true}
returns one vector per input; GET /health returns 200 once the model is loaded.
"""

import logging
import time

import httpx

log = logging.getLogger("pp_tools.embedder")


class EmbedderError(Exception):
    pass


class Embedder:
    def __init__(self, url: str, batch_size: int = 16, timeout_s: float = 120.0, retries: int = 3) -> None:
        self.url = url.rstrip("/")
        self.batch_size = batch_size
        self.retries = retries
        self.http = httpx.Client(timeout=timeout_s)

    def close(self) -> None:
        self.http.close()

    def model_id(self) -> str | None:
        """The served model, from TEI's GET /info; None if the server does not say."""
        try:
            resp = self.http.get(f"{self.url}/info", timeout=10)
            return resp.json().get("model_id") if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    def healthy(self) -> bool:
        try:
            return self.http.get(f"{self.url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[i : i + self.batch_size]))
        return vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        last: str = ""
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.http.post(f"{self.url}/embed", json={"inputs": batch, "truncate": True, "normalize": True})
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data, list) or len(data) != len(batch):
                        raise EmbedderError(f"expected {len(batch)} vectors, got {type(data).__name__}")
                    return data
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                # 413/422 mean the request itself is wrong; retrying will not help.
                if resp.status_code in (400, 413, 422):
                    break
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            log.warning("embedding request failed, retrying", extra={"attempt": attempt, "error": last})
            time.sleep(2 * attempt)
        raise EmbedderError(f"embedding failed: {last}")
