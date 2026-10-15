"""Query the knowledge base at chunk level or paper level."""

from dataclasses import asdict
from typing import Any

from .embedder import Embedder
from .store import Hit, Store


class Retriever:
    def __init__(self, embedder: Embedder, store: Store) -> None:
        self.embedder = embedder
        self.store = store

    def chunks(self, query: str, strategy: str, k: int, filters: dict[str, Any] | None = None) -> list[Hit]:
        vector = self.embedder.embed([query])[0]
        return self.store.search(strategy, vector, k, filters)

    def by_ids(self, chunk_ids: list[str], strategy: str) -> list[Hit]:
        return self.store.get(strategy, chunk_ids)

    def papers(
        self, query: str, strategy: str, k: int, pool: int | None = None, filters: dict[str, Any] | None = None
    ) -> list[Hit]:
        """Top-k distinct papers, each represented by its best-scoring chunk.

        Retrieves a larger pool of chunks first because one paper can fill many
        of the top chunk slots.
        """
        pool = pool or max(50, k * 10)
        best: dict[str, Hit] = {}
        for hit in self.chunks(query, strategy, pool, filters):
            if hit.paper_id not in best:  # hits arrive best first
                best[hit.paper_id] = hit
            if len(best) == k:
                break
        return list(best.values())


def hit_dict(hit: Hit) -> dict[str, Any]:
    return asdict(hit)
