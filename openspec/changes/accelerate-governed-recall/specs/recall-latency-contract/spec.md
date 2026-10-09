## Purpose

The recall latency contract states what a warm governed search may cost, gives
each stage of the request a budget inside that cost, forbids corpus walks and
corpus-proportional work on the read path, and defines how the numbers are
measured so a contended box cannot be mistaken for a regression.

## ADDED Requirements

### Requirement: Governed Recall Meets Fixed Latency Ceilings On A Quiescent Cell

On a quiescent two-CPU cell or reference corpus of at least 6,500 governed pages
with warm caches, every warm search request SHALL complete with p95 at or below
200 ms. This covers hybrid recall (semantic, BM25, keyword, graph and fusion),
keyword recall, hybrid recall with one supported structured filter, and the
`mixed` and `unit` result levels. Warm hybrid p50 has a 100 ms target that every
report shows beside the measured p50. The ceilings are the capability's
contract, not calibrated from any runner, and a gate MUST NOT loosen them.

#### Scenario: Warm hybrid search on the reference corpus

- **WHEN** thirty or more novel, varied hybrid requests run back to back through `ask_memory` against a warm corpus of at least 6,500 pages, in a process restricted to two CPUs, with the load average at or below 2.0
- **THEN** the p95 of the elapsed time of the `ask_memory` call is at or below 200 ms
- **AND** the report shows the p50 beside the 100 ms target
- **AND** no request in the series reports a corpus walk in any stage

#### Scenario: Every search shape holds the same ceiling

- **WHEN** the series repeats as keyword recall, as hybrid recall with a `projects` filter that the index can answer, and at `result_level="mixed"`
- **THEN** the p95 of each series is at or below 200 ms
- **AND** the eligibility stage of the filtered series reports an index outcome within its stage budget

#### Scenario: Requests outside the warm contract are reported apart

- **WHEN** a request asks for reranking, runs while an accelerator auto-reranks, runs on a cold process, or runs while an index build is in progress
- **THEN** the gate reports it in its own series with its own count
- **AND** never folds it into a warm series

#### Scenario: A warming outcome is counted, not hidden

- **WHEN** a request in a measured series returns the typed warming outcome
- **THEN** it is excluded from the percentiles and counted separately
- **AND** more than one warming outcome in a series fails the gate

#### Scenario: A contended measurement is refused, not reported

- **WHEN** the gate is started while the one-minute load average is above 2.0
- **THEN** it waits a bounded time for quiescence and otherwise exits without a result, naming the load it observed
- **AND** it never emits ceiling comparisons from samples taken under that load

### Requirement: Warm Search Stages Stay Within Their Budgets

Each stage of a warm search request SHALL have the fixed p95 budget below, and
the budgets SHALL sum below the 200 ms ceiling. A stage that a request does not
run is reported as not run and is excluded from that stage's percentiles. A gate
SHALL report the p50 and p95 of every stage beside its budget, and SHALL fail a
series in which a stage's p95 exceeds twice its budget. A budget changes only
through this requirement.

| Stage | p95 budget |
|---|---|
| Request setup: recall projection, freshness, pending visibility | 5 ms |
| Admission and structured-filter eligibility | 15 ms |
| Query encode | 40 ms |
| Dense search, chunk text included | 15 ms |
| BM25 lane | 10 ms |
| Keyword lane | 10 ms |
| Unit lanes (`mixed` and `unit` result levels) | 30 ms |
| Parent hints | 2 ms |
| Graph lane | 15 ms |
| Temporal lane (temporal requests) | 5 ms |
| Fusion and multipliers, lexical guard included | 10 ms |
| Hydration: hit construction, excerpts, release gate, serialization, response blocks | 20 ms |
| CLIP lane (where enabled) | 10 ms |

#### Scenario: A stage over its budget is named

- **WHEN** a series meets the 200 ms ceiling but the keyword stage's p95 is more than twice its 10 ms budget
- **THEN** the gate fails and names the keyword stage, its p95 and its budget

#### Scenario: A stage a request did not run is not a zero

- **WHEN** a page-level request runs no unit lanes
- **THEN** the unit-lane stage is reported as not run for that request
- **AND** it is excluded from the unit-lane percentiles instead of counted as 0 ms

### Requirement: Warm Search Work Does Not Grow With The Corpus

For a warm search request, Markdown page reads, catalogue connections, catalogue
readiness proofs and SQLite virtual-machine steps SHALL NOT grow with corpus
size. Markdown page reads SHALL stay at or below twice the requested limit at
every result level. Each derived store SHALL open at most one connection and run
at most one readiness proof per request. Ranking, temporal, graph-seed and
unit-lane stages SHALL read page and unit metadata from the maintained
catalogue and sidecars for the current generation, not from Markdown.

#### Scenario: Corpus growth cannot hide per-request work

- **WHEN** the same warm request series runs on generated corpora of 1,600 and 6,500 pages
- **THEN** page reads, connections and readiness proofs per request stay within their bounds at both sizes
- **AND** the SQLite virtual-machine steps per request at 6,500 pages stay within 1.5 times the 1,600-page count plus a fixed slack

#### Scenario: A per-candidate table scan fails the gate

- **WHEN** a catalogue query for the candidate set scans the page table once per candidate path
- **THEN** its virtual-machine steps grow with corpus size and the structural gate fails

#### Scenario: The structural gate runs on every pull request

- **WHEN** a pull request runs CI
- **THEN** the structural gate runs in the pull-request tier, model-free, with no wall-clock threshold

### Requirement: Search Latency Is Gated Before Release

An in-process gate SHALL check the ceilings and the stage budgets on a
deterministic generated corpus of at least 6,500 pages, in a process restricted
to two CPUs, with the served query encoder. The corpus SHALL have realistic
prose with a vocabulary of at least 5,000 terms, a median page of 2 to 3 KB,
5 to 25 wikilinks per page, typed frontmatter, and semantic units on at least
half of its pages. The gate SHALL run in the scheduled and dispatched full CI,
and the release evidence check SHALL count it. After each release the same
series SHALL run against the live cell through its served transport.

#### Scenario: A constant-factor regression blocks the release

- **WHEN** a merged change raises warm hybrid p95 on the generated corpus above 200 ms
- **THEN** the next scheduled or dispatched full CI run fails the latency job and names the stages over budget
- **AND** the release evidence check stays red until a full run passes

#### Scenario: An encoder that cannot load is not a fast encoder

- **WHEN** the served query encoder cannot be loaded in the gate's environment
- **THEN** the encode and dense stages are reported as unknown, never as 0 ms
- **AND** the gate fails the series instead of passing it without those stages

### Requirement: The Read Path Never Walks The Corpus

No stage of a governed recall SHALL enumerate, read or parse every page of the
vault or of the knowledge-base scope on the reader thread. Eligibility,
widening, hydration and hit construction SHALL consume maintained indexes and
exact receipts, and a stage that cannot be answered from an index SHALL return
the typed warming outcome. Timing diagnostics SHALL expose, per stage, whether
the stage was answered from an index, from a cache, or declined, so a walk that
reappears is visible without a benchmark.

#### Scenario: Walk sentinel stays silent on the reference corpus

- **WHEN** a hybrid recall with a structured filter runs with timing diagnostics enabled
- **THEN** every stage reports `index`, `cache` or `declined` as its source
- **AND** the process performed no directory enumeration of the knowledge-base scope during the request

#### Scenario: An unanswerable filter declines instead of walking

- **WHEN** a recall names a supported filter field whose index is not live for the current generation
- **THEN** the recall returns the retryable warming outcome for that stage
- **AND** the reader thread does not read page frontmatter to evaluate the filter

### Requirement: Timing Attribution Is Complete

For a real recall with timing diagnostics enabled, the sum of stage durations
plus `unattributed_ms` SHALL NOT exceed `total_ms`, and `unattributed_ms` SHALL
NOT exceed fifteen percent of `total_ms`. Inside a parent stage, time that no
child stage covers SHALL also stay within fifteen percent of the parent. Every
stage that reports a duration SHALL be an interval registered with the timing
merge, never a manual difference written into the table.

#### Scenario: A real recall satisfies the attribution bound

- **WHEN** an opt-in timed hybrid recall runs through the public leaf, not a hand-built timing object
- **THEN** the sum of the root-level stages plus `unattributed_ms` does not exceed `total_ms`,
  nested stages being reported under their parent rather than counted again at the root
- **AND** `unattributed_ms <= 0.15 * total_ms` holds

#### Scenario: A costly step cannot hide inside a parent stage

- **WHEN** a sub-step inside `semantic.search` takes 85 ms and registers no interval of its own
- **THEN** the completeness check fails and names `semantic.search` with its uncovered time

#### Scenario: A manual timing write fails the completeness check

- **WHEN** a stage writes its duration into the table without registering an interval
- **THEN** the completeness check fails by reporting that stage as double-counted

### Requirement: Search Timing Covers The Whole Request

The timing diagnostics of a product search request SHALL cover the whole
request, including work that runs after retrieval, so that the elapsed time a
caller measures in process exceeds `total_ms` by at most 10 ms at p95. A stage
that ran without a reported duration, and a request without diagnostics, SHALL
be reported as unknown. A gate SHALL count and show unknown samples and fail
the series that holds one, and SHALL NOT record an unknown duration as 0 ms.

#### Scenario: Work after retrieval is inside the total

- **WHEN** `ask_memory` attaches a block to the response after retrieval finishes
- **THEN** that work is a registered stage inside `total_ms`

#### Scenario: Missing timings are unknown, not zero

- **WHEN** a measured request returns no timing diagnostics, or a stage that ran reports no duration
- **THEN** the gate reports that sample or stage as unknown and fails the series
- **AND** no percentile includes a 0 ms value for it
