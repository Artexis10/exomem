## Context

See proposal.md for the measurements. The shape of today's read path that the
design has to respect:

- A recall's structured-filter plan is classified by `plan_index_candidates`.
  Only exact positive `unit.category` and `unit.kind` clauses are "complete"
  and seed from the semantic-unit sidecar; every page-level field (`projects`,
  `tags`, `types`, speakers, file types) is "unsupported" and falls to the
  canonical full-scan oracle, which walks the Markdown scope and parses
  frontmatter on the reader thread. That oracle is also the definition of
  correctness: an index-backed answer must equal it for the same generation.
- `scope="kb"` auto-widens through the out-of-KB reserve, which resolves
  eligibility again with vault scope and then runs BM25 over every non-KB page,
  building a Python corpus when the maintained FTS5 catalogue is not fresh.
- Substrate caches (lexical corpus, eligibility catalogue, frontmatter cache,
  embedding matrix) key on whole-scope freshness keys. Any governed write moves
  the key, so on a busy day every recall rebuilds what the previous write
  discarded. The `accelerate-durable-write-acknowledgement` change already
  gives each governed write an exact receipt naming its paths and a
  pending-visibility row per path; nothing on the read side consumes them for
  invalidation yet.
- Timing diagnostics merge registered intervals into `unattributed_ms`; the
  stages that #983 converted are intervals now, but nothing enforces that a new
  stage is, and stages do not say whether they were answered from an index.
- The recall projection admission and the lexical repair worker already
  implement the "warm away from the reader" pattern this change extends.
- The 2026-08-31 plan's tranche 1 (#951, matrix copy) is on main; #983 and #984
  are closed. Their combined effect on the live cell has not been measured.
- The live cell runs in WSL on a box shared with test suites; the graph
  scheduler's whole-vault rebuild can livelock under a sustained writer
  (`converge-graph-incrementally`).

## Goals / Non-Goals

**Goals:**
- Zero corpus walks on the reader thread for any supported request shape,
  enforced by diagnostics and a test sentinel, not by review.
- Read-side caches that survive governed writes through exact receipt custody.
- A measured, ratcheting latency contract on the live cell.
- Result identity: every index-backed path returns the set the scan oracle would.
- Warm search p95 at or below 200 ms on a two-CPU cell of 6,500 pages, with a
  budget for every stage and a pull-request gate that sees corpus-proportional
  work before merge.

**Non-Goals:**
- Approximate retrieval, dropped features, ANN, or any quality-for-speed trade.
- Changing what hybrid recall computes (embed, vector, graph, fusion, rerank).
- Fixing the graph rebuild's optimistic check (owned elsewhere).
- Rust, process changes, or moving the cell off the shared box.
- The cold-start index build, explicitly requested or accelerator-driven
  reranking, and RAW admission candidate sizing for non-owner callers
  (`fix/admission-candidate-sizing` owns it).

## Decisions

### 1. Page-level filters get a maintained page-metadata index

A page-metadata table keyed by relative path holds the filterable frontmatter
fields (`type`, `projects`, `tags`, `speakers`, file type, status) plus the
page's content hash and the generation that wrote it. It lives in the existing
lexical catalogue store, is written by the existing writer fan-out component
that already touches the catalogue on every governed write, and is rebuilt by
the same single-flight repair worker that rebuilds the catalogue.
A page-axis eligibility plan is compiled alongside the unit seeds rather than
folded into `plan_index_candidates`: widening the unit planner would change the
bounded-prefix read the unit lane depends on. A plan is "complete" when every
clause is either a unit clause the sidecar answers or a page clause the metadata
table answers; AND/OR/NOT composition stays exactly as today, with complements
taken only over clauses the index answers exactly.

The oracle stays. It becomes the identity test's reference and the offline
(unmanaged) fallback, never a managed reader's path.

The SQL candidate columns can conservatively narrow a timestamp or unusual
YAML value without deciding it exactly. Final evaluation therefore consumes
typed page views and semantic-unit metadata from the same maintained catalogue,
not freshly opened Markdown. The snapshot preserves absent versus null fields,
scalar versus list values, and date versus timestamp precision. A catalogue
schema upgrade rebuilds this derived metadata through the existing background
repair; missing or stale metadata yields typed warming, never a source scan.

Empty-query browsing orders emitted identities using maintained dates and
parent/frame metadata before opening page snapshots. It merges current pending
metadata into that ordering, retaining the exhaustive browse's first-candidate
excerpt and first-frame annotation for each parent. Only selected groups are
hydrated: one page for an ordinary result, plus a frame snapshot when that
frame supplies the selected parent's excerpt. Unavailable, incomplete or stale
ordering yields typed warming rather than removing the read bound.

**Alternative rejected: derive page eligibility from FTS5 columns.** The
catalogue stores text for ranking, not typed metadata; encoding list fields
into it makes `$in` and `$exists` semantics a text-matching approximation.

### 2. Exact custody flows from the receipts to the read-side caches

Each substrate cache registers an invalidation seam keyed by relative path.
When a governed write commits, the receipt's path set is applied to those
seams: rows for those paths are evicted or refreshed; nothing else moves. The
whole-scope freshness key keeps its role for receipt-less changes only: the
reconciliation pass that detects an external edit invalidates the scope it
found drift in, as today.

The pending-visibility overlay already shadows stale rows for paths pending
custody and re-offers the committed pages; the eligibility evaluation consumes
the same overlay so a filter sees the committed frontmatter before the
catalogue row is refreshed.

**Alternative rejected: keep whole-scope keys and make rebuilds cheaper.** A
rebuild that costs 8 s at 8k pages costs 32 s at 32k; the fix has to remove
the rebuild from the request, not shave it.

### 3. Out-of-KB widening is a request option with a hard reserve

`scope="kb"` serves the KB. A new boolean request option enables widening; the
`ask_memory` product default is off. When on, widening runs one catalogue query
over the out-of-KB eligible set (from the same metadata index) and reserves at
most `limit - 1` slots, as today. If the catalogue is not live the stage
declines and says so in the diagnostics. The MCP tool surface changes, so the
hosted artifact set is regenerated in the delivery (see the surface-change
pattern in the knowledge base).

**Alternative rejected: delete the reserve.** The reserve exists for terse
out-of-KB pages whose title is the query; removing it silently changes answers
for callers that rely on it. Opt-in keeps the behaviour reachable and stops the
default from paying for it.

### 4. Timing completeness is a property test, and stages carry a source

`FindTimings.span` remains the only way a stage gets a duration; a manual write
into the stages table is rejected by construction (the table becomes
write-through from spans). Every span records a `source` in
{`index`, `cache`, `declined`, `computed`}. A test drives the real public leaf
with timing enabled and asserts the two attribution bounds; a second test
asserts that no stage on the reference corpus reports a walk. The walk sentinel
is a directory-enumeration counter installed for the duration of the request
in tests, so the assertion is structural, not a timing threshold.

### 5. The latency gate refuses to measure under load

`scripts/recall_latency_gate.py` runs the series from the proposal against the
live cell over the direct transport, novel query per sample (nonce), warm
caches, and reads the per-stage diagnostics. It checks the one-minute load
average before starting and between samples; above 2.0 it waits a bounded
time and then exits without a verdict, naming the load. It records the load it
ran under next to every percentile. Ceilings live in the script as the
contract and are not calibrated from the runner.

CI keeps a model-free structural guard (the walk sentinel and the attribution
bounds) on every PR; the live-cell numbers are produced on the operator's box
at delivery and after each release, because CI runners cannot host the 8k-page
warm cell. Decision 9 adds a generated-corpus gate to the full CI.

### 6. Delivery order follows measurement, not ambition

Tranche order: (1) walk sentinel and timing completeness, because every later
claim is measured through them; (2) page-metadata index and index-backed
eligibility, the largest win; (3) exact custody invalidation; (4) opt-in
widening and the surface regeneration; (5) the live-cell gate and the
before/after series, including re-measuring #951/#983/#984 together on the
live cell for the first time. Each tranche is a lane with its own
author-independent reviewer and mutation proofs, after the pattern that
delivered the write-side change.

### 7. Warm search costs at most 200 ms at p95 on a two-CPU cell

The owner set the target on 2026-10-09: every search mode subsecond and well
under, 100 to 200 ms acceptable. The contract takes the top of that range as
the p95 ceiling and the bottom as the p50 target. The reference is 6,500
pages, about the size of the owner's vault, on two CPUs, the allocation of a
Cloud cell. The personal service has more CPUs, so meeting the contract on two
leaves it headroom there. The measured quantity
is the elapsed time of the `ask_memory` call, which is what a client waits for
before transport. The live-cell gate measures the same call at the REST facade.

`baseline.md` records the reproduction. Its third run (76 samples, load average
16-17 on a shared 20-CPU laptop, process pinned to two CPUs) is the reference
profile below. Run 1 and run 2 measured most stages at one to four times these
values. Every number was taken under load, so a quiescent cell is expected to be
faster; the gate, not this table, decides the verdict.

| Stage | p50 / p95 ms | Cause | Budget |
|---|---|---|---|
| Request setup | 9 / 28 | projection and pending-visibility checks | 5 |
| Admission | not run (owner) | non-owner path: 214 / 440 ms model-free, sized by the other lane | 15 |
| Query encode | 33 / 116 | bge-m3 int8 on two contended CPUs | 40 |
| Dense search | 33 / 52 | chunk text for 3 x `candidate_k` rows (`embedding_index.py:1822`) | 15 |
| BM25 | 29 / 56 | FTS5 `bm25()`; the rest is connection setup and readiness | 10 |
| Keyword | 39 / 91 (up to 177 / 342) | trigram query plus connection setup and readiness | 10 |
| Unit lanes (`mixed`) | 647 / 2,143 | per-candidate parent re-parse and policy load | 30 |
| Parent hints | 85 / 132 | page-table scan per candidate (`lexstore.py:8382`) | 2 |
| Graph | 26 / 135 | seed excerpts through Markdown reads, expansion | 15 |
| Temporal | 44 / 124 | `updated` read from Markdown per candidate | 5 |
| Fusion and multipliers | 24 / 66 | type and status read from Markdown per candidate | 10 |
| Hydration and response | 30 / 79 | due-state block outside `total_ms`, serialization | 20 |
| CLIP | not measured | 37 ms warm on the live cell (`baseline.md`, 2026-09-03) | 10 |

The budgets sum to 187 ms, so a request that runs every stage still has
headroom. A default page-level request for the owner runs ten of them, 132 ms in
total. The encode budget is the one this change cannot buy with code: the
Cloud lane measured 45 ms on one thread, and the laptop 33 ms p50 on two. If a
quiescent cell cannot hold 40 ms at p95, the next step is an evaluation of a
smaller served encoder, not a wider ceiling.

### 8. The catalogue generation is the unit of precomputation

Every slow stage pays per request for something the catalogue generation
already fixes. Each fix keeps results identical, and an identity test compares
it with the current path on the reference corpus.

- **Parent hints.** `_emitted_parent_hints_query` (`lexstore.py:8382-8394`) joins
  `pages` to `json_each(?)`, and SQLite drives the join from `pages_kb`, so it
  scans the page table once per candidate. Driving it from `json_each` with a
  primary-key lookup per path returned the same rows in 0.2 ms instead of
  135 ms for 300 paths, and 1.0 ms instead of 488 ms for 1,000.
- **One catalogue read session per request.** Each catalogue query
  (`_serve_from_ready_catalog_result`, `lexstore.py:7770`) runs a readiness proof
  on its own connection, then opens a second connection and proves the
  checkpoint, schema and semantic identity again before it queries.
  `catalog_semantic_identity` (`lexstore.py:1040`) rereads the registry file and
  hashes it every time, and every connection goes through the reserved-path
  ownership checks (`lexstore.py:3346-3428`). A request opened nine connections
  and ran six proofs: 36 to 85 ms of setup across runs. One session per request,
  proved once against the request's checkpoint, serves BM25, keyword, parent
  hints and ranking metadata. The identity is computed once per generation.
- **Ranking metadata from catalogue columns.** Multipliers
  (`find_policy.py:264`), recency (`find_policy.py:495`), graph seeds
  (`find_candidates.py:702-708`) and the lexical guard (`find_candidates.py:634`)
  read each candidate through `_page_of`. `FrontmatterCache.get`
  (`find_corpus.py:218`) reads and hashes the whole file even on a cache hit,
  and `is_recall_candidate` classifies the path again (`recall_policy.py:182`).
  That is 92 hydrations and 126 file reads per request to return 15 hits. The
  `pages` table already holds `page_type`, `status`, `updated` and
  `emitted_parent_path` under the same receipt custody, so ranking reads them
  there and Markdown is read only for returned hits.
- **Unit lanes.** `_hydrate_indexed_unit_records` (`find.py:2188`) rebuilds each
  candidate parent's unit state (`semantic_index.py:250`, `:189`), then resolves
  its prose carriers through a governance policy load per parent
  (`find_results.py:15`, `governance/egress.py:4342`, `governance/policy.py:1297`).
  `search_semantic_units` stacks the candidate unit vectors on every query
  (`embedding_index.py:1765`). The unit sidecar already holds the parent
  generation and unit metadata for the current generation, the policy is one
  snapshot per request, and the unit matrix is built once per generation.
- **Dense search.** The vector lane asks for 3 x `candidate_k` chunk rows and
  hydrates the text of every row with an OR of primary-key pairs
  (`embedding_index.py:1822`), but it keeps only the best chunk per file, and
  only returned hits use it. Text is hydrated after fusion, for returned hits
  only.
- **Response blocks.** `_with_due_state` (`commands.py:6427`) runs after the
  find timings close, so 21 / 58 ms of the request is outside `total_ms`. It
  becomes a registered stage and reads role state once per request instead of
  once per hit (`artifact_role_state.py:494`).

The temporal lane decides "temporal" with a word regex (`find_policy.py:427`).
That is old C6 debt; this change moves where `updated` is read from, not how the
lane is chosen.

### 9. Two gates: counts on every pull request, wall clock before release

The canonical `Structural Scaling Is The CI Gate` requirement already says that
pull-request CI uses operation counts and leaves timing to release evidence.
Search follows it:

- **Structural gate, pull-request tier.** Model-free, on generated corpora of
  1,600 and 6,500 pages. Per warm request it counts Markdown page reads,
  connections per derived store, readiness proofs, and SQLite virtual-machine
  steps through `sqlite3.Connection.set_progress_handler`. Counts are
  deterministic for a fixed corpus and query, so the gate fires only when the
  work changes. A per-candidate scan such as the parent-hint plan grows the step
  count with the corpus and fails here before merge. The full CI latency jobs
  found the last linear-in-corpus regression only after merge (run 37969875369
  on 2026-10-09: context compiler 208.9 ms at 2,000 pages to 826.8 ms at 8,000).
- **Wall-clock gate, scheduled and dispatched full CI.** It extends
  `scripts/recall_latency_gate.py`'s in-process transport. The corpus comes from
  `scripts/synth_vault.py` with the profile the requirement names. The process
  pins itself to two CPUs. Queries are encoded by the served encoder from the
  published artifact, cached by digest. Corpus chunk and unit vectors are seeded
  unit vectors written through the product's own index build and stamped with
  the served encoder's identity, because embedding 46,000 chunks at the 6.7
  chunks per second the Cloud lane measured on two threads takes about two
  hours. Dense-search cost depends on the matrix size and the candidate
  counts, not on the vector values, and ranking quality is not this gate's
  question. It runs the hybrid, keyword, filtered and `mixed` series from a
  fixed list of varied queries with a run nonce, and fails on the ceiling, on a
  stage over twice its budget, and on any unknown sample. It belongs in the
  `retrieval-latency` job, not in the pull-request tier, because shared-runner
  wall clocks vary run to run and the canonical requirement forbids flaky timing
  there. Release evidence already requires a passing full run.
- **Live cell, after each release.** The same series through the served
  transport, as task 5.6 already plans.

### 10. Slices follow measured gain, after the gate

The gate and the structural counters land first, so every slice reports a
before and an after on the same instrument. The slices then follow the measured
saving on a default hybrid request, with the unit lanes placed by their saving
on `mixed` requests. Tasks section 6 lists them.

## Risks / Trade-offs

- **[Risk] The page-metadata index drifts from frontmatter.** → It is written
  by the same component and receipt that publish the catalogue; the identity
  test compares it against the scan oracle on the reference corpus, and
  reconciliation audits drift the way the graph is audited.
- **[Risk] Exact invalidation misses a path.** → Receipts name every path a
  write touched, moves included; the audit compares cache rows against
  frontmatter after a burst; an unmatched row fails closed to a scope
  invalidation, never to a stale answer.
- **[Risk] Default `scope="kb"` results change for callers that relied on the
  reserve.** → Called out as a behaviour change; the option is on the surface;
  the connector docs name it.
- **[Risk] The gate never sees a quiet box.** → It refuses rather than reports;
  the operator can pause suites, and the structural guards still run in CI.
- **[Risk] A faster read path raises the write rate a client sustains, and the
  graph rebuild livelocks more often.** → Owned by
  `converge-graph-incrementally`; this change ensures a rebuild in flight cannot
  invalidate the read-side caches.

- **[Risk] A request session pins an old catalogue generation.** → The session
  opens after the request's readiness proof and holds that checkpoint for the
  whole request, as `_serve_from_ready_catalog_result` does for one query today.
  A publication during the request is caught by the same checkpoint check.
- **[Risk] Seeded corpus vectors hide a dense-search cost.** → Dense cost is a
  function of the matrix size and the candidate counts, which the seeded corpus
  keeps; the live-cell series after each release runs on real vectors.
- **[Risk] The encode budget is wrong for a slower CPU.** → It was measured under
  load, not on a quiescent cell. The gate reports encode against its budget, and
  a miss leads to an encoder evaluation, not a wider ceiling.

## Migration Plan

1. Land tranches 1 to 3 behind no flag: they are result-identical by
   construction and proven by the identity tests.
2. Land tranche 4 with the widening option default off; regenerate the hosted
   artifacts, the ChatGPT pending digest and the v1 release identities in the
   same commit.
3. Release; upgrade the live cell; run the gate; record before/after in the
   change; then archive.

Rollback is a release rollback; no data migration, since the metadata index is
derived and rebuilds from the vault.

## Open Questions

- Whether `speakers` and file-type filters need the metadata table or can stay
  on the media sidecar they read today; decided by the lane that inventories
  the filter registry, without changing the specs.
