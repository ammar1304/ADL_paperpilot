"""Qdrant storage: one collection per chunking strategy."""

import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient, models

from ..corpus.chunking import STRATEGIES, Chunk

COLLECTION_PREFIX = "papers_"
INDEXED_FIELDS = ("paper_id", "chunk_type", "strategy")


def collection_name(strategy: str) -> str:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    return f"{COLLECTION_PREFIX}{strategy}"


def point_id(chunk_id: str, strategy: str) -> str:
    # Deterministic: re-running a build overwrites points instead of duplicating them.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"paperpilot:{strategy}:{chunk_id}"))


def make_client(location: str, timeout_s: float = 10.0) -> QdrantClient:
    """'http://qdrant:6333' for the real server; 'local::memory:' or 'local:/dir' in-process."""
    if location.startswith("local:"):
        target = location[len("local:") :]
        return QdrantClient(location=":memory:") if target == ":memory:" else QdrantClient(path=target)
    return QdrantClient(url=location, timeout=int(timeout_s))


@dataclass
class Hit:
    chunk_id: str
    paper_id: str
    score: float
    chunk_type: str
    section: str | None
    title: str
    year: int
    text: str


class Store:
    def __init__(self, client: QdrantClient) -> None:
        self.client = client

    def ensure_collection(self, strategy: str, dim: int, recreate: bool = False) -> None:
        name = collection_name(strategy)
        exists = self.client.collection_exists(name)
        if exists and recreate:
            self.client.delete_collection(name)
            exists = False
        if not exists:
            self.client.create_collection(
                name, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
            )
            for field in INDEXED_FIELDS:
                self.client.create_payload_index(name, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD)

    def reusable_vectors(self, chunks: list[Chunk], model: str | None, batch: int = 256) -> dict[str, list[float]]:
        """Vectors already stored for these chunks, keyed by chunk id.

        A vector is reused only when the stored text is identical and it was made
        by the same embedding model. Points stored before the model was recorded
        (builds before pp-tools 0.3, all BGE-M3) count as the current model.
        """
        if not chunks or model is None:
            return {}
        name = collection_name(chunks[0].strategy)
        if not self.client.collection_exists(name):
            return {}
        by_point = {point_id(c.chunk_id, c.strategy): c for c in chunks}
        ids = list(by_point)
        out: dict[str, list[float]] = {}
        for i in range(0, len(ids), batch):
            for p in self.client.retrieve(name, ids=ids[i : i + batch], with_vectors=True, with_payload=["text", "embedding_model"]):
                chunk = by_point.get(str(p.id))
                pl = p.payload or {}
                if chunk is None or pl.get("text") != chunk.text or not isinstance(p.vector, list):
                    continue
                if pl.get("embedding_model", model) != model:
                    continue
                out[chunk.chunk_id] = p.vector
        return out

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]], model: str | None = None) -> None:
        if not chunks:
            return
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        strategy = chunks[0].strategy
        points = [
            models.PointStruct(
                id=point_id(c.chunk_id, c.strategy),
                vector=v,
                payload={**c.payload(), **({"embedding_model": model} if model else {})},
            )
            for c, v in zip(chunks, vectors, strict=True)
        ]
        self.client.upsert(collection_name(strategy), points=points, wait=True)

    def search(self, strategy: str, vector: list[float], limit: int, filters: dict[str, Any] | None = None) -> list[Hit]:
        conditions = [
            models.FieldCondition(key=k, match=models.MatchValue(value=v)) for k, v in (filters or {}).items() if v is not None
        ]
        result = self.client.query_points(
            collection_name(strategy),
            query=vector,
            limit=limit,
            with_payload=True,
            query_filter=models.Filter(must=conditions) if conditions else None,
        )
        return [_hit(p.payload or {}, float(p.score)) for p in result.points]

    def get(self, strategy: str, chunk_ids: list[str]) -> list[Hit]:
        """Chunks by id (point ids are derived from chunk ids); unknown ids are skipped."""
        if not chunk_ids:
            return []
        points = self.client.retrieve(
            collection_name(strategy), ids=[point_id(c, strategy) for c in chunk_ids], with_payload=True, with_vectors=False
        )
        return [_hit(p.payload or {}, 0.0) for p in points]

    def stats(self) -> dict[str, Any]:
        """Chunk and paper counts per strategy; papers are counted from payloads."""
        out: dict[str, Any] = {}
        for strategy in STRATEGIES:
            name = collection_name(strategy)
            if not self.client.collection_exists(name):
                out[strategy] = {"exists": False}
                continue
            papers: set[str] = set()
            with_sections: set[str] = set()  # papers whose full text was chunked (structured strategy only)
            types: dict[str, int] = {}
            offset = None
            while True:
                points, offset = self.client.scroll(
                    name, limit=1000, offset=offset, with_payload=["paper_id", "chunk_type"], with_vectors=False
                )
                for p in points:
                    pl = p.payload or {}
                    papers.add(pl.get("paper_id", ""))
                    if pl.get("chunk_type") in {"section", "table", "figure"}:
                        with_sections.add(pl.get("paper_id", ""))
                    types[pl.get("chunk_type", "")] = types.get(pl.get("chunk_type", ""), 0) + 1
                if offset is None:
                    break
            out[strategy] = {
                "exists": True,
                "chunks": sum(types.values()),
                "papers": len(papers),
                "papers_with_sections": len(with_sections),
                "chunk_types": types,
            }
        return out


def _hit(pl: dict[str, Any], score: float) -> Hit:
    return Hit(
        chunk_id=pl.get("chunk_id", ""),
        paper_id=pl.get("paper_id", ""),
        score=score,
        chunk_type=pl.get("chunk_type", ""),
        section=pl.get("section"),
        title=pl.get("title", ""),
        year=int(pl.get("year") or 0),
        text=pl.get("text", ""),
    )
