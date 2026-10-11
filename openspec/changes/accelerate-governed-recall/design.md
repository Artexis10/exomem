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
- Warm hybrid search p95 at or below 200 ms on a two-CPU cell of 6,500 pages,
  with a 100 ms p95 target and a 50 ms p50 target that gate nothing, a budget
  for every stage along the request's critical path as the plan toward the
  target, a pull-request gate that sees per-candidate work grow before merge, a
  full-CI comparison of head with the most recent release tag, and an absolute
  verdict recorded for each release that gates nothing.

**Non-Goals:**
- Approximate retrieval, dropped features, ANN, or any quality-for-speed trade.
- More CPU per cell, or a cheaper encoder, to buy search latency.
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

### 7. Warm hybrid search costs at most 200 ms at p95 on a two-CPU cell, with a 100 ms target

On 2026-10-09 the owner set the target for every search mode: subsecond and
well under, 100 to 200 ms acceptable. The owner then asked why search cannot
run under 100 ms. On 2026-10-10 the owner weighed the whole system: a user
does not notice 100 against 200 ms, and CPU per cell and retrieval quality are
worth more than that difference. So every warm hybrid series holds p95 at or
below 200 ms, and that ceiling gates the verdict. 100 ms at p95 and 50 ms at
p50 are targets: the report shows them, records each as met or missed, and
they gate nothing. This design estimates that the served encoder cannot meet
the 100 ms target on two CPUs: the encode floor, below, puts a short query at
about 110 to 115 ms p95, well under the ceiling. The p50 target leaves little
room, because the query encode alone takes about 30 ms at p50 (encode
evidence, below), which leaves about 20 ms at the median for every other
stage. The reference is 6,500 pages, about the size of the owner's vault, on
two CPUs, the allocation of a Cloud cell. The personal service has more CPUs,
so meeting the contract on two leaves it headroom there. Only the
reference-corpus runs are pinned to two CPUs; the live personal service is
measured as it is served. The measured quantity is the elapsed time of the
`ask_memory` call, which is what a client waits for before transport. The
live-cell gate measures the same call at the REST facade.

The measured principal is the vault owner, identified through the product's
owner-authority path. The reproduction did not do that: it patched
`raw_protection.has_unrestricted_access` to return true, so its owner-path
numbers are patched numbers (`baseline.md`). An admitted non-owner principal
runs the same series, reported apart. It gates once admission no longer sizes
the candidate pool; today that path runs Python BM25 over the admitted pages. The model-free A1 run, which took the in-process
principal's RAW admission predicate, measured that path at 6.8 s p50.

Keyword recall runs no query encode, so the contract holds it to half of each
hybrid p95 figure: a ceiling of p95 at or below 100 ms, which gates, and a target
of p95 at or below 50 ms, which does not. Its budgeted path is 23 ms, or 29 ms
with a structured filter (the arithmetic below). The rest of the 50 ms target
covers keyword work that no budget row bounds: the trigram doclist
intersection, which the structural gate cannot see (decision 9), and the
reference mix's query whose tokens are all under three characters, which scans
every KB row by design. The ceiling replaces keyword's old p50 ceiling of
120 ms. A series whose p95 is at most 100 ms has a p50 under 120 ms, so the old
ceiling no longer bounds anything.

A hybrid request served without the encoder, where embeddings are disabled,
gets no lower ceiling or target. It skips only the encode branch and still
runs every stage after the lanes, so its budgeted path is 47 to 78 ms. That is
not far enough under the 100 ms target to justify a separate one, and no
reference series runs without the served encoder. It holds the 200 ms ceiling
and reports against the 100 ms target. The empty-query browse stays outside
the ceilings and the targets, as the shipped gate already treats it.

`baseline.md` records the reproduction. Its third run (R3: 76 samples, load
average 16-17 on a shared 20-CPU laptop, process pinned to two CPUs) is the
reference profile below. R3 ran with CLIP off (`EXOMEM_DISABLE_CLIP=1`), the
reranker hard-off and the patched owner path. Run 1 and run 2 measured most
stages at one to four times these values. Every number was taken under load, so
a quiescent cell is expected to be faster; the gate, not this table, decides
the verdict.

The stage budgets serve the 100 ms target: they are the plan toward it, not a
second ceiling. A stage whose p95 exceeds twice its budget fails its row, as
before. A failed row diagnoses: the report names the stage, its p95 and its
budget, so it shows where the target is lost. It fails neither the series nor
the ceiling verdict, because the target it serves gates nothing. Only the
ceilings, unknown samples and warming outcomes fail a series.

| Stage | p50 / p95 ms | Cause | Budget | Runs |
|---|---|---|---|---|
| Request setup | 9 / 28 | projection and pending-visibility checks | 4 | before the lanes |
| Admission and eligibility | not run (patched owner) | non-owner path: 214 / 440 ms model-free, sized by the other lane | 6 | before the lanes, with a filter or admission |
| Query encode | 33 / 116 | bge-m3 int8 on two contended CPUs | 40, a target under the 250 ms bound of `multilingual-recall` | encode branch |
| Dense search | 33 / 52 | chunk text for 3 x `candidate_k` rows (`embedding_index.py:1822`) | 15 | encode branch, after the encode |
| BM25 | 29 / 56 | FTS5 `bm25()`; the rest is connection setup and readiness | 8, a target until 6.1 grounds it | lexical branch |
| Keyword | 39 / 91 (up to 177 / 342) | trigram query plus connection setup and readiness; in keyword recall, page reads and hit construction too | 8, a target until 6.1 grounds it | lexical branch |
| CLIP | not run (`EXOMEM_DISABLE_CLIP=1`) | 37 ms warm on the live cell (`baseline.md`, 2026-09-03) | 10, where CLIP is enabled | lexical branch |
| Unit lanes (`mixed`) | 647 / 2,143 | per-candidate parent re-parse and policy load; the unspanned query encode runs inside it | 12 | lexical branch; vector search after the encode |
| Parent hints | 85 / 132 | the plan scans `json_each` once per KB page (`lexstore.py:8382`) | 2 | after the lanes |
| Graph | 26 / 135 | seeds 6 / 30 through Markdown reads; expand 14 / 31; resolver 5 / 16 | 9 | after the lanes |
| Temporal | 44 / 124 | `updated` read from Markdown per candidate | 3 | after the lanes, when it runs |
| Fusion and multipliers | 24 / 66 | type and status read from Markdown per candidate | 5 | after the lanes |
| Hydration | 30 / 79, the due-state block included | hit construction, excerpts, serialization | 8 | after the lanes |
| Due state | 21 / 58 | role state read once per hit, outside `total_ms` | 3 | after the lanes |

**Critical path.** ONNX Runtime releases the GIL while it encodes, and SQLite
while it steps, so the query encode can run beside the BM25, keyword and unit
lanes (decision 12, slice 6.13). The budgets therefore compose along the
request's critical path, not as a sum. Stages that run one after another add,
and of two branches that run at the same time only the longer counts.

```
Before the lanes:  setup 4 + eligibility 6                          = 10
Encode branch:     encode 40 + dense search 15                      = 55
Lexical branch:    max(BM25 8 + keyword 8 + CLIP 10, encode 40)
                   + unit lanes 12                                  = 52
After the lanes:   parent hints 2 + graph 9 + temporal 3 + fusion 5
                   + hydration 8 + due state 3                      = 30
Critical path:     10 + max(55, 52) + 30                            = 95
```

The lexical-branch line counts all of the unit lanes as if they started after
the encode. Their vector search needs the query vector, so it waits for the
encode; their lexical lane runs earlier, so the line overstates the branch.
The encode branch is still the longer one, so whenever the encoder runs, the
encode and dense search are on the critical path. A request that runs every
stage, with a filter, the temporal lane, the unit lanes and CLIP, has a 95 ms
path, 5 ms under the target, if the encode meets its 40 ms budget. The encode
floor, below, estimates that it does not on two CPUs. The arithmetic adds p95
budgets, as the old sum did; the gate's measured percentiles, not this arithmetic, decide the verdict.

| Series | Path | ms |
|---|---|---|
| Hybrid, owner, page level | 4 + 55 + 27 | 86 |
| Hybrid with the temporal lane, and the temporal series | 4 + 55 + 30 | 89 |
| Filtered hybrid | 10 + 55 + 27, or 10 + 55 + 30 with the temporal lane | 92 or 95 |
| `mixed` and `unit` levels | as hybrid, because the lexical branch's 52 ms ends before the encode branch's 55 | 86 to 95 |
| Keyword | setup 4 + keyword 8 + hydration 8 + due state 3, plus eligibility 6 with a filter | 23 or 29 |
| Hybrid without the encoder | 4 + BM25 8 + keyword 8 + 27, up to 10 + 38 + 30 with every lane | 47 to 78 |

Run one after another, the same budgets sum to 133 ms, under the 200 ms
ceiling, and a default hybrid request to 102 ms (4 + 40 + 15 + 8 + 8 + 2 + 9 +
5 + 8 + 3). The ceiling holds on the serial path; the 100 ms target is
reachable only with the encode beside the lexical lanes.

**The BM25 and keyword budgets are targets.** Their 8 ms rests on a replay at
6,500 pages on one retained connection: BM25 3.7 / 7.6 ms and keyword
2.6 / 6.5 ms p50 / p95 over 50 short queries
(`verification/subsecond-2026-10-09/sqlcheck.txt`). Three things in that
replay differ from the request that the budgets bound:

- *The connection.* Decision 8's catalogue session opens a fresh connection
  for each request. The same replay measured a new connection per query at
  6.3 / 10.8 ms for BM25 and 3.3 / 8.3 ms for keyword.
- *The SQLite.* The replay ran on SQLite 3.53.1, not on a reference SQLite
  (decision 8).
- *The query length.* The replay used short queries. BM25 matches an OR of the
  query terms, so a 15-40 word query matches most of the corpus, and FTS5
  scores and sorts every matched row.

Both budgets are therefore targets until task 6.1 grounds them with a replay on
a new connection per request, on SQLite 3.45.1 and 3.46.1, over the whole query
mix, the 15-40 word band included. Until then a gate reports them against
8 ms and does not apply the twice-budget rule to them.

In keyword recall the `keyword` span wraps `_find_keyword` (`find.py:1838`),
which builds the hits itself: it reads each matched page, makes its excerpt and
builds the hit, for every page that `_keyword_match_paths` returns, before the
limit cuts the list. That call passes no `k` (`find.py:4115`), so it returns
every page that matches, and slice 6.14 limits it before any page read
(decision 8). `filter_hits` runs only inside `_find_semantic`, so in keyword
recall no interval is counted twice: the hydration row counts `release_gate`
and `serialize` only, and the keyword row counts the page reads and hit
construction. The 23 ms keyword path counts hit construction once, in the
keyword row. In keyword recall the 8 ms keyword row includes page reads, which
the replay did not measure, so 6.1 grounds it in that mode too.

Dense search keeps its 15 ms: the exact scan reads the whole float32 chunk
matrix, about 184 MB at 45,000 chunks of 1,024 dimensions, so memory bandwidth
sets its floor, and ANN is a non-goal.

Every stage is over its budget today, at p95 by about three times (query
encode, 116 ms against 40) to about 180 times (unit lanes, 2,143 ms against
12). Slices 6.3 to 6.8 remove named per-request work from most of them. Three
rows have no removable work named yet, and for the 100 ms target to hold they
need constant-factor speed:

- Request setup, 9.4 ms at p50 and 28.1 at p95 against a 4 ms budget, must
  fall by more than half at p50 and about sevenfold at p95 (slice 6.11).
- Graph expand and resolver, 13.9 and 5.1 ms at p50 and 31.4 and 16.3 at p95,
  must fit the lane's 9 ms once 6.5 moves the seeds. That means falling to
  under half at p50 and to about a fifth at p95 (slice 6.12).
- Query encode must fall to about a third at p95, from 116 ms to 40. The
  encode floor below estimates that it cannot on two CPUs.

**The encode floor.** The encode decides whether the 100 ms target is
reachable, and code in this change cannot buy it. The floor is information
about the target: it sits under the 200 ms ceiling, so it decides no verdict.
These are the measured encode figures:

- The archived `make-recall-multilingual` D7 table: short-query encode at
  30 / 43 ms p50 / p95, and bge-m3 int8 whole queries at 42 / 57 ms at load
  22.2. The record does not say whether those runs were pinned to two CPUs.
- The Cloud lane: about 45 ms at p50 on one intra-op thread.
- R3: 33 / 116 ms p50 / p95, pinned to two CPUs, under load 16-17.
- Encode during a one-text-at-a-time build: 75 / 178 ms, which is why a
  request during an index build stays outside the warm series.

*Encode threads under concurrency.* On slice 6.13's concurrent path the served
encoder runs one intra-op thread on a two-CPU cell, and the lexical branch
runs on the other CPU. The budgets fit two CPUs at one encode thread, if each stage uses
one CPU-ms for each millisecond of its budget:

```
Whole request:   133 CPU-ms of budgets / 2 CPUs  = 66.5 ms, within the 95 ms path
While the branches overlap:
                 encode 40 + dense search 15 + BM25 8 + keyword 8 + CLIP 10
                 + unit lanes 12 = 93 CPU-ms / 2 CPUs
                                                 = 46.5 ms, within the 55 ms branch
```

With two intra-op threads the encode would hold both CPUs while it runs, and
the lexical branch would wait for a core.

*The estimated floor.* On one thread the encode's measured p50, about 45 ms, is
already above its 40 ms p95 budget. On two CPUs with bge-m3 int8, this design
estimates the floor at about 110 to 115 ms p95 for short queries, with every
non-encode stage on budget. That is the 55 ms of non-encode budgets on the path
of a request that runs every stage, plus a one-thread encode p95 of about 55 to
60 ms. The whole query mix is likely higher, because longer queries encode
more slowly (D7: whole queries 42 / 57 ms against short queries 30 / 43). This
is an estimate, not a measurement: no run has measured a one-thread encode p95
on the pinned profile.

*What 100 ms needs.* The target is reachable at p95 with four or more CPUs,
where the encode keeps two or more intra-op threads beside the lexical branch,
or with a cheaper encoder whose one-thread p95 fits the 40 ms budget. Neither
is in this change. The owner chose on 2026-10-10 not to trade CPU or
retrieval quality for the 100 ms target.

The rule for the encode is therefore:

1. Task 6.1 measures the encode for each query-length band (1-3, 4-9 and
   15-40 words) on one and on two intra-op threads, on the pinned profile,
   before any budget is trusted.
2. The quiet reference run (6.9) re-measures the encode on the pinned
   profile. If the encode p95 alone exceeds its 40 ms budget there, the run
   records that p95 as the measured floor, beside the budget.
3. Neither the ceiling, the target nor the encode budget widens to fit the
   floor. The lever in this change is the encoder runtime: the thread policy
   of slice 6.10.
4. The measured floor keeps the target out of reach when the budgets' critical
   path, with the measured encode p95 in place of the 40 ms budget, exceeds
   100 ms. The acceptance summary then records the target as missed, with that
   floor. A missed target never holds this change (decision 9, acceptance).

`multilingual-recall` already bounds encode p95 at 250 ms, so the 40 ms row is
a diagnostic target that cites that bound, not a second bound on one
quantity, and the twice-budget rule does not apply to it. The 200 ms ceiling
bounds it in practice. The CPU spin slice (decision 11) comes first, because a
request that keeps a second core busy slows every stage on a two-CPU cell, the
encode most.

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
  uses the `IN` form with `in_vault = 1`, so it plans as
  `SEARCH pages USING INDEX pages_vault (in_vault=?)` and visits every vault
  page: 23,160 steps at 1,600 pages and 77,060 at 6,500, from a replay that
  `verification/` does not keep. The proven form for parent hints is
  `FROM json_each(?) r CROSS JOIN pages p ON p.path = r.value`. `CROSS JOIN`
  fixes the join order, so `json_each` drives a primary-key lookup; it held
  flat at 1,016 steps at both sizes. A replay of that plan returned the same
  rows in 0.2 ms instead of 135 ms for 300 paths, and 1.0 ms instead of 488 ms
  for 1,000. The eligibility query must also return each requested path's
  parent, which its `wanted` CTE adds, so its outer select drives from that
  CTE: `FROM wanted w CROSS JOIN pages p ON p.path = w.path`. Driving it from
  `json_each(?)` would drop the parents. Slice 6.3 moves both queries and
  confirms each plan with `EXPLAIN QUERY PLAN`. It lands after the admission
  candidate-sizing work, which edits `_eligibility_metadata_query`
  (decision 10).
- **The SQLite that deployments link.** CI's `retrieval-latency` job runs
  system Python 3.12.3 with SQLite 3.45.1, and Cloud cells link 3.46.1
  (`python:3.12-slim` on trixie). Without `ANALYZE`, both plan an unordered
  `fts JOIN pages` from `pages_kb`, which is linear in the KB, while the 3.53
  that a local uv venv bundles drives the join from `MATCH`. The replays above
  and `sqlcheck.txt` ran on 3.53.1. #1649 pins every FTS5 `MATCH` join with
  `CROSS JOIN` (`lexstore._FTS_PAGES`). The keyword lane's `_substring_query`
  (`lexstore.py:8441`) still uses `tri JOIN pages`, so slice 6.3 pins its
  `MATCH` branch with `CROSS JOIN` the same way. Its branch without a `MATCH`,
  where every token is under three characters, scans `pages_kb` by design and
  stays. A *reference SQLite* is therefore the one that CI's system Python or
  a Cloud cell links, 3.45.1 or 3.46.1 today. The reference runs link one, and
  the structural gate and every plan check also run on 3.45.1, the older
  (decision 9). No verdict then rests on a plan that neither CI nor a Cloud
  cell runs.
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
  The admitted branch of `search_semantic_units_result`
  (`lexstore.py:7980-7989`) loads every admitted unit's stemmed text and
  rebuilds the term frequencies on each call, with no cache. That is the
  pattern #1649 removes for pages, so a restricted unit search pays for every
  admitted unit on every query. It is old debt from the #1649 review, and
  slice 6.6 computes those statistics once per catalogue generation and
  admitted set.
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
- **Keyword recall's page reads.** `_find_keyword` (`find.py:4070`) calls
  `_keyword_match_paths` with no `k` (`find.py:4115`). It then reads, parses
  and builds a hit for every matching page, and only after that sorts and cuts
  the list to `limit` (`find.py:4244-4245`). Its Markdown reads therefore grow
  with the matches, not with the hits it returns, which breaks the bound that
  Markdown reads track the candidate count.

  Today's order and membership depend only on data that the catalogue holds:
  - The hits sort by the emitted page's `updated or "0000-00-00"` and then its
    path, both descending. A frame child emits under its parent video, whose
    `updated` is its own. The catalogue holds each row's `updated` and path,
    its `emitted_parent_path`, and the parent's own row.
  - The structured filters apply to the emitted page, and the page-metadata
    index answers them (decision 1). The eligible and admitted sets are path
    sets.
  - Page bytes decide only the content of a hit: the excerpt comes from the
    first matched row of its group in today's walk order, and the frame
    annotation from the first matched frame child.

  Slice 6.14 therefore orders and limits in SQL before any page read. One
  query groups the matched rows by emitted identity, orders the groups by the
  hit list's key, and returns the first `limit` groups, each with its first
  matched row and first matched frame child. Only those pages are read.
  Pending rows lead, as today, as the fixed overfetch. Two checks still read
  the file per row: `is_recall_candidate` and the page parse. A row that fails
  either drops, as today, and the slice reads the next group in order, so a
  drop costs one read and never shortens or reorders the list.

  Results stay identical, so no result contract changes. If an identity case
  shows that today's order depends on data that only the page holds, slice
  6.14 records the difference as a contract change in this change's
  `recall-latency-contract` delta, with a scenario, before it lands.

The temporal lane decides "temporal" with a word regex, `TEMPORAL_MARKERS`
(`find_policy.py:41`, read by `is_temporal_query` at `:427`). Three more
mechanisms decide intent from English words: `RELATIONSHIP_MARKERS`
(`find_policy.py:48`), `EXACT_LEADING` (`:54`) and `classify_intent` (`:434`),
which combines them. All four are old C6 debt. This change moves where
`updated` is read from, not how a lane or an intent is chosen, and it removes
none of them.

### 9. Three instruments: counts per pull request, a paired comparison in full CI, an absolute record per release

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
- *SQLite versions.* The gate runs on SQLite 3.45.1, the older reference
  SQLite, from CI's system Python 3.12.3, as well as on the
  pull-request tier's own SQLite, and its report names each version. A plan
  that is linear on 3.45 and bounded on 3.53 then fails where it would ship
  (decision 8).
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
    `IN` form plans as a search of `pages_vault (in_vault=?)` that visits every
    vault page (decision 8).

  Also expected: the ranking stages' Markdown reads, 126 per request in R3
  against 15 hydrated hits, and keyword recall's Markdown reads for a query
  that matches more pages than its limit, because it reads every matching
  page (decision 8). The BM25 and keyword step ratios pass, because their
  matched rows are the same at both sizes.

**Paired comparison, full CI.** The `retrieval-latency` job runs on shared
`ubuntu-latest` runners and feeds `gate` and release evidence, so it applies no
absolute threshold.

- *Arms.* Head and the pairing base, installed side by side in one job on one
  runner, against one reference corpus generated once, with the same cases. The
  pairing base is the most recent release tag. It is code only and moves
  automatically when Release Please tags a release. No vault data, live-cell
  result or box load moves it.
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
  the interval's level instead of multiplying it by fourteen stages. A stage
  with fewer than 20 paired cases reports "insufficient samples". A stage that
  the pairing base does not span yet, such as `parent_hints` before 6.3,
  reports "not comparable".
- *Load.* Recorded, never refused: both arms share the runner's speed.
- *Unknown samples.* A sample without timings, or an encoder that cannot load,
  fails the series, because the instrument is broken.

Control justification:

- *It prevents* a merged change that slows warm search by 10% or more against
  the last release from reaching the next release. The structural counters
  cannot see constant-factor regressions: a thread-policy change, a slower plan
  with the same step count, encoder settings or added Python overhead.
- *A wrong firing* comes only from runner noise, because both arms are code
  and run on one runner, one corpus and one case list. It turns the full run
  red, and the release evidence check stays red with it. The agent or operator
  who drives the release pays: one rerun of the failed `retrieval-latency` job
  (`gh run rerun <run-id> --failed`) and a release delay of that job's
  duration. A green rerun clears it. No user and no pull request is gated, and
  vault data or the shared box's load cannot fire it.
- *Wrong firings are rare by construction.* The verdict needs the interval's
  lower bound above the margin, so at a true slowdown of exactly 10% it fires
  falsely in at most 2.5% of runs, and far less near no change. Alternating
  the arm order keeps runner drift out of the ratios. A second red on the same
  SHA is a regression.
- *No human decides.* The verdict is computed, and nobody reads numbers to
  pass it.
- *It fails closed only on a broken instrument.* A noisy runner widens the
  interval, so it makes the gate fire less, not more.
- *Drift.* Each release can be up to the 10% margin slower than the release
  before it without firing the job, and more when a noisy runner widens the
  interval. CI does not bound cumulative drift: five releases that are each 9%
  slower compound to about 54%. The absolute record on each release shows that
  drift against the fixed ceilings, and a failed record opens follow-up work.
- *A deliberate slowdown* is a correct firing. A change that accepts more than
  10% on purpose, such as a correctness fix, ships through a committed accept
  record (`benchmarks/recall-latency/accept.json`, beside the gate's other
  inputs, so archiving this change does not move it). The record names
  the release tag it accepts against, the series, a maximum paired ratio per
  series, the full-CI run that measured the slowdown, the reason and the pull
  request. Each maximum ratio is that run's upper confidence bound for the
  series, rounded up to the next 0.05, so the reviewer can see that the
  allowance is not padded. A pull request adds it, so the same
  independent review that checks every change checks the waiver; no person
  approves it separately. The job reads it on scheduled and dispatched runs
  alike, so nightly runs stay green for the accepted series. It bounds the
  waiver: an uncovered series, or a covered series beyond its maximum ratio,
  still fails. It expires by itself when a newer tag exists: the job then
  ignores it, reports it as expired, and the slowdown it covered fails again.
  An expired record has no effect, so deleting it is housekeeping, not a
  gate. The agent or operator who drives the release deletes it in a pull
  request, as a step of `docs/release.md`, unless the next accept record's
  pull request replaces it first. There is no run
  input that waives the comparison, because a self-granted waiver at dispatch
  time would be the only unreviewed decision in the release path.

**Absolute record, workstation and live cell.** The ceilings are judged on a
quiet workstation and on the live cell after each release, and the same runs
report the targets and the stage rows. Neither verdict gates CI, a merge or a
release. The live cell's
result depends on the owner's vault data and on the shared box's load, and no
code change controls either.

- *States.* Each verdict is recorded as one of four visible states: passed,
  failed, refused or not measured. A refused verdict holds no ceiling
  comparison, and the operator can repeat it on a quiet cell.
- *SQLite.* The workstation run links a reference SQLite, 3.45.1 or 3.46.1
  today, not a local uv venv's 3.53, and it records the version. A run on
  another version gives no ceiling verdict, as a corpus below the profile
  gives none. This prevents a passed verdict on plans that neither CI nor a
  Cloud cell runs (decision 8). When CI or the Cloud image moves to a newer
  SQLite, the version list beside the accept record changes in one line, and
  a run that fired wrongly is repeated.
- *Acceptance.* The workstation verdict is this change's acceptance check
  (6.9). The change completes when a quiet-workstation run on the reference
  corpus records its verdict against the ceilings, 200 ms p95 for hybrid
  series and 100 ms for keyword recall, as passed, with its summary stored
  under `verification/`. A failed ceiling keeps the change open. A refused or
  not-measured state completes nothing. The summary also records the target
  outcome for each series, met or missed, with the measured encode floor and
  any failed stage rows. A missed target never blocks completion.
- *Who records the live-cell state, and where.* The agent or operator who
  rolls a release onto the live cell runs the series. It is a step of the
  release runbook, `docs/release.md`, under "Managed Linux service: what to
  check after the handoff", after `/health/ready` reports `ready` (5.6). They
  attach the gate's content-free summary to that release's GitHub Release with
  `gh release upload "$TAG" <summary>`. That is the path the release workflow
  already uses for the wheel, the sdist and the hosted runtime evidence
  (`.github/workflows/release-please.yml`). Each attempt is one asset, named
  `recall-latency-live-cell-<UTC time>.json`, so a refused attempt stays
  visible beside its repeat. The latest attempt gives the release's state. A
  release with no such asset shows "not measured".
- *Follow-up.* A failed or refused live-cell state opens follow-up work: a
  GitHub issue that names the release, the series and the stages over budget.
  It blocks nothing.

The live cell checks contention per sample. Each sample records its request
thread's run-queue delay from `/proc/self/task/<tid>/schedstat`: the time that
thread waited for a CPU. That wait includes waits behind the measured
process's own threads, so it is not net of the measured process. A sample that
waited more than the larger of 5 ms and 10% of its elapsed time is contended:
it leaves the percentiles and is counted, and more than 10% contended samples
refuse the series. The 5 ms floor keeps ordinary scheduler jitter on a fast
request from counting. Dropping contended samples biases p95 downward, because
a slow sample is the likelier one to have waited. The 10% refusal cap bounds
that bias: at worst, the p95 of the kept samples is about the p85 of the whole
series. The report therefore states the dropped count and the p95 with
contended samples included, beside the p95 without them. The reference runs
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
the unit lanes placed by their saving on `mixed` requests. The concurrent
encode (6.13) lands last, before the close. It needs one catalogue session per
request (6.4), the unit lanes' new shape (6.6) and the thread policy (6.10).
Its saving, the shorter branch, is known only once the lanes are fast. Tasks
section 6 lists them.

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

Every span therefore records CPU time (`cpu_ms`) beside its wall time. While
no stage on another thread overlaps a span, its `cpu_ms` is the process CPU
over it, so it includes another thread's spin during the span; decision 12
says how CPU is reported where stages on different threads overlap. Until
slice 6.13, outside query encode and the
matrix products of dense and unit search, the request path runs on one thread.
Another stage whose `cpu_ms` exceeds its wall time therefore
shows a second thread at work: a spinning pool, or a background rebuild such as
the recall resolver's (`find.py:5703`). Slice 6.10 attributes and removes the spin before
the other slices are measured.

The cell thread policy is in scope: intra- and inter-op thread counts,
spinning, and `session.disable_prepacking=1`, which cells that share weights
set, Cloud cells among them (`embedding_backend.py:543`). Spinning trades CPU
for wake-up latency, so the slice shows encode latency on the paired instrument
and keeps encoder output identical.

### 12. The query encode runs beside the lexical lanes

The 100 ms target needs the encode off the serial path; the 200 ms ceiling
does not (decision 7). After
eligibility, the request runs two branches at the same time, and the stages
after the lanes start when both have ended:

- The *encode branch* runs the query encode, then dense search.
- The *lexical branch* runs BM25, keyword, CLIP where it is enabled, and the
  unit lanes. The unit lanes' vector search starts when the encode has
  produced the query vector. Until then `semantic_units` blocks, and that wait
  is spanned as `vector.wait`.

On the concurrent path the served encoder runs one intra-op thread on a
two-CPU cell, so the lexical branch keeps the other CPU (decision 7).

CLIP runs on the lexical branch because it encodes the query text with its
own model and does not need the bge-m3 vector. On the encode branch it would
add its 10 ms to the critical path. ONNX Runtime releases the GIL while it
encodes, SQLite while it steps, and NumPy during matrix products. Python work
on either branch still takes the GIL in turn, so slice 6.13 measures the
overlap instead of assuming it.

*Identical results.* Each lane computes its ranking from the query and the
eligible set alone, and fusion reads the rankings in a fixed lane order, so
the order in which the branches finish cannot change a result. Lane statuses,
the degraded and failed lists and the retrieval trace keep the serial order.
Each branch uses its own connections, and no connection crosses threads; the
lexical branch owns the request's catalogue session (decision 8). An identity
test compares fused order, hits, excerpts and lane statuses with the serial
path on the reference corpus.

*CPU contention.* On two CPUs, the encoder's intra-op pool and the lexical
thread compete for the same cores, and dense search and the unit lanes'
vector search compete for memory bandwidth. Slice 6.13 runs the reference
series both ways on the pinned profile, on the paired cases:

- *Serial*, with the encode on the thread count that 6.10 sets.
- *Concurrent*, with the encode on one intra-op thread.

Concurrency helps on two CPUs when both of these hold:

- the hybrid series is faster concurrent than serial: the upper bound of the
  95% interval of the paired ratios (decision 9) is below 1.0;
- the concurrent encode p95 stays within 10% of a serial encode on one
  intra-op thread, so contention between the branches stays bounded.

The slice keeps the concurrent path only when it helps. If concurrency does
not help on two CPUs, the slice records that finding with both arms' figures,
keeps the serial path, and completes with its finding. The 100 ms target is
then out of reach on two CPUs (decision 7), and 6.9 records it as missed. The
thread policy of slice 6.10 and this slice are decided on the same
instrument.

*Overlapping spans.* Every span keeps its own interval, on the thread that ran
it, and the timing table keeps one row per stage:

- *Wall time.* A row's wall time is the union of the intervals it counts, so
  overlap with the other branch neither shortens nor lengthens it. Wherever
  intervals are added up, at the root or inside a parent, overlapping
  intervals count once: the covered time is their union, and
  `unattributed_ms` is `total_ms` less the union of the root-level intervals.
- *Waiting on another branch.* The unit lanes' `vector.wait` interval is
  excluded from the unit-lane row, because time blocked on another branch's
  result is not the stage's own. It is not reported as unbudgeted either: the
  encode row already counts the time it waits on.
- *Critical path.* Each request records its critical path: the stages that
  ran one after another, plus the branch that ended last. Each row reports
  the share of the series' requests in which its stage lay on the critical
  path. A stage off the critical path still answers to its budget, because
  its branch becomes the critical one when it overruns the other.
- *CPU time.* The request's *overlap* is the time during which spans on two or
  more threads are open. A row's `cpu_ms` is the process CPU over the parts of
  its intervals outside the overlap, as decision 11 uses it. The process CPU
  over the overlap is reported once per request as `overlap_cpu_ms`, and no
  row counts it. The process CPU over the part of `total_ms` that no
  root-level interval covers is reported as `unattributed_cpu_ms`. No CPU is
  split between threads: the pools that the encoder and NumPy drive run
  native threads that no span opens, so an attribution per thread would be a
  guess.
- *CPU source.* The CPU fields read `/proc/self/task`, only while timing
  diagnostics are on. On a platform without it, every CPU field reads
  unknown, never 0, and the wall-time verdicts stand.

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
- **[Risk] The gate never sees a quiet box.** → It refuses rather than reports,
  and the refused state is recorded and blocks nothing. The operator can pause
  suites, and the structural guards and the paired job still run in CI. This
  change's acceptance waits for a passed workstation verdict (6.9).
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
- **[Risk] The encode budget is below what the pinned profile can do.** → It
  is likely: the one-thread encode's p50, about 45 ms, already exceeds the
  40 ms p95 budget, and the estimated floor is about 110 to 115 ms p95 for
  short queries (decision 7). Task 6.1 measures the encode per query-length
  band before any budget is trusted. The quiet reference run records a miss
  as the measured floor. The lever in this change is the thread policy of
  6.10, never a wider ceiling, target or budget. The owner chose on
  2026-10-10 not to trade CPU or retrieval quality for the 100 ms target, so a
  floor above it is recorded as a missed target and holds nothing (decision 9,
  acceptance).
- **[Risk] The concurrent branches slow each other on two CPUs.** → Slice 6.13
  keeps the concurrency only when it helps on the pinned profile (decision
  12). If it does not, the slice records that, keeps the serial path and
  completes. The budgets then sum to 133 ms, under the 200 ms ceiling; the
  100 ms target is out of reach on two CPUs, and the gate records it as missed
  rather than widening it.
- **[Risk] The filler corpus hides growth that a real vault has.** → In a real
  vault, matches grow with the corpus. The contract bounds that work per
  matched row, and the absolute verdict on the reference corpus and on the live
  cell measures it.
- **[Risk] The paired comparison carries a slow release forward.** → It does:
  each release can be up to the margin slower than the last, and CI does not
  bound the sum. The absolute record on each GitHub Release shows the drift
  against the fixed ceilings, and a failed record opens follow-up work.
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
3. Release; upgrade the live cell; run the gate; attach its summary to the
   GitHub Release and record before/after in the change; then archive.

Rollback is a release rollback; no data migration, since the metadata index is
derived and rebuilds from the vault.

## Open Questions

- Whether `speakers` and file-type filters need the metadata table or can stay
  on the media sidecar they read today; decided by the lane that inventories
  the filter registry, without changing the specs.
