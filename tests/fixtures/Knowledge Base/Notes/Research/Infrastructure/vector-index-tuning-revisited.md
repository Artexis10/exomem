---
type: research-note
project: infrastructure
status: active
created: 2026-06-25
updated: 2026-06-25
sources: []
tags: [vector-database, indexing, hnsw]
---

# Tuning vector indexes, revisited with HNSW

## Question

After benchmarking, how should we tune vector database indexes for recall and build cost?

## Findings

The latest read: HNSW beats IVFFlat on recall-at-fixed-latency for our corpus. Tune `m` (graph degree, 16 is a good default) and `ef_construction` (build-time breadth, 64–200) for build cost, and raise `ef_search` at query time to trade latency for recall. This supersedes the earlier IVFFlat list-count guidance as the current recommendation for tuning vector database indexes.

## Connections

- [[Knowledge Base/Notes/Research/Infrastructure/vector-index-tuning]]
