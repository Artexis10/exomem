"""Retrieval metrics over sentinel identity (binary relevance).

Relevant documents are the query's oracle-required citations; a hit is
relevant when any of its sentinels names one. Binary relevance makes
recall@k and MRR the informative metrics; graded NDCG adds nothing here and
is deliberately omitted (see docs/memory-proof-benchmark.md).
"""

from __future__ import annotations

from membench.adapters.base import Hit
from membench.schema import ExpectedRecord, QueryRecord


def score_retrieval(
    query: QueryRecord, expected: ExpectedRecord, hits: list[Hit]
) -> dict[str, object] | None:
    relevant = set(expected.required_citations)
    if not relevant:
        return None  # not applicable (abstain/control queries)
    found_at: list[int] = []
    seen: set[str] = set()
    for hit in hits:
        matched = relevant.intersection(hit.sentinels)
        if matched - seen:
            found_at.append(hit.rank)
            seen.update(matched)
    def recall_at(k: int) -> float:
        covered = set()
        for hit in hits:
            if hit.rank <= k:
                covered.update(relevant.intersection(hit.sentinels))
        return len(covered) / len(relevant)

    return {
        "relevant": sorted(relevant),
        "recall_at_5": recall_at(5),
        "recall_at_10": recall_at(10),
        "mrr": (1.0 / found_at[0]) if found_at else 0.0,
        "first_relevant_rank": found_at[0] if found_at else None,
        "hit_count": len(hits),
    }
