"""Ranking metrics for retrieval evaluation (E1).

Both are computed per query on a ranked list of paper ids, then averaged.

Recall@k = |relevant papers in the top k| / |relevant papers|
MRR@k    = 1 / rank of the first relevant paper, or 0 if none is in the top k
"""

from collections.abc import Sequence


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        raise ValueError("a query needs at least one relevant item")
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    for rank, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
