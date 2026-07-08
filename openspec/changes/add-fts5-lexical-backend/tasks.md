## 1. Tests First

- [x] 1.1 Add `tests/test_lexstore.py`: FTS5-availability probe + failure memo;
      schema creation idempotent; page-count/mtime sync-on-first-use populates a
      fresh sidecar from markdown (migration) and heals manual drift; lockstep
      after write/delete/move through the writer seams.
- [x] 1.2 Add the bm25 backend gate tests: FTS5-served lane returns ranked paths
      feeding RRF with the unchanged interface; `EXOMEM_LEXICAL_BACKEND=python`
      kill switch forces the rank-bm25 path; forced FTS5-unavailable serves the
      rank-bm25 path with no degradation recorded.
- [x] 1.3 Add the keyword-contract parity suite: FTS5/trigram-served keyword lane
      returns the SAME match set as the reference substring scan — including
      mid-word substrings, multi-token all-present semantics, title-vs-body hits,
      and 1-/2-char needles (the trigram floor cases).
- [x] 1.4 Add the stemming pin: a query whose target page uses a morphological
      variant ("regulation" → "regulator") ranks under the FTS5 backend as it
      does under rank-bm25 (byte-identical pre-stemming makes this hold).
- [x] 1.5 Add graph-lane sub-span timing tests: the graph stage exposes
      sub-spans (freshness, seed resolution, expansion, in-degree) in
      FindTimings without changing results.
- [x] 1.6 Extend `tests/test_latency_gate.py` with a second corpus size and a
      warm-graph scaling-ratio bound (bound value set from post-fix
      measurement — re-measure, don't hand-tune).

## 2. lexstore Module

- [x] 2.1 Implement `src/exomem/lexstore.py` modeled on `vecstore.py`: sidecar
      path/pragmas, FTS5 + trigram schema, availability probe with
      process-global memo, sync-on-first-use (page count + max mtime vs the
      markdown walk), page upsert/delete, `bm25_search(stemmed_query, k)` and
      `substring_search(tokens, k)`, `EXOMEM_LEXICAL_BACKEND` reader.

## 3. Lane Integration

- [x] 3.1 `bm25.BM25Index.search()` gains the ladder: FTS5 when available →
      rank-bm25 otherwise; interface to find.py unchanged.
- [x] 3.2 find.py keyword block gains the ladder: trigram-served match set →
      reference scan otherwise; short-needle fallback per the parity suite.
- [x] 3.3 Hook the lexical sidecar into the writer / watcher / reconcile seams
      via a shared post-write dispatch that runs on lean installs (not gated
      behind the embeddings import); fold the sidecar mtime into the find-cache
      freshness key.

## 4. Graph Lane

- [x] 4.1 Profile the warm graph lane at 2k/10k/50k over the cached corpora
      using the new sub-spans; record the identified O(N) recompute in
      design.md's decision (replacing the reserved target).
- [x] 4.2 Event-maintain or memoize the identified recompute; warm graph median
      passes the new scaling bound at both gate sizes.

## 5. Warmup, Doctor, Benchmark

- [x] 5.1 Warm the lexical sidecar at startup through the existing warm seam
      (sync check + one query), soft-fail as ever.
- [x] 5.2 Doctor: lexical sidecar presence/health probe + FTS5/trigram
      availability check (warn, not fail — the in-process paths exist).
- [x] 5.3 Extend `scripts/latency_curve.py` with `--lexical-backend
      python,fts5` passes; re-run 10k/50k (and the cached 100k) tiers; publish
      before/after + the graph-lane fix in `docs/benchmarks.md`.

## 6. Validation

- [x] 6.1 Lean suite green: `uv run python -m pytest -q` with
      `KB_MCP_DISABLE_EMBEDDINGS=1` — the lexical backend runs and is exercised
      on lean installs (no extras involved).
- [x] 6.2 Retrieval eval: golden floors + per-query pins hold under
      `EXOMEM_LEXICAL_BACKEND=fts5` (and the vec backend's two modes remain
      green — the gates compose).
- [x] 6.3 Keyword parity suite exact; `ruff check`; `openspec validate --strict`.
- [x] 6.4 End-to-end at scale: 50k-note cached corpus, bm25/keyword lanes at
      low-tens-of-ms, warm graph within the scaling bound, end-to-end `find()`
      total recorded in docs.
