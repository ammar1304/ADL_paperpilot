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

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        strategy = chunks[0].strategy
        points = [
            models.PointStruct(id=point_id(c.chunk_id, c.strategy), vector=v, payload=c.payload())
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
        hits = []
        for p in result.points:
            pl = p.payload or {}
            hits.append(
                Hit(
                    chunk_id=pl.get("chunk_id", ""),
                    paper_id=pl.get("paper_id", ""),
                    score=float(p.score),
                    chunk_type=pl.get("chunk_type", ""),
                    section=pl.get("section"),
                    title=pl.get("title", ""),
                    year=int(pl.get("year") or 0),
                    text=pl.get("text", ""),
                )
            )
        return hits

    def stats(self) -> dict[str, Any]:
        """Chunk and paper counts per strategy; papers are counted from payloads."""
        out: dict[str, Any] = {}
        for strategy in STRATEGIES:
            name = collection_name(strategy)
            if not self.client.collection_exists(name):
                out[strategy] = {"exists": False}
                continue
            papers: set[str] = set()
            types: dict[str, int] = {}
            offset = None
            while True:
                points, offset = self.client.scroll(
                    name, limit=1000, offset=offset, with_payload=["paper_id", "chunk_type"], with_vectors=False
                )
                for p in points:
                    pl = p.payload or {}
                    papers.add(pl.get("paper_id", ""))
                    types[pl.get("chunk_type", "")] = types.get(pl.get("chunk_type", ""), 0) + 1
                if offset is None:
                    break
            out[strategy] = {"exists": True, "chunks": sum(types.values()), "papers": len(papers), "chunk_types": types}
        return out
