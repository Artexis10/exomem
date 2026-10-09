## Purpose

The recall latency contract states what a warm governed search may cost, gives
each stage of the request a budget inside that cost, bounds the read path's work
per candidate and per matched row, forbids corpus walks, and defines how the
numbers are measured so a contended box cannot be mistaken for a regression.

## ADDED Requirements

### Requirement: Governed Recall Meets Fixed Latency Ceilings On A Quiescent Cell

On the reference corpus in a process restricted to two CPUs, and on the live
cell as it is served, quiescent and with warm caches, every warm search series
SHALL have p95 at or below 200 ms. The two-CPU restriction applies to the
reference-corpus runs only; the live personal service is not pinned. This covers
hybrid recall (semantic, BM25, keyword, graph and fusion), keyword recall,
hybrid recall with one supported structured filter, hybrid recall that runs the
temporal lane, and the `mixed` and `unit` result levels. Every report shows each
series' p50 beside a 100 ms target, and keyword recall keeps its p50 ceiling of
120 ms. The ceilings are the capability's contract, not calibrated from any
runner, and a gate MUST NOT loosen them. The empty-query browse is outside these
ceilings.

The measured principal SHALL be the vault owner, identified through the
product's owner-authority path with nothing patched. An admitted non-owner
principal SHALL run the same series, reported separately. That series becomes
gating once admission no longer sizes the candidate pool.

On the live cell, contention during a series SHALL be measured net of the
measured process. Each sample's timing diagnostics record the request thread's
run-queue delay: the change, over the request, in the second field of the
served process's `/proc/self/task/<tid>/schedstat`. A sample whose request
thread waited on a run queue for more than the larger of 5 ms and 10% of its
elapsed time is contended. The gate excludes contended samples from the
percentiles and counts them, and it refuses a series in which more than 10% of
the samples are contended. When the delay cannot be read, the gate reports
contention as unknown and refuses the series.

An index build is in progress while a full build or a repair of a derived store
for the request's scope runs: the lexical catalogue, the embedding sidecar, the
semantic-unit sidecar or the graph. Incremental indexing of the pages that one
governed write changed is not an index build.

#### Scenario: Warm hybrid search on the reference corpus

- **WHEN** thirty or more hybrid requests from the reference query mix run back to back through `ask_memory` as the vault owner, against the warm reference corpus, in a process restricted to two CPUs, after the quiescence check passed
- **THEN** the p95 of the elapsed time of the `ask_memory` call is at or below 200 ms
- **AND** the report shows the p50 beside the 100 ms target
- **AND** no request in the series reports a corpus walk in any stage

#### Scenario: Every search shape holds the same ceiling

- **WHEN** the series repeats as keyword recall, as hybrid recall with a `projects` filter that the index can answer, as the dedicated temporal series, and at `result_level="mixed"`
- **THEN** the p95 of each series is at or below 200 ms
- **AND** the p50 of the keyword series is at or below 120 ms
- **AND** the eligibility stage of the filtered series reports an index outcome within its stage budget

#### Scenario: The owner is real and the admitted principal is reported apart

- **WHEN** a gate measures a series
- **THEN** the owner's identity comes from the product's owner-authority path, and no authority check is patched
- **AND** the admitted non-owner series is reported with its own percentiles and count
- **AND** the admitted series fails the gate only once admission no longer sizes the candidate pool

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

#### Scenario: Quiescence is checked once, before the series

- **WHEN** the load average rises during a series that started quiescent
- **THEN** the gate does not refuse the series on the load average, because the measured process's own load counts in it
- **AND** it records the load beside every percentile
- **AND** on the live cell it judges contention per sample from the request thread's run-queue delay instead

#### Scenario: A contended live-cell sample is dropped and counted

- **WHEN** another process keeps the live cell's CPUs busy and a sample's request thread waits on a run queue for 30 ms of a 150 ms request
- **THEN** the gate excludes that sample from the percentiles and counts it as contended
- **AND** it refuses the series when more than 10% of its samples are contended, naming the count

### Requirement: Warm Search Stages Stay Within Their Budgets

Each stage of a warm search request SHALL have the fixed p95 budget below, and
the budgets SHALL sum below the 200 ms ceiling. Each row names the timing span
keys it counts. A row counts its keys and their children, excluding any
descendant interval that another row names. The query encode SHALL be spanned
as `vector.embed` wherever it first runs, inside `semantic_units` included, so
that no other row absorbs it. A span key that no row counts, by name or as a
descendant, SHALL be reported as "unbudgeted" with its own p50 and p95, net of
any descendant interval that a row names, so time cannot hide in an unlisted
key. Examples are `outside_kb`, `matched_units`, `referents` and the part of
`semantic.search` that no child covers. A stage that a request does not run is
reported as not run and is excluded from that stage's percentiles. A gate SHALL
report the p50 and p95 of each stage's wall time and of its process CPU time
(`cpu_ms`) beside its budget.

A stage verdict SHALL need at least 20 samples that ran the stage. With fewer,
the gate reports the stage as "insufficient samples", never as a pass or a
fail. A gate SHALL fail a series in which a stage with enough samples has a p95
above twice its budget. Query encode is a diagnostic target: its bound is the
encode p95 of 250 ms in `multilingual-recall`, and the twice-budget rule does
not apply to it. A budget changes only through this requirement.

| Stage | Span keys | p95 budget |
|---|---|---|
| Request setup: recall projection, freshness, pending visibility | `recall_projection`, `pending_visibility`, `freshness`, `cache_lookup` | 5 ms |
| Admission and structured-filter eligibility | `filter_eligibility` | 15 ms |
| Query encode (diagnostic target) | `vector.embed`, wherever the encode first runs | 40 ms |
| Dense search, chunk text included | `vector.index`, `vector.search` | 15 ms |
| BM25 lane | `bm25` | 10 ms |
| Keyword lane | `keyword` | 10 ms |
| Unit lanes (`mixed` and `unit` result levels) | `semantic_units` | 30 ms |
| Parent hints | `parent_hints`, inside `semantic.search` | 2 ms |
| Graph lane | `graph`, with `graph.seeds`, `graph.resolver` and `graph.expand` inside it | 15 ms |
| Temporal lane, when the temporal lane runs | `temporal` | 5 ms |
| Fusion and multipliers, lexical guard included | `fusion`, `lexical_guard` | 10 ms |
| Hydration: hit construction, excerpts, release gate, serialization, response blocks | `filter_hits`, `release_gate`, `serialize`, `due_state` | 20 ms |
| CLIP lane, where CLIP is enabled | `clip` | 10 ms |

#### Scenario: A stage over its budget is named

- **WHEN** a series meets the 200 ms ceiling but the keyword stage's p95 is more than twice its 10 ms budget
- **THEN** the gate fails and names the keyword stage, its p95 and its budget

#### Scenario: A stage a request did not run is not a zero

- **WHEN** a page-level request runs no unit lanes
- **THEN** the unit-lane stage is reported as not run for that request
- **AND** it is excluded from the unit-lane percentiles instead of counted as 0 ms

#### Scenario: A stage with too few samples gets no verdict

- **WHEN** the temporal lane runs in 12 requests of the hybrid series
- **THEN** the gate reports the hybrid series' temporal stage as "insufficient samples"
- **AND** that stage neither passes nor fails the hybrid series
- **AND** the temporal stage's verdict comes from the dedicated temporal series

#### Scenario: A nested span is counted once

- **WHEN** parent hints run inside `semantic.search`
- **THEN** the parent-hints row counts the `parent_hints` interval
- **AND** no other row counts that interval again

#### Scenario: An encode inside the unit lanes is counted once

- **WHEN** a `mixed` request first encodes the query inside `semantic_units`
- **THEN** that encode is spanned as `vector.embed`, and the encode row counts it
- **AND** the unit-lane row counts `semantic_units` without that interval

#### Scenario: An unlisted span is reported, not hidden

- **WHEN** a request runs `outside_kb`, a span key that no row names
- **THEN** the gate reports `outside_kb` as "unbudgeted" with its own p50 and p95
- **AND** its time is not folded into any row

#### Scenario: Encode answers to its canonical bound

- **WHEN** query encode p95 is above 80 ms and at or below 250 ms
- **THEN** the gate reports encode over its 40 ms target
- **AND** the encode stage does not fail the series

### Requirement: Warm Search Work Is Bounded Per Candidate And Per Matched Row

For a warm search request, each catalogue query keyed by the candidate set
(parent hints, ranking metadata, eligibility, hydration and readiness) SHALL do
work bounded by the candidate count. For a fixed candidate set, that work SHALL
NOT grow with corpus size. A lane driven by a full-text match SHALL do bounded
work per row that its match yields. Work that a gate cannot observe per row, in
a full-text index's own match and in the exact dense scan, stays under the
latency ceilings instead.

Markdown page reads SHALL track the candidate count plus a fixed overfetch, as
`Structural Scaling Is The CI Gate` states; the candidates are the hits that the
response hydrates. Each derived store SHALL open at most one connection and run
at most one readiness proof per scope per request. Ranking, temporal,
graph-seed and unit-lane stages SHALL read page and unit metadata from the
maintained catalogue and sidecars for the current generation, not from
Markdown. A gate SHALL check the work bounds as ratios between two corpus sizes
that hold the same candidate set, and SHALL NOT pin absolute work counts.

#### Scenario: Corpus growth cannot hide per-candidate work

- **WHEN** the same warm request series, with the same candidate set, runs on a generated corpus and on the same corpus grown four times larger
- **THEN** the work of each candidate-keyed catalogue query at the larger size stays within 1.5 times its work at the smaller size
- **AND** the work per matched row of each full-text lane stays within 1.5 times
- **AND** Markdown page reads stay within the candidate count plus the fixed overfetch, and connections and readiness proofs within one per store and scope

#### Scenario: A per-candidate scan fails the gate

- **WHEN** a catalogue query for the candidate set scans every KB row for each candidate, or the candidate list once for each KB row
- **THEN** its work grows with corpus size for the same candidates
- **AND** the structural gate fails and names the stage of that query

#### Scenario: Exact BM25 is judged per matched row

- **WHEN** a query's terms match four times as many rows in a larger corpus
- **THEN** the gate compares the BM25 lane's work per matched row, not per request
- **AND** it passes while that work per matched row stays within the bound

#### Scenario: The structural gate runs on every pull request

- **WHEN** a pull request runs CI
- **THEN** the structural gate runs in the pull-request tier, model-free, with no wall-clock threshold
- **AND** it drives `ask_memory` and `activate_context` as the vault owner and as one admitted principal
- **AND** the admitted principal's bounds fail the gate only once admission no longer sizes the candidate pool, and are reported until then

### Requirement: Search Latency Is Gated Before Release

Two instruments SHALL gate warm search latency, each with the served query
encoder. The absolute verdict on the ceilings and the stage budgets SHALL come
from a run on a quiet workstation, on the reference corpus in a process
restricted to two CPUs, and from the live-cell series after each release,
through the live cell's served transport. That verdict is release evidence.
Each live-cell attempt SHALL record its state as passed, failed or refused,
three distinct visible states, with its sample counts, its load, its contended
samples and the principal it measured. A refused attempt holds no verdict, and
the operator can repeat it on a quiet cell.

In the scheduled and dispatched full CI, the `retrieval-latency` job SHALL
compare head with the pairing base instead: the last release whose live-cell
verdict passed. A release whose verdict failed or was refused never becomes the
pairing base, so drift from a release that met the ceilings stays bounded by
construction. Until a release has passed, the pairing base is the last release
tag, and the job reports that no passed base exists. Both arms run in a process
restricted to two CPUs, on the same runner, the same reference corpus and the
same cases, back to back. The job SHALL compute each case's paired ratio of
head to the pairing base. It SHALL fail a series only when the lower bound of
the 95% confidence interval of those ratios is above 1.10, that is, head at
least 10% slower than the pairing base. A comparison SHALL need at least 20
paired cases; with fewer, it reports "insufficient samples". The job SHALL NOT
apply an absolute latency threshold.

#### Scenario: A constant-factor regression blocks the release

- **WHEN** a merged change makes warm hybrid requests slower than the pairing base by more than 10%, with 95% confidence over the paired cases
- **THEN** the next scheduled or dispatched full CI run fails the `retrieval-latency` job and names the series and the stages where the time moved
- **AND** the release evidence check stays red until a full run passes

#### Scenario: A failed or refused live-cell verdict does not move the pairing base

- **WHEN** the live-cell series after a release fails a ceiling, or is refused for load or contention
- **THEN** the release evidence records that release's state as failed or refused, never as passed and never as absent
- **AND** the next full CI run keeps pairing head with the last release whose live-cell verdict passed

#### Scenario: A slow shared runner is not a regression

- **WHEN** a full CI run lands on a runner that is slower for both arms
- **THEN** the verdict depends only on the paired ratios between head and the pairing base
- **AND** no absolute ceiling or stage budget is checked on the shared runner

#### Scenario: The absolute verdict is workstation and live-cell evidence

- **WHEN** a release is delivered
- **THEN** a quiet workstation run and the live-cell series after the release record the ceiling and stage-budget verdicts
- **AND** each verdict records its state, its sample counts, its load and the principal it measured

#### Scenario: An encoder that cannot load is not a fast encoder

- **WHEN** the served query encoder cannot be loaded in the gate's environment
- **THEN** the encode and dense stages are reported as unknown, never as 0 ms
- **AND** the gate fails the series instead of passing it without those stages

### Requirement: The Reference Corpus And Query Mix Are Fixed

The workstation run and the full-CI paired comparison SHALL measure a
deterministic generated reference corpus with:

- at least 6,500 governed pages, with typed frontmatter and 5 to 25 wikilinks per page;
- realistic prose with a vocabulary of at least 5,000 terms and a median page of 2 to 3 KB;
- transcript pages for 5% of the pages, 10 to 30 KB each;
- at least 45,000 chunks;
- at least 9,500 semantic units, on at least half of the pages.

The query list SHALL hold at least 40 queries per series. About a quarter have
1 to 3 words, about half have 4 to 9 words, and about a quarter have 15 to 40
words, written the way an agent asks. The hybrid mix keeps that shape and is
not skewed toward the temporal lane. A dedicated temporal series, whose queries
all run the temporal lane, SHALL give the temporal stage its samples. The
live-cell series uses the owner's vault and reports its page, chunk and unit
counts in buckets of 500.

#### Scenario: A corpus below the profile is not the reference

- **WHEN** a gate runs on a generated corpus with fewer pages, chunks or units than the profile names
- **THEN** it reports the counts it measured
- **AND** it gives no ceiling verdict for that corpus

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
child stage covers SHALL stay within the larger of fifteen percent of the
parent and 10 ms. Every stage that reports a duration SHALL be an interval
registered with the timing merge, never a manual difference written into the
table.

#### Scenario: A real recall satisfies the attribution bound

- **WHEN** an opt-in timed hybrid recall runs through the public leaf, not a hand-built timing object
- **THEN** the sum of the root-level stages plus `unattributed_ms` does not exceed `total_ms`,
  nested stages being reported under their parent rather than counted again at the root
- **AND** `unattributed_ms <= 0.15 * total_ms` holds

#### Scenario: A costly step cannot hide inside a parent stage

- **WHEN** a sub-step inside a 200 ms `semantic.search` takes 85 ms and registers no interval of its own
- **THEN** the completeness check fails and names `semantic.search` with its uncovered time

#### Scenario: Fixed overhead in a small parent is not a hidden step

- **WHEN** a 20 ms parent stage has 4 ms that no child stage covers
- **THEN** the completeness check passes, because 4 ms is under the 10 ms floor

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
