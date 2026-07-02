"""Pure ranking-quality metrics for the retrieval eval harness.

No torch, no exomem imports — these operate on already-normalized path strings
so they're importable in the fast (embedding-free) test suite and reusable by
`scripts/eval_retrieval.py`. The harness is responsible for canonicalizing
both the golden-set paths and find()'s returned paths into the same form
before calling these (see `scripts/eval_retrieval.py:_canon`).

Conventions:
- `ranked`: list[str] of retrieved paths, best-first (what find() returned).
- `relevance`: dict[str, float] mapping a path to its graded relevance
  (0..3 typical). Absent paths are treated as grade 0.
- `relevant`: set[str] of paths with grade > 0 (binary relevance), used by
  MRR and recall where grade magnitude doesn't matter.

NDCG uses the standard exponential gain (2**g - 1), so a grade-3 "ideal" hit is
worth far more than a grade-1 "marginal" one.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping


def dcg(gains: Iterable[float]) -> float:
    """Discounted cumulative gain. gains[i] is the gain at rank i+1."""
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: list[str], relevance: Mapping[str, float], k: int) -> float:
    """Normalized DCG@k with exponential gain. Returns 0.0 when no ideal gain."""
    actual = dcg(2.0 ** relevance.get(p, 0.0) - 1.0 for p in ranked[:k])
    ideal_grades = sorted(relevance.values(), reverse=True)[:k]
    ideal = dcg(2.0 ** g - 1.0 for g in ideal_grades)
    return actual / ideal if ideal > 0 else 0.0


def mrr(ranked: list[str], relevant: set[str]) -> float:
    """Reciprocal rank of the first relevant hit; 0.0 if none in `ranked`."""
    for i, p in enumerate(ranked, start=1):
        if p in relevant:
            return 1.0 / i
    return 0.0


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Fraction of the relevant set that appears in the top-k. 0.0 if none relevant."""
    if not relevant:
        return 0.0
    top = set(ranked[:k])
    return len(top & relevant) / len(relevant)


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0
