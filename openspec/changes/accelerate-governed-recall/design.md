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
  budget for every stage, a pull-request gate that sees per-candidate work grow
  before merge, and a full-CI comparison of head with the last release that
  passed its live-cell verdict.

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
average once, before the series; above 2.0 it waits a bounded time and then
exits without a verdict, naming the load. It does not check between samples:
the measured process keeps up to two cores busy, so its own load would refuse
its own series. It records the load it ran under next to every percentile. On
the live cell, contention during the series is judged per sample instead, from
the request thread's run-queue delay (decision 9). Ceilings live in the script
as the contract and are not calibrated from the runner. The shipped script
still checks between samples; task 6.1 moves it to the single check.

The live-cell series appends a token to each query because it cannot reach the
result cache. That token changes what the lexical lanes match, so the report
says so. The in-process gate clears only the result cache between samples and
sends the query text unchanged.

CI keeps a model-free structural guard (the walk sentinel and the attribution
bounds) on every PR; the live-cell numbers are produced on the operator's box
at delivery and after each release, because CI runners cannot host the 8k-page
warm cell. Decision 9 adds the structural search gate and the paired full-CI
comparison.

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
leaves it headroom there. Only the reference-corpus runs are pinned to two
CPUs; the live personal service is measured as it is served. The measured
quantity
is the elapsed time of the `ask_memory` call, which is what a client waits for
before transport. The live-cell gate measures the same call at the REST facade.

The measured principal is the vault owner, identified through the product's
owner-authority path. The reproduction did not do that: it patched
`raw_protection.has_unrestricted_access` to return true, so its owner-path
numbers are patched numbers (`baseline.md`). An admitted non-owner principal
runs the same series, reported apart. It gates once admission no longer sizes
the candidate pool; today that path runs Python BM25 over the admitted pages. The model-free A1 run, which took the in-process
principal's RAW admission predicate, measured that path at 6.8 s p50.

The old keyword p50 ceiling of 120 ms stays. It is the contract's only p50
ceiling, and dropping it would loosen the keyword bound. The empty-query browse
stays outside the ceilings, as the shipped gate already treats it.

`baseline.md` records the reproduction. Its third run (R3: 76 samples, load
average 16-17 on a shared 20-CPU laptop, process pinned to two CPUs) is the
reference profile below. R3 ran with CLIP off (`EXOMEM_DISABLE_CLIP=1`), the
reranker hard-off and the patched owner path. Run 1 and run 2 measured most
stages at one to four times these values. Every number was taken under load, so
a quiescent cell is expected to be faster; the gate, not this table, decides
the verdict.

| Stage | p50 / p95 ms | Cause | Budget |
|---|---|---|---|
| Request setup | 9 / 28 | projection and pending-visibility checks | 5 |
| Admission | not run (patched owner) | non-owner path: 214 / 440 ms model-free, sized by the other lane | 15 |
| Query encode | 33 / 116 | bge-m3 int8 on two contended CPUs | 40, a target under the 250 ms bound of `multilingual-recall` |
| Dense search | 33 / 52 | chunk text for 3 x `candidate_k` rows (`embedding_index.py:1822`) | 15 |
| BM25 | 29 / 56 | FTS5 `bm25()`; the rest is connection setup and readiness | 10 |
| Keyword | 39 / 91 (up to 177 / 342) | trigram query plus connection setup and readiness | 10 |
| Unit lanes (`mixed`) | 647 / 2,143 | per-candidate parent re-parse and policy load; the unspanned query encode runs inside it | 30 |
| Parent hints | 85 / 132 | the plan scans `json_each` once per KB page (`lexstore.py:8382`) | 2 |
| Graph | 26 / 135 | seeds 6 / 30 through Markdown reads; expand 14 / 31; resolver 5 / 16 | 15 |
| Temporal | 44 / 124 | `updated` read from Markdown per candidate | 5 |
| Fusion and multipliers | 24 / 66 | type and status read from Markdown per candidate | 10 |
| Hydration and response | 30 / 79 | due-state block outside `total_ms`, serialization | 20 |
| CLIP | not run (`EXOMEM_DISABLE_CLIP=1`) | 37 ms warm on the live cell (`baseline.md`, 2026-09-03) | 10, where CLIP is enabled |

The budgets sum to 187 ms, so a request that runs every stage still has
headroom. A default page-level request for the owner runs at most ten of them,
132 ms in total when the temporal lane runs.

Every stage is over its budget today, at p95 by two to sixty times. Slices 6.3
to 6.8 remove named per-request work from most of them. Three rows have no
removable work named yet, and for 200 ms at p95 to hold they need
constant-factor speed:

- Request setup, 9.4 ms at p50 and 28.1 at p95 against a 5 ms budget, must at
  least halve at p50 and fall about sixfold at p95 (slice 6.11).
- Graph expand and resolver, 13.9 and 5.1 ms at p50 and 31.4 and 16.3 at p95,
  must fit the lane's 15 ms once 6.5 moves the seeds. That means at least
  halving at p50 and falling to about a third at p95 (slice 6.12).
- Query encode must fall to about a third at p95, from 116 ms to 40.

Encode is the one this change cannot buy with code: the Cloud lane measured
45 ms on one thread, and the laptop 33 ms p50 on two. `multilingual-recall`
already bounds encode p95 at 250 ms, so the 40 ms row is a diagnostic target
that cites that bound, not a second bound on one quantity. If a quiescent cell
cannot hold 40 ms at p95, the next step is an evaluation of a smaller served
encoder, not a wider ceiling. The CPU spin slice (decision 11) comes first,
because a request that keeps a second core busy slows every stage on a two-CPU
cell.

### 8. The catalogue generation is the unit of precomputation

Every slow stage pays per request for something the catalogue generation
already fixes. Each fix keeps results identical, and an identity test compares
it with the current path on the reference corpus.

- **Parent hints and eligibility metadata.** `_emitted_parent_hints_query`
  (`lexstore.py:8382-8394`) joins `pages` to `json_each(?)`. SQLite walks the
  `pages_kb` index and scans `json_each` once per KB page, so the work is KB
  pages times candidates. The `IN` form does not fix it. `lexstore` never runs
  `ANALYZE`, so `p.path IN (SELECT value FROM json_each(?)) AND p.in_kb = 1`
  plans as `SEARCH p USING INDEX pages_kb (in_kb=?)`: about 67,000 to 74,000
  steps at 6,500 pages. `_eligibility_metadata_query` (`lexstore.py:2837`)
  uses that `IN` form and scans the same way: 23,160 steps at 1,600 pages and
  77,060 at 6,500. The proven form is
  `FROM json_each(?) r CROSS JOIN pages p ON p.path = r.value`. `CROSS JOIN`
  fixes the join order, so `json_each` drives a primary-key lookup; it held
  flat at 1,016 steps at both sizes. A replay of that plan returned the same
  rows in 0.2 ms instead of 135 ms for 300 paths, and 1.0 ms instead of 488 ms
  for 1,000. Slice 6.3 moves both queries to that form and confirms the plan
  with `EXPLAIN QUERY PLAN`. It lands after the admission candidate-sizing
  work, which edits `_eligibility_metadata_query` (decision 10).
- **One catalogue read session per request.** Each catalogue query
  (`_serve_from_ready_catalog_result`, `lexstore.py:7770`) runs a readiness proof
  on its own connection, then opens a second connection and proves the
  checkpoint, schema and semantic identity again before it queries. Every
  connection goes through the reserved-path ownership checks
  (`lexstore.py:3346-3428`). A request opened nine connections and ran six
  proofs: 36 to 85 ms of setup across runs. A per-request catalogue session
  serves BM25, keyword, parent hints and ranking metadata. It opens one
  connection and runs one proof per store and scope, against the request's
  checkpoint. Opt-in widening adds the vault scope, so it adds one proof for
  that scope. The session closes with the request; no connection is retained
  across requests.
- **Semantic identity.** `catalog_semantic_identity` (`lexstore.py:1040-1088`)
  rereads and hashes the semantic-language registry
  (`<KB>/_Schema/semantic-language-registry.yaml`) on every call. The identity
  invalidates catalogue rows when the registry or the authoring contract changes
  with no note change, and a registry edit does not move the recall checkpoint.
  A memo per catalogue generation would therefore serve a stale identity. The
  identity is memoized on its inputs instead: the registry file's stat signature
  (`st_mtime_ns`, `st_size`, `st_ino`, `st_ctime_ns`) plus the authoring
  contract's version. When any of them moves, the identity is hashed again.
- **Ranking metadata from catalogue columns.** Multipliers
  (`find_policy.py:264`), recency (`find_policy.py:495`), graph seeds
  (`find_candidates.py:702-708`) and the lexical guard (`find_candidates.py:634`)
  read each candidate through `_page_of`. `FrontmatterCache.get`
  (`find_corpus.py:218`) reads and hashes the whole file even on a cache hit,
  and `is_recall_candidate` classifies the path again (`recall_policy.py:182`).
  That is 92 hydrations and 126 file reads per request to return 15 hits. The
  `pages` table already holds `page_type`, `status`, `updated` and
  `emitted_parent_path` under the same receipt custody, so ranking reads them
  there. Two consumers need data derived from the page body:
  - The lexical guard needs each page's `stem_set` and `letter_script`
    (`find_policy.py:754-809`). Slice 6.5 adds them as derived catalogue
    columns.
  - Graph seeds test `make_excerpt` and `stem_tokens_present` against the page
    (`find_candidates.py:702-708`). Slice 6.5 answers that test from the
    catalogue's stored text.

  Results stay identical, and Markdown is read only for returned hits.
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
  (`embedding_index.py:1822`), but it keeps only the best chunk per file. Three
  consumers read that text:
  - frame collapse attributes frame children with it
    (`collapse_frame_children`, `find_candidates.py:94`, called at `:590`);
  - the rerank prefix uses it as each hit's passage (`find.py:4749`);
  - excerpts use it for returned hits (`find.py:4603`).

  Text is hydrated by primary key only for the rows that those consumers read.
- **Response blocks.** `_with_due_state` (`commands.py:6427`) runs after the
  find timings close, so 21 / 58 ms of the request is outside `total_ms`. It
  becomes the registered stage `due_state` and reads role state once per
  request instead of once per hit (`artifact_role_state.py:494`).

The temporal lane decides "temporal" with a word regex, `TEMPORAL_MARKERS`
(`find_policy.py:41`, read by `is_temporal_query` at `:427`). Three more
mechanisms decide intent from English words: `RELATIONSHIP_MARKERS`
(`find_policy.py:48`), `EXACT_LEADING` (`:54`) and `classify_intent` (`:434`),
which combines them. All four are old C6 debt. This change moves where
`updated` is read from, not how a lane or an intent is chosen, and it removes
none of them.

### 9. Three instruments: counts per pull request, a paired comparison in full CI, the absolute verdict at release

The canonical `Structural Scaling Is The CI Gate` requirement says that
pull-request CI uses operation counts, and that timing thresholds stay
workstation release evidence and must not make shared-runner tests flaky.
Search follows it.

**Structural gate, pull-request tier.** It is model-free, has no wall-clock
threshold, and compares work between two corpus sizes as ratios.

- *Sizes.* 400 and 1,600 generated pages. A ratio needs only the 4x size
  ratio, the same ratio as the canonical 2,000- and 8,000-page fixtures, and
  small corpora keep the pull-request tier fast.
- *One candidate set at both sizes.* The 1,600-page corpus is the 400-page
  corpus plus 1,200 filler pages. The filler's vocabulary is disjoint from the
  query list at the trigram level, for example in another script, and filler
  pages link only to each other. Every lane then yields the same matched rows
  and candidates at both sizes, and the test asserts that before it compares
  work.
- *Instrument.* SQLite virtual-machine steps through
  `sqlite3.Connection.set_progress_handler` on every catalogue connection,
  attributed to the timing span open when they run. Beside them: a connection
  counter per store and scope, the readiness-proof counter, and a Markdown
  page-read counter. Counts are deterministic for a fixed corpus, query and
  SQLite version, so the gate fires only when the work changes. It compares
  ratios only, so a SQLite upgrade that shifts every count does not fire it.
- *Candidate-keyed catalogue queries* (parent hints, ranking metadata,
  eligibility, hydration, readiness): steps at 1,600 pages stay within 1.5
  times the steps at 400.
- *BM25.* The lane matches an OR of the query terms, and FTS5 emits every
  matching row for `bm25()` to score and sort (`lexstore.py:7180`,
  `:7286-7292`). Steps per matched row stay within 1.5 times between sizes.
  The doclist merge inside FTS5's `xFilter` runs no virtual-machine steps, so
  the gate cannot see it. It reads each query term's posting list, so it is
  bounded by the term count times the matched rows; the wall-clock instruments
  own it.
- *Keyword.* A trigram MATCH narrows on tokens of three or more characters,
  `instr` verifies every token, and the survivors sort by `updated`
  (`lexstore.py:8441-8477`). Steps per trigram-matched row stay within 1.5
  times. The trigram doclist intersection is bounded by the posting lengths of
  the tokens' trigrams, not by the matched rows, and the gate cannot see it;
  the wall-clock instruments own it.
- *Excluded: keyword queries whose every token is under three characters.* No
  trigram can answer them, so the lane scans every KB row by design; the
  wall-clock query mix includes one.
- *Excluded: dense search.* Exact search is a linear scan of the chunk matrix
  by design, ANN is a non-goal, and the structural gate is model-free; the
  wall-clock instruments bound it.
- *Not observed.* Python work over in-memory structures, such as graph
  expansion and the carry's scoring loop. The wall-clock ratio checks in
  `tests/test_latency_gate.py` keep covering those.
- *Entry points and principals.* `ask_memory` and `activate_context`, as the
  vault owner and as one admitted principal. Full CI run 37969875369
  (2026-10-09) found two linear-in-corpus regressions only after merge: in
  `activate_context` carry ranking (the working-set compiler test, 208.9 ms at
  2,000 pages and 826.8 ms at 8,000) and in semantic validate. Neither was in
  `ask_memory`. The carry regression is SQL work, so the step counter would
  have caught it; the Markdown page-read counter would not. #1630 added
  `carry_term_statistics` (`lexstore.py:6858`). It reads every KB row of
  `pages`, then, for each stem, matches FTS rows against a `json_each` list of
  every admitted path. Both grow with the corpus while the matched rows stay
  the same, so the carry's steps per matched row fail the full-text bound. The
  carry's Markdown reads did not change in #1630. This attribution comes from
  the diff, not from a measurement. The admitted principal's bounds are
  reported from the start and gate once admission no longer sizes the
  candidate pool. Until then its BM25 falls back to Python `rank_bm25` over the
  admitted pages (`lexstore.py:7136-7145`), which grows with the admitted set.
- *Expected failure on main.* Two step ratios, each far above the 1.5 bound:
  - the parent-hint query, about 4x, because its plan scans `json_each` per KB
    page;
  - the eligibility metadata query (`_eligibility_metadata_query`), because its
    `IN` form plans as a scan of `pages_kb` (decision 8).

  Also expected: the ranking stages' Markdown reads, 126 per request in R3
  against 15 hydrated hits. BM25 and keyword pass, because their matched rows
  are the same at both sizes.

**Paired comparison, full CI.** The `retrieval-latency` job runs on shared
`ubuntu-latest` runners and feeds `gate` and release evidence, so it applies no
absolute threshold.

- *Arms.* Head and the pairing base, installed side by side in one job on one
  runner, against one reference corpus generated once, with the same cases. The
  pairing base is the last release whose live-cell verdict passed. Until a
  release has passed, it is the last release tag, and the job reports that no
  passed base exists.
- *Order.* Each case runs in both arms back to back, three times per arm, and
  the arm that goes first alternates between cases.
- *Statistic.* Per case, the ratio of head's median elapsed time to the
  pairing base's. Per series, the geometric mean of those ratios with its 95%
  confidence interval, a t-interval on the log ratios.
- *Verdict.* A series fails when the interval's lower bound is above 1.10:
  head at least 10% slower than the pairing base, with 95% confidence. A series
  with fewer than 20 paired cases reports "insufficient samples".
- *Stages.* Stage comparisons use the same statistic, name where the time
  moved, and fail nothing. One verdict per series keeps the false-fire rate at
  the interval's level instead of multiplying it by thirteen stages. A stage
  with fewer than 20 paired cases reports "insufficient samples". A stage that
  the pairing base does not span yet, such as `parent_hints` before 6.3,
  reports "not comparable".
- *Load.* Recorded, never refused: both arms share the runner's speed.
- *Unknown samples.* A sample without timings, or an encoder that cannot load,
  fails the series, because the instrument is broken.

Control justification:

- *It prevents* a merged change that slows warm search by 10% or more from
  reaching a release. The structural counters cannot see constant-factor
  regressions: a thread-policy change, a slower plan with the same step count,
  encoder settings or added Python overhead.
- *A wrong firing* keeps release evidence red until a rerun of the full run
  passes. The operator pays one rerun and a delayed release. No user and no
  pull request is gated.
- *Wrong firings are rare by construction.* The verdict needs the interval's
  lower bound above the margin, so at a true slowdown of exactly 10% it fires
  falsely in at most 2.5% of runs, and far less near no change. Alternating
  the arm order keeps runner drift out of the ratios. A second red on the same
  SHA is a regression.
- *No human decides.* The verdict is computed, and nobody reads numbers to
  pass it.
- *It fails closed only on a broken instrument.* A noisy runner widens the
  interval, so it makes the gate fire less, not more.
- *Drift.* A release that failed or was refused on the live cell never becomes
  the pairing base. Head is therefore compared with a release that met the
  ceilings, and drift cannot accumulate across releases by construction. Until
  the first release passes, the base is the last release tag, and drift is
  not bounded; the absolute verdict that every release records shows it.

**Absolute verdict, workstation and live cell.** The ceilings and the stage
budgets are judged on a quiet workstation at delivery (6.9) and on the live
cell after each release (5.6); that verdict is the release evidence for the
contract's numbers. Each live-cell attempt records one of three states: passed,
failed or refused. A refused attempt holds no verdict, and the operator repeats
it on a quiet cell. Only a passed release becomes the pairing base.

The live cell checks contention per sample, net of the measured process. Each
sample records its request thread's run-queue delay from
`/proc/self/task/<tid>/schedstat`, which counts only the time that thread
waited for a CPU. A sample that waited more than the larger of 5 ms and 10% of
its elapsed time is contended: it leaves the percentiles and is counted, and
more than 10% contended samples refuse the series. The 5 ms floor keeps
ordinary scheduler jitter on a fast request from counting. The reference runs
keep only the load check before the series. On two pinned CPUs the process's
own threads, such as a spinning encoder pool, can delay the request thread.
That delay is a product cost (decision 11), so dropping those samples would
hide it.

The in-process reference transport pins itself to two CPUs and loads
the served encoder from the published artifact, cached by digest. Corpus chunk
and unit vectors are seeded unit vectors written through the product's own
index build and stamped with the served encoder's identity, because embedding
46,000 chunks at the 6.7 chunks per second the Cloud lane measured on two
threads takes about two hours. Dense-search cost depends on the matrix size and
the candidate counts, not on the vector values, and ranking quality is not this
gate's question.

**Overlaps.**

- `watch-recall-latency-from-the-ledger` holds `RECALL_P90_CEILING_MS = 1000`
  (`latency_watch.py:36`), which `doctor` reports. It is a p90 over every
  served recall in the ledger, with cold, reranked and transport time included.
  That population is wider than this contract's warm reference series, so the
  two do not conflict, and this change leaves it as it is.
- `tests/test_latency_gate.py` keeps its per-lane ceilings and ratio checks in
  the same `retrieval-latency` job. They are model-free blowup backstops at
  2,000 and 8,000 notes. The paired comparison adds steps to the job and
  repeats none of their numbers.
- `scripts/recall_latency_gate.py` and `tests/test_recall_latency_gate.py`
  still pin the 300, 600 and 400 ms ceilings and the 20 ms eligibility bound;
  task 6.1 moves them to this contract.

### 10. Slices follow measured gain, after the gate

The gate and the structural counters land first, so every slice reports a
before and an after on the same instrument. The CPU spin slice goes next. The
other slices then follow the measured saving on a default hybrid request, with
the unit lanes placed by their saving on `mixed` requests. Tasks section 6
lists them.

Slices 6.3 to 6.8 land after three changes that touch the same functions:

- #1649 (`lexstore`, `bm25`);
- #1645 (`find_policy`, `find_corpus`, `embedding_index`, `lexstore`,
  `commands`);
- `fix/admission-candidate-sizing` (`_eligibility_metadata_query`,
  `candidate_k`). Slice 6.3 rewrites `_eligibility_metadata_query` too, so it
  starts from that work's version of the query.

### 11. Process CPU is measured per stage, and the idle spin goes first

Process CPU over wall time was 1.89 at p50 in R3 page level, with the encoder
loaded, and 0.42 in the model-free M1 run. Something keeps about one core busy
for each request while the encoder is loaded. The likely cause is ONNX
Runtime's intra-op pool spinning between runs, because
`configure_onnx_session_options` never disables `allow_spinning`
(`runtime_resources.py:155-169`), or OpenBLAS spinning while idle. Neither is
verified.

Every span therefore records process CPU time (`cpu_ms`) beside its wall time.
The value is process-wide, so it includes another thread's spin during the
span, and stage values overlap when stages run at the same time. Outside query
encode and the matrix products of dense and unit search, the request path runs
on one thread. Another stage whose `cpu_ms` exceeds its wall time therefore
shows a second thread at work: a spinning pool, or a background rebuild such as
the recall resolver's (`find.py:5703`). Slice 6.10 attributes and removes the spin before
the other slices are measured.

The cell thread policy is in scope: intra- and inter-op thread counts,
spinning, and `session.disable_prepacking=1`, which cells that share weights
set, Cloud cells among them (`embedding_backend.py:543`). Spinning trades CPU
for wake-up latency, so the slice shows encode latency on the paired instrument
and keeps encoder output identical.

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
- **[Risk] The encode target is wrong for a slower CPU.** → It was measured under
  load, not on a quiescent cell. The gate reports encode against its target, and
  a miss leads to an encoder evaluation, not a wider ceiling.
- **[Risk] The filler corpus hides growth that a real vault has.** → In a real
  vault, matches grow with the corpus. The contract bounds that work per
  matched row, and the absolute verdict on the reference corpus and on the live
  cell measures it.
- **[Risk] The paired comparison carries a slow release forward.** → It judges
  head against the last release whose live-cell verdict passed, so a release
  that missed the ceilings never becomes the base. The absolute verdict at each
  release checks the ceiling itself.
- **[Risk] Tranches 1 to 5's MODIFIED blocks go stale while section 6 lands.** →
  Section 6 stays in this change, so the archive waits for it, and other changes
  can modify the same canonical requirements meanwhile. Task 5.8 refreshes the
  blocks against the canonical specs before the archive.

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
