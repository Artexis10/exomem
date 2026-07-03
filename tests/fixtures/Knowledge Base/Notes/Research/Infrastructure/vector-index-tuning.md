---
type: research-note
project: infrastructure
status: active
created: 2026-03-10
updated: 2026-03-10
sources: []
tags: [vector-database, indexing, ivfflat]
---

# Tuning IVFFlat vector indexes

## Question

How should the IVFFlat list count be chosen for an approximate nearest-neighbour vector index?

## Findings

IVFFlat partitions vectors into `lists` cells and probes a subset at query time. A common starting point is `lists = rows / 1000` for up to a million rows, with `probes` traded off against recall. This is the earlier reading, before HNSW was benchmarked on the same corpus.

## Connections

- [[Knowledge Base/Notes/Research/Infrastructure/vector-index-tuning-revisited]]
