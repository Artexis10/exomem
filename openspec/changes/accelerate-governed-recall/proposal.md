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

On 2026-10-09 the owner set the target for every search mode (semantic, BM25,
keyword): subsecond and well under, with 100 to 200 ms acceptable. The 300/600 ms
ceilings this change first set do not meet that, and the Cloud service gate (p95
at or below 3 s in `cloud-service-resource-policy`) is fifteen times looser. A
reproduction on a 6,500-page synthetic vault in a process pinned to two CPUs
measured warm hybrid `ask_memory` at p50 363-700 ms and p95 636-1,799 ms over
three runs, and the `mixed` result level at p50 871 ms and p95 2,460 ms
(`baseline.md`). Those runs were on a loaded laptop, with the owner path
obtained by patching the authority check. The cost is not the ranking
arithmetic. It is per-request work that the catalogue generation already
determines: a parent-hint query whose plan scans the candidate list once per KB
page, nine catalogue connections and six readiness proofs per request, 92 page
hydrations and 126 Markdown file reads per request to rank 15 hits, and unit
lanes that re-parse every candidate parent. No pull-request gate counts this
work. Full CI run 37969875369 on 2026-10-09 found two linear-in-corpus
regressions only after merge, in `activate_context` carry ranking and in
semantic validate. Neither was in `ask_memory`, but `activate_context` shares
its read entry points.

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
  registers an interval, the sum of the root-level stages plus `unattributed_ms` stays within
  `total_ms` for a
  real `op_find`, and `unattributed_ms` is bounded.
- A recall latency contract replaces the catastrophic-blowup backstop: fixed
  ceilings (first hybrid p50 300 ms and p95 600 ms on 8,000 pages, tightened on
  2026-10-09 as below), zero corpus walks on the read path, measured by the
  existing timing diagnostics and checked by a script that refuses to measure
  under load rather than reporting noise.
- The graph rebuild's whole-vault optimistic check stays out of scope; it is
  owned by `converge-graph-incrementally`. This change only ensures a rebuild in
  flight cannot invalidate the read-side caches.
- The latency contract tightens to warm search p95 at or below 200 ms, with a
  100 ms p50 target, on a two-CPU cell of at least 6,500 pages. It covers hybrid,
  keyword, filtered and `mixed`/`unit` requests, measured as the elapsed time of
  the `ask_memory` call. Keyword recall keeps its 120 ms p50 ceiling, and the
  empty-query browse stays outside the ceilings.
- The measured principal is the real vault owner, with nothing patched. An
  admitted non-owner series is reported apart and gates once the admission
  candidate-sizing fix lands.
- Each stage of the request gets a p95 budget, named by its timing span keys,
  and the budgets sum below the ceiling. The gate names a stage that exceeds
  twice its budget, and reports process CPU per stage. Query encode keeps the
  bound that `multilingual-recall` already sets; its stage budget is a target.
- Search work is bounded per candidate and per matched row. Catalogue queries
  keyed by the candidate set do work that does not grow with the corpus for a
  fixed candidate set. BM25 and keyword do bounded work per matched row.
  Ranking, temporal, graph-seed and unit stages read metadata from the catalogue
  and sidecars of the current generation. Markdown reads track the candidate
  count plus a fixed overfetch, and each derived store opens one connection and
  runs one readiness proof per scope per request.
- Timing diagnostics cover the whole `ask_memory` request, including the
  due-state block that runs after retrieval, and an unreported duration is
  shown as unknown, never as 0 ms.
- Three instruments enforce it. A model-free structural gate in the
  pull-request tier compares work per candidate and per matched row between
  400- and 1,600-page generated corpora that hold the same candidates. In the
  scheduled and dispatched full CI, a paired comparison runs head against the
  last release tag on one runner and fails only when head is at least 10%
  slower with 95% confidence. The absolute verdict on the ceilings and budgets
  comes from a quiet workstation run and the live-cell series, and it is the
  release evidence for those numbers.
- Out of scope: the cold-start index build, explicitly requested or
  accelerator-driven reranking, and RAW admission candidate sizing for non-owner
  callers, which `fix/admission-candidate-sizing` owns.

## Capabilities

### New Capabilities
- `recall-latency-contract`: the warm search latency ceilings and per-stage
  budgets, the no-corpus-walk invariant and the per-candidate work bounds, the
  reference corpus, the quiescence and attribution rules for measuring them,
  and the pull-request, full-CI and release instruments that enforce them.

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
- Subsecond search slices (tasks section 6): `src/exomem/lexstore.py` (parent-hint
  query, one catalogue read session per request), `src/exomem/find_candidates.py`,
  `src/exomem/find_policy.py` and `src/exomem/find.py` (ranking metadata from
  catalogue columns, unit-lane hydration), `src/exomem/embedding_index.py` (chunk
  text after fusion, unit matrix per generation), `src/exomem/commands.py` and
  `src/exomem/due_state.py` (response blocks inside the timings),
  `src/exomem/find_types.py` (`cpu_ms` per span), `src/exomem/runtime_resources.py`
  and `src/exomem/embedding_backend.py` (the cell thread policy),
  `scripts/synth_vault.py` (reference corpus), `scripts/recall_latency_gate.py`
  and its test (new ceilings and budgets, the paired mode), a new pull-request
  structural test, and `.github/workflows/ci.yml` (the `retrieval-latency`
  job). The tool surface does not move.
