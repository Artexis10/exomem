---
type: insight
status: active
created: 2026-06-14
updated: 2026-06-14
sources: []
tags: [retrieval, fusion, ranking, rrf]
---

# Reciprocal rank fusion is more robust than normalizing retriever scores

## Claim

To combine a lexical retriever with a vector retriever, fuse by rank with reciprocal rank fusion (RRF) rather than normalizing and adding their raw scores. RRF depends only on the position of a document in each list, so it is immune to the incomparable score scales that break weighted-sum fusion.

## Why it holds

BM25 scores and cosine similarities live on different, non-linear scales; any fixed normalization is a guess that drifts per query. Rank is scale-free: a document ranked first contributes `1 / (k + 1)` from each list regardless of the underlying score magnitude.

## Where it applies

Any hybrid search that blends two or more rankers with unlike score distributions.

## Connections

- [[Knowledge Base/Notes/Research/Project Alpha/engine-architecture]]
