## Why

Governed recall on the live cell is not sub-second, and on a busy day it is not
even sub-minute: two timed hybrid `ask_memory` calls on 2026-09-02 took 29.5 s
and 17.7 s, of which actual retrieval (vector, keyword, graph, fusion, rerank)
was 2.9 s and 1.4 s. The rest was two whole-vault walks per request:
`filter_eligibility` (18.1 s, 7.9 s), which resolves a structured filter by
reading every page's frontmatter whenever the filter cannot be answered from the
index, and `outside_kb` (7.6 s, 8.3 s), the `scope="kb"` auto-widening pass that
re-runs eligibility with vault scope and then a BM25 pass over every non-KB file.
The same two stages cost about 40 ms on 2026-08-31, when recall median was
1.4 s, and recall median was 719 ms on 2026-09-01. Every call over Cloudflare's
100-second edge cap surfaced to the ChatGPT connector as a 502.

The remaining gap between 700 ms and the 300 ms target is fixed per-request
cost that the 2026-08-31 recall-performance plan already attributed: the
embedding matrix copy (#951, landed), the query embedded twice per mixed-level
recall (#984, closed), and three stages that wrote into the timings table
without registering an interval, so `unattributed_ms` double-counted them
(#983, closed). Those fixes have never been measured together on the live cell,
and there is no gate that would notice if any of them regressed. The
`accelerate-durable-write-acknowledgement` change (0.69.0) removed the write-side
floor; this change removes the read-side one.

## What Changes

- Structured-filter eligibility becomes index-backed for every supported filter:
  `projects`, `tags`, `types`, categories, kinds, speakers and file types resolve
  from the maintained catalogue or the semantic-unit sidecar, and a request that
  cannot be answered from an index returns the typed warming outcome instead of
  walking the Markdown scope on the reader thread.
- Outside-KB widening for `scope="kb"` becomes opt-in and index-backed. The
  default `kb` scope serves the KB only; a caller that wants the reserve asks
  for it, and the reserve then runs one catalogue-backed BM25 query with the
  eligibility set resolved from the same index, never a second vault walk.
- Read-side caches take exact custody from the derived receipts: a governed
  write invalidates only the catalogue rows, eligibility entries and BM25
  postings for the paths it changed, through the same batch receipts and
  pending-visibility rows that already give writes exact custody. A whole-scope
  freshness change no longer discards the lexical corpus or the eligibility
  catalogue.
- Span accounting is made complete and enforced: every stage that reports time
  registers an interval, `sum(stages) + unattributed_ms <= total_ms` holds for a
  real `op_find`, and `unattributed_ms` is bounded.
- A live-cell recall latency contract replaces the catastrophic-blowup backstop:
  hybrid p50 at or below 300 ms and p95 at or below 600 ms on a quiescent cell
  of at least 8,000 pages, keyword p50 at or below 120 ms, zero corpus walks on
  the read path, measured by the existing timing diagnostics and checked by a
  script that refuses to measure under load rather than reporting noise.
- The graph rebuild's whole-vault optimistic check stays out of scope; it is
  owned by `converge-graph-incrementally`. This change only ensures a rebuild in
  flight cannot invalidate the read-side caches.

## Capabilities

### New Capabilities
- `recall-latency-contract`: the live-cell recall latency ceilings, the
  no-corpus-walk read-path invariant, the quiescence and attribution rules for
  measuring them, and the regression gate that enforces them.

### Modified Capabilities
- `structured-retrieval-filters`: `Governed Unit Metadata Is Filterable` gains the
  requirement that eligibility for every supported filter resolves from an index
  and never walks the Markdown scope on the reader thread; an unanswerable plan
  yields a typed warming outcome.
- `find-recall-efficiency`: `Hot Find Cache With Freshness Invalidation` is
  narrowed from whole-scope invalidation to exact path custody; `Optional Find
  Timing Diagnostics` gains completeness (every material stage is an interval and
  the sum bound holds); a new requirement makes `scope="kb"` widening opt-in and
  index-backed.
- `recall-read-path`: `Server Recall Never Rebuilds Projection On The Reader
  Thread` is extended from the recall projection to the lexical corpus and the
  eligibility catalogue.

## Impact

- Code: `src/exomem/find.py` (eligibility resolution, outside-KB widening, spans),
  `src/exomem/find_candidates.py` (spans, candidate hydration),
  `src/exomem/structured_filters.py` (index plan coverage), `src/exomem/lexstore.py`
  and `src/exomem/bm25.py` (exact-path invalidation, catalogue-backed reserve),
  `src/exomem/freshness.py` and `src/exomem/pending_recall.py` (receipt-driven
  invalidation), `src/exomem/find_types.py` (timing merge), `src/exomem/commands.py`
  (the widening option on `ask_memory`/`find`), `scripts/recall_latency_gate.py`
  (new), `tests/test_recall_latency_gate.py` (new), `tests/test_read_path_timing_attribution.py`.
- APIs: `ask_memory` and the `find` leaf gain an explicit widening option; the
  default `scope="kb"` result set changes for callers that relied on out-of-KB
  reserve hits appearing without asking. The MCP tool surface moves, so the
  hosted artifacts, the ChatGPT plugin pending digest and the v1 release
  identities are regenerated in the same delivery.
- Dependencies: the derived batch receipts and pending-visibility rows shipped in
  `accelerate-durable-write-acknowledgement`; the maintained FTS5 catalogue; the
  semantic-unit sidecar.
- Operations: the live cell shares its box with test suites; the gate refuses to
  run above a load average of 2.0 and records the load it ran under, so a
  contended measurement is never mistaken for a regression.
