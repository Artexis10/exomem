## Purpose

The recall latency contract states what a warm governed search may cost, gives
each stage of the request a budget inside that cost, bounds the read path's work
per candidate and per matched row, forbids corpus walks, and defines how the
numbers are measured so a contended box cannot be mistaken for a regression.

## ADDED Requirements

### Requirement: Governed Recall Meets Fixed Latency Ceilings On A Quiescent Cell

On the reference corpus in a process restricted to two CPUs, and on the live
cell as it is served, quiescent and with warm caches, every warm hybrid search
series SHALL have p95 at or below 200 ms, and keyword recall, which runs no
query encode, SHALL have p95 at or below 100 ms. The two-CPU restriction applies
to the reference-corpus runs only; the live personal service is not pinned. The
hybrid series are hybrid recall (semantic, BM25, keyword, graph and fusion),
hybrid recall with one supported structured filter, hybrid recall that runs the
temporal lane, and the `mixed` and `unit` result levels.

Each series also has targets, which gate nothing: p95 at or below 100 ms for a
hybrid series, p95 at or below 50 ms for keyword recall, and p50 at or below 50
ms for every series. Every report SHALL show each percentile beside its target
and record each target as met or missed. A missed target SHALL NOT fail a
series or a verdict. The ceilings and the targets are the capability's
contract, not calibrated from any runner, and a gate MUST NOT loosen them. The
empty-query browse is outside these ceilings and targets.

The measured principal SHALL be the vault owner, identified through the
product's owner-authority path without changes to that path. An admitted non-owner
principal SHALL run the same series, reported separately. That series becomes
gating once admission no longer sizes the candidate pool.

On the live cell, contention during a series SHALL be judged per sample from
the request thread's run-queue delay. Each sample's timing diagnostics record
that delay: the change, over the request, in the second field of the served
process's `/proc/self/task/<tid>/schedstat`. The delay includes waits behind
the measured process's own threads. A sample whose request thread waited on a
run queue for more than the larger of 5 ms and 10% of its elapsed time is
contended. The gate excludes contended samples from the percentiles and counts
them, and it refuses a series in which more than 10% of the samples are
contended. Dropping them biases p95 downward, and that cap bounds the bias.
The report SHALL state the count of contended samples and the p95 with them
included, beside the p95 without them. When the delay cannot be read, the gate
reports contention as unknown and refuses the series.

An index build is in progress while a full build or a repair of a derived store
for the request's scope runs: the lexical catalogue, the embedding sidecar, the
semantic-unit sidecar or the graph. Incremental indexing of the pages that one
governed write changed is not an index build.

#### Scenario: Warm hybrid search on the reference corpus

- **WHEN** thirty or more hybrid requests from the reference query mix run back to back through `ask_memory` as the vault owner, against the warm reference corpus, in a process restricted to two CPUs, after the quiescence check passed
- **THEN** the p95 of the elapsed time of the `ask_memory` call is at or below 200 ms
- **AND** the report shows the p95 beside the 100 ms target and the p50 beside the 50 ms target
- **AND** no request in the series reports a corpus walk in any stage

#### Scenario: Every search shape holds its ceiling

- **WHEN** the series repeats as keyword recall, as hybrid recall with a `projects` filter that the index can answer, as the dedicated temporal series, and at `result_level="mixed"`
- **THEN** the p95 of each hybrid series is at or below 200 ms
- **AND** the p95 of the keyword series is at or below 100 ms
- **AND** the eligibility stage of the filtered series reports an index outcome, with its row reported against its stage budget

#### Scenario: The targets gate nothing

- **WHEN** a hybrid series has a p95 of 160 ms and a p50 of 62 ms, and the keyword series has a p95 of 70 ms
- **THEN** the report records the hybrid series' 100 ms p95 and 50 ms p50 targets as missed, and the keyword series' 50 ms p95 target as missed
- **AND** the hybrid series passes its 200 ms ceiling and the keyword series its 100 ms ceiling

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
- **AND** the report shows the series p95 with the contended samples included, beside the p95 without them

### Requirement: Warm Search Stages Stay Within Their Budgets

Each stage of a warm search request SHALL have the fixed p95 budget below.
The budgets are the plan toward the 100 ms hybrid target: they serve the
target, not the ceiling. Budgets compose along the request's critical path:
stages that run one after another add, and of branches that run at the same
time only the longest counts. The encode branch runs the query encode and then
dense search. The lexical branch runs BM25, keyword, CLIP where it is enabled,
and the unit lanes, whose vector search waits for the query vector. For a
request that runs every stage, the critical path of the budgets SHALL be at
most 95 ms, under the 100 ms target. Each row names the timing span keys it
counts. A row counts its
keys and their children, excluding any descendant interval that another row
names. The query encode SHALL be spanned as `vector.embed` wherever it first
runs, inside `semantic_units` included, so that no other row absorbs it. While
the branches run at the same time, the time that `semantic_units` blocks until
the encode on the other thread produces the query vector SHALL be spanned as
`vector.wait`. No row counts `vector.wait`, and it is not reported as
unbudgeted: time blocked on another branch's result is not the stage's own.
The waiting thread may resume after encoding ends; an encode interval need
not cover the entire wait. A span
key that no row counts, by name or as a descendant, SHALL be reported as
"unbudgeted" with its own p50 and p95, net of any descendant interval that a
row names, so time cannot hide in an unlisted key. Examples are `outside_kb`,
`matched_units`, `referents` and the part of `semantic.search` that no child
covers. A stage that a request does not run is reported as not run and is
excluded from that stage's percentiles.

Spans on different threads can overlap. A row's wall time SHALL be the union
of the intervals it counts, so overlap neither shortens nor lengthens it. Each
request SHALL record its critical path: the stages that ran one after another,
plus the concurrent branch that ended last. Explicit fork/join contexts SHALL
record branch lifetimes, including gaps between named stages. These structural
contexts SHALL own no wall time, CPU or budget. All rows in the branch that
ends last belong to the path; tied branches both contribute. Serial setup and
response work remain on the path even when their thread also ran a branch.
Thread identity and overlap windows alone SHALL NOT establish branch identity.
The serial producer SHALL declare its execution model. Missing execution
structure SHALL produce an unknown path, never an inferred path.
A gate SHALL report, for each row,
the p50 and p95 of its wall time and of its CPU time (`cpu_ms`) beside its
budget, and the share of the series' requests in which the stage lay on the
critical path. The share SHALL use only requests with known paths and SHALL
report their count. Unknown paths SHALL NOT become zero membership.
A stage off the critical path still answers to its budget.

A request's overlap is the time during which spans on two or more threads are
open. A row's `cpu_ms` SHALL count the process CPU over the parts of its
intervals outside the overlap only. The request SHALL report the process CPU
over the overlap once, as `overlap_cpu_ms`. Every remaining process CPU delta
SHALL enter its effective row or `unattributed_cpu_ms`, including intervals
excluded from row ownership. Row CPU plus overlap CPU plus unattributed CPU
SHALL equal request CPU. Raw interval coverage still defines `unattributed_ms`;
that wall value may be zero while unattributed CPU is positive. Structural
fork/join contexts SHALL NOT count as open work spans.
No CPU figure SHALL be attributed per thread. The CPU fields SHALL use the
process-wide CPU clock, `time.process_time_ns()`, while timing diagnostics are
on. The supported Linux profile also requires `/proc/self/task` for run-queue
diagnostics. On a platform without that facility or the CPU clock, every CPU
field SHALL read unknown, never 0. An unknown CPU field does not fail a series.

A row's result SHALL need at least 20 samples that ran the stage. With fewer,
the gate reports the row as "insufficient samples", never as a pass or a fail.
A row with enough samples whose p95 is above twice its budget SHALL fail. A
failed row diagnoses: it names the stage, its p95 and its budget, and shows
where the 100 ms target is lost. It SHALL NOT fail the series or the ceiling
verdict, because the target that the budgets serve gates nothing. Query encode
is a diagnostic target: its bound is the encode p95 of 250 ms in
`multilingual-recall`, and the twice-budget rule does not apply to it. When the
encode p95 alone exceeds its budget on the reference profile, the acceptance
run SHALL record that p95 as the measured floor beside the budget, and neither
the ceiling, the target nor the budget widens to fit it. The BM25 and keyword
budgets are targets until a replay on a new connection per request, on a
reference SQLite and over the whole query mix, grounds them. Until then a gate
reports them against their budgets, and the twice-budget rule does not apply to
them. A budget changes only through this requirement.

| Stage | Span keys | Runs | p95 budget |
|---|---|---|---|
| Request setup: recall projection, freshness, pending visibility | `recall_projection`, `pending_visibility`, `freshness`, `cache_lookup` | before the lanes | 4 ms |
| Admission and structured-filter eligibility | `filter_eligibility` | before the lanes | 6 ms |
| Query encode (diagnostic target) | `vector.embed`, wherever the encode first runs | encode branch | 40 ms |
| Dense search, chunk text included | `vector.index`, `vector.search` | encode branch | 15 ms |
| BM25 lane (target) | `bm25` | lexical branch | 8 ms |
| Keyword lane (target), page reads and hit construction included in keyword recall | `keyword` | lexical branch, or alone in keyword recall | 8 ms |
| CLIP lane, where CLIP is enabled | `clip` | lexical branch | 10 ms |
| Unit lanes (`mixed` and `unit` result levels) | `semantic_units` | lexical branch; vector search after the encode | 12 ms |
| Parent hints | `parent_hints`, inside `semantic.search` | after the lanes | 2 ms |
| Graph lane | `graph`, with `graph.seeds`, `graph.resolver` and `graph.expand` inside it | after the lanes | 9 ms |
| Temporal lane, when the temporal lane runs | `temporal` | after the lanes | 3 ms |
| Fusion and multipliers, lexical guard included | `fusion`, `lexical_guard` | after the lanes | 5 ms |
| Hydration: hit construction, excerpts, release gate, serialization | `filter_hits`, `release_gate`, `serialize` | after the lanes | 8 ms |
| Due-state response block | `due_state` | after the lanes | 3 ms |

#### Scenario: A stage over its budget is named and fails only its row

- **WHEN** a hybrid series has a p95 of 140 ms, and the graph stage's p95 is more than twice its 9 ms budget
- **THEN** the gate fails the graph row and names the graph stage, its p95 and its budget
- **AND** the series passes its 200 ms ceiling, and the report records the 100 ms target as missed

#### Scenario: A lexical budget that is still a target fails nothing

- **WHEN** no replay has grounded the keyword budget yet, and the keyword stage's p95 is 20 ms
- **THEN** the gate reports the keyword stage over its 8 ms target
- **AND** the keyword row does not fail, though 20 ms is more than twice its budget

#### Scenario: The budgets fit the target on the critical path

- **WHEN** a request runs every stage, with a structured filter, the temporal lane, the unit lanes and CLIP
- **THEN** its budgeted critical path is 10 ms before the lanes, plus the longer branch, 55 ms, plus 30 ms after the lanes
- **AND** that path, 95 ms, is under the 100 ms target

#### Scenario: Overlapping stages are counted once

- **WHEN** the query encode and the BM25 lane run at the same time for 8 ms
- **THEN** each row reports its own wall time
- **AND** the covered time of their parent counts those 8 ms once
- **AND** neither row's `cpu_ms` counts the CPU of those 8 ms, which the request reports once as `overlap_cpu_ms`

#### Scenario: A unit lane's wait for the query vector is not its own time

- **WHEN** the branches run at the same time and `semantic_units` blocks for 20 ms until the encode produces the query vector
- **THEN** those 20 ms are spanned as `vector.wait`
- **AND** the unit-lane row counts `semantic_units` without that interval
- **AND** the gate does not report `vector.wait` as unbudgeted

#### Scenario: CPU that cannot be read is unknown, not zero

- **WHEN** a gate runs on a platform without `/proc/self/task`
- **THEN** every `cpu_ms`, `overlap_cpu_ms` and `unattributed_cpu_ms` field reads unknown, never 0
- **AND** the wall-time verdicts of the series stand

#### Scenario: An excluded wait cannot lose process CPU

- **WHEN** a unit stage covers 100 ms and an excluded wait covers its first 40 ms
- **AND** the request consumes 80 ms of CPU, including 20 ms during that wait, without cross-thread overlap
- **THEN** the row reports 60 ms of CPU and the request reports 20 ms of unattributed CPU
- **AND** the CPU buckets sum to the request's 80 ms even though raw wall coverage is complete

#### Scenario: A branch gap does not remove its earlier stages

- **WHEN** one branch encodes from 0 to 40 ms and searches from 46 to 50 ms
- **AND** the other branch ends at 45 ms
- **THEN** the explicit branch envelope selects encode and dense search for the path
- **AND** stages in the other branch do not enter the path merely because their intervals overlap

#### Scenario: Shared setup and response work remain on the path

- **WHEN** the request thread runs setup, one branch, and response work after the join
- **AND** the worker branch ends last
- **THEN** the path includes setup, every row in the worker branch, and response work
- **AND** missing branch envelopes produce an unknown path with a visible known-sample count

#### Scenario: A stage off the critical path still answers to its budget

- **WHEN** the encode branch ends after the lexical branch in every `mixed` request, and the unit-lane stage's p95 is more than twice its 12 ms budget
- **THEN** the report shows the unit-lane stage off the critical path
- **AND** the gate still fails the unit-lane row and names the unit-lane stage

#### Scenario: A stage a request did not run is not a zero

- **WHEN** a page-level request runs no unit lanes
- **THEN** the unit-lane stage is reported as not run for that request
- **AND** it is excluded from the unit-lane percentiles instead of counted as 0 ms

#### Scenario: A stage with too few samples gets no verdict

- **WHEN** the temporal lane runs in 12 requests of the hybrid series
- **THEN** the gate reports the hybrid series' temporal row as "insufficient samples"
- **AND** that row neither passes nor fails
- **AND** the temporal row's result comes from the dedicated temporal series

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
- **AND** the encode row does not fail
- **AND** the series still answers to its 200 ms ceiling

#### Scenario: An encode floor above the budget is recorded, not absorbed

- **WHEN** the quiet reference run measures the encode p95 alone at 48 ms on the pinned profile
- **THEN** the acceptance summary records 48 ms as the measured floor beside the 40 ms budget
- **AND** the ceiling stays 200 ms, the target 100 ms and the encode budget 40 ms

### Requirement: Warm Search Work Is Bounded Per Candidate And Per Matched Row

For a warm search request, each catalogue query keyed by the candidate set
(parent hints, ranking metadata, eligibility, hydration and readiness) SHALL do
work bounded by the candidate count. For a fixed candidate set, that work SHALL
NOT grow with corpus size. A lane driven by a full-text match SHALL do bounded
work per row that its match yields. Work that a gate cannot observe per row, in
a full-text index's own match and in the exact dense scan, stays under the
latency ceilings instead.

Markdown page reads SHALL track the candidate count plus a fixed overfetch, as
`Structural Scaling Is The CI Gate` states; the candidates are the hits that
the response hydrates, each with its own page and selected frame sources.
Each keyword hit SHALL use at most two distinct frame pages for its excerpt and annotation;
other hits SHALL use at most one frame page. Keyword recall SHALL order and
limit emitted groups from admitted catalogue metadata and the entire frozen
pending overlay before it reads a page. For unchanged admitted evidence, it
SHALL return the same hits, order, excerpts and frame annotations as exhaustive
recall. Each derived store SHALL open at
most one connection and run at most one full readiness proof per scope per
selection. A request MAY use a cache-probe selection and a selection after
encoding. Activation MAY use an initial lexical selection and a selection for
synchronous compilation after semantic evidence. Each selection SHALL close
before provider work or final evidence checks, respectively. Activation SHALL
defer its lag repair until the request ends, preserving its pinned checkpoint
between selections. Independent readers SHALL retain their current proof, and
an absent checkpoint SHALL NOT gain authority from a retained view.
Ranking, temporal, graph-seed and unit-lane stages SHALL read page and unit
metadata from the maintained catalogue and sidecars for the current generation,
not from Markdown. A gate SHALL check the work bounds as ratios between two
corpus sizes that hold the same candidate set, and SHALL NOT pin absolute work
counts. The gate and every query-plan check SHALL also run on the older
reference SQLite (see `The Reference Corpus And Query Mix Are Fixed`), and the
gate's report SHALL name each SQLite version it ran on.

Keyword grouping SHALL use admitted parent parseability, independent of later
canonical existence. An absent or invalid admitted parent leaves its child as
an orphan. Existing authorization SHALL still suppress a child whose referenced
parent is denied, including a missing parent. An authorized parent outside the
raw candidate scope MAY receive an in-scope child.

Keyword selection SHALL preserve pending-first encounter order for excerpt and
annotation sources. Annotation precedence SHALL remain: the first source's
child annotation, then the emitted parent's transcript and nearest-frame
fallback, then the first later matching child that supplies a frame.
Selected canonical drift SHALL omit the affected group, mark coverage
incomplete, and SHALL NOT promote children, rerank groups, or refill the answer.
Cached keyword hits SHALL retain and validate every selected Markdown source
hash that supplies their disclosed excerpt or frame annotation.

#### Scenario: Corpus growth cannot hide per-candidate work

- **WHEN** the same warm request series, with the same candidate set, runs on a generated corpus and on the same corpus grown four times larger
- **THEN** the work of each candidate-keyed catalogue query at the larger size stays within 1.5 times its work at the smaller size
- **AND** the work per matched row of each full-text lane stays within 1.5 times
- **AND** Markdown page reads stay within the candidate count plus the fixed overfetch, and connections and full readiness proofs within one per store and scope per selection

#### Scenario: Activation retains a checkpoint across its selection phases

- **WHEN** activation pins a catalogue checkpoint that trails only on withheld pages
- **THEN** its initial lexical selection closes before encoding
- **AND** its compilation selection closes before final evidence checks
- **AND** lag repair runs after the activation ends, so it cannot replace that checkpoint between selections

#### Scenario: A retained view preserves admission checks

- **WHEN** an admission reader supplies an explicit checkpoint on its exact retained store and connection
- **THEN** it reuses the view while checking the current policy, schema and semantic identity
- **AND** an absent or mismatched checkpoint retains its refusal behavior

#### Scenario: An unread candidate retains its admitted ranking

- **WHEN** a managed recall admits one catalogue generation and freezes its pending overlay
- **AND** a candidate's canonical bytes change after admission and that candidate is not selected for output
- **THEN** ranking, graph seeds and emitted-parent collapse use the admitted metadata and frozen overlay
- **AND** the later bytes do not rerank the retained selection or cause a ranking-time Markdown read

#### Scenario: Changed selected evidence reduces coverage

- **WHEN** final hydration finds that a selected page or frame differs from its admitted content hash
- **THEN** recall omits that evidence and marks coverage incomplete while preserving unaffected admitted results
- **AND** the partial selection does not enter the shared result cache
- **AND** access-policy and semantic-identity changes retain their existing refusal behavior

#### Scenario: A deleted parent does not promote an unread child

- **WHEN** a managed keyword selection admits a recent child under an older parent
- **AND** another emitted group ranks above that parent
- **AND** the parent's canonical page disappears after admission
- **THEN** the child retains its admitted group and does not outrank the other group
- **AND** selected evidence that cannot be validated is omitted with incomplete coverage and no refill

#### Scenario: Separate excerpt and annotation sources remain verified

- **WHEN** a keyword hit takes its excerpt from a child without a frame file and its annotation from a later matching child
- **THEN** selection retains both child identities and hashes alongside the emitted page
- **AND** a cached read validates both selected child sources before disclosing their evidence
- **AND** a changed child causes that group to be omitted with incomplete coverage

#### Scenario: Modern dense chunks hydrate only selected evidence

- **WHEN** fusion selects a fixed prefix from modern proof-bearing dense candidates
- **THEN** frame collapse carries chunk references without fetching their text
- **AND** only selected excerpts, transcript localization, frame annotations and rerank passages consume chunk text
- **AND** unchanged evidence preserves hits, order, excerpts, timestamps and rerank passages

#### Scenario: Unrelated publication preserves a selected chunk

- **WHEN** another path is published after a modern chunk was scored
- **AND** the existing contiguous path log proves that its source path is unchanged in the same epoch, instance and vector space
- **THEN** the selected chunk text may be read with that proof in one short transaction

#### Scenario: Lost chunk proof cannot replace selected evidence

- **WHEN** a selected modern chunk is missing or its scored publication can no longer be proved
- **THEN** recall omits that selected evidence and marks coverage incomplete without shared caching
- **AND** it does not refill from lower fused rows or substitute a current body or another passage
- **AND** a present empty chunk remains distinct from missing or unproved text

#### Scenario: Legacy dense selection remains explicit debt

- **WHEN** the NumPy matrix belongs to a generation-zero sidecar without durable publication proof
- **THEN** it keeps the existing eager text path and tie behavior
- **AND** modern deferred-text completion and latency claims do not cover that path

#### Scenario: A per-candidate scan fails the gate

- **WHEN** a catalogue query for the candidate set scans every KB row for each candidate, or the candidate list once for each KB row
- **THEN** its work grows with corpus size for the same candidates
- **AND** the structural gate fails and names the stage of that query

#### Scenario: Keyword recall reads only the pages it returns

- **WHEN** a keyword recall with a limit of 10 matches 400 pages, and every matched page's catalogue row is current
- **THEN** it reads the pages of the 10 hits it returns, the frame pages that supply their excerpts or frame annotations, and the fixed overfetch, and no other page
- **AND** its hits, their order, their excerpts and their frame annotations equal those of a recall that reads every matching page

#### Scenario: Exact BM25 is judged per matched row

- **WHEN** a query's terms match four times as many rows in a larger corpus
- **THEN** the gate compares the BM25 lane's work per matched row, not per request
- **AND** it passes while that work per matched row stays within the bound

#### Scenario: The structural gate runs on every pull request

- **WHEN** a pull request runs CI
- **THEN** the structural gate runs in the pull-request tier, model-free, with no wall-clock threshold
- **AND** it drives `ask_memory` and `activate_context` as the vault owner and as one admitted principal
- **AND** the admitted principal's bounds fail the gate only once admission no longer sizes the candidate pool, and are reported until then

#### Scenario: A plan is checked on the older reference SQLite

- **WHEN** a catalogue join plans from `MATCH` on the pull-request tier's SQLite but from `pages_kb` on SQLite 3.45.1, which CI's system Python links
- **THEN** the structural gate fails on 3.45.1 and names the stage of that query
- **AND** its report names both SQLite versions

### Requirement: Search Latency Is Gated Before Release

In the scheduled and dispatched full CI, the `retrieval-latency` job SHALL
gate warm search latency with the served query encoder. It SHALL compare head
with the pairing base: the most recent release tag. That base is code only. It
moves when a release is tagged, and no vault data, live-cell result or box load
moves it. Both arms run in a process restricted to two CPUs, on the same
runner, the same reference corpus and the same cases, back to back.

Both arms SHALL use the same private measurement adapter. It observes the
actual collectors and span boundaries while their original bodies run. It
SHALL NOT change principal authority, admission, selection, egress or returned
fields. Internal timings for an admitted series SHALL remain private to the
operator-owned harness; public timing suppression still applies. Each worker
SHALL measure the entire `ask_memory` call, including response work after the
retrieval timing snapshot. Only the active benchmark worker SHALL run during
a sample, so the inactive arm's native pools cannot consume its CPUs.

The job SHALL compute each case's paired ratio of head to the pairing base. It SHALL
fail a series only when the lower bound of the 95% confidence interval of those
ratios is above 1.10, that is, head at least 10% slower than the pairing base.
A comparison SHALL need at least 20 paired cases; with fewer, it reports
"insufficient samples". A stage that the pairing base does not span yet SHALL
be reported as "not comparable". The job SHALL NOT apply an absolute latency
threshold. The job does not bound drift across releases: each release can be up
to the margin slower than the release before it.

A deliberate slowdown SHALL be accepted only through a committed accept
record, which a pull request adds and the normal independent review checks.
The record SHALL name the release tag it accepts against, each series it
covers, a maximum paired ratio for each, the full-CI run that measured the
slowdown, the reason and the pull request. Each maximum ratio SHALL be that
run's upper confidence bound for the series, rounded up to the next 0.05, so
the reviewer can check the allowance against the run. The job SHALL read the
record on scheduled and dispatched runs alike. A covered series whose
interval's lower bound stays at or below its maximum ratio SHALL report
"accepted" with the reason and SHALL NOT fail. Any other series beyond the
margin, and a covered series beyond its maximum ratio, SHALL fail. A record
whose tag is not the most recent release tag SHALL be ignored and reported as
expired, and it no longer excuses the slowdown it covered. An expired record
has no other effect. The agent or operator who drives the release deletes it,
unless the next accept record replaces it first. No run input SHALL waive the
comparison.

The absolute verdict on the ceilings SHALL come from a run on a quiet
workstation, on the reference corpus in a process restricted to two CPUs, and
from the live-cell series after each release, through the live cell's served
transport. The same runs report each target as met or missed and each stage
row against its budget. The verdict SHALL NOT gate CI, a merge or a release.
Each verdict SHALL be recorded as one of four distinct visible states: passed,
failed, refused or not measured, with its sample counts, its load, its
contended samples and the principal it measured. A refused verdict holds no
ceiling comparison, and the operator can repeat it on a quiet cell. The agent
or operator who rolls a release onto the live cell SHALL run the live-cell
series as a step of the release runbook, validate its summary against the
content-free summary schema, and attach it to that release's GitHub
Release. A release with no attached summary SHALL show
"not measured". A failed or refused live-cell state SHALL open follow-up work.

#### Scenario: A constant-factor regression blocks the release

- **WHEN** a merged change makes warm hybrid requests slower than the most recent release tag by more than 10%, with 95% confidence over the paired cases
- **THEN** the next scheduled or dispatched full CI run fails the `retrieval-latency` job and names the series and the stages where the time moved
- **AND** the release evidence check stays red until a full run passes

#### Scenario: A deliberate slowdown ships through a reviewed accept record

- **WHEN** a merged correctness fix makes warm hybrid requests 15% slower than the most recent release tag
- **AND** a merged accept record names that tag, the hybrid series, the full-CI run whose upper bound for that series was 1.17, a maximum ratio of 1.20 and the fix
- **THEN** scheduled and dispatched runs report the hybrid series as accepted, with the reason and the paired ratios, and do not fail

#### Scenario: An accept record does not cover another regression

- **WHEN** an accept record covers the hybrid series up to 1.20
- **AND** the keyword series is 30% slower, or the hybrid series is 3 times slower
- **THEN** the `retrieval-latency` job fails and names the uncovered or exceeded series

#### Scenario: An accept record expires with its tag

- **WHEN** a new release is tagged after an accept record was merged
- **THEN** the job pairs with the new tag, ignores the record and reports it as expired
- **AND** a covered series that is still beyond the margin against the new tag fails

#### Scenario: A failed or refused live-cell verdict opens work and blocks nothing

- **WHEN** the live-cell series after a release fails a ceiling, or is refused for load or contention
- **THEN** that release's GitHub Release carries its state as failed or refused, never as passed
- **AND** follow-up work is opened that names the release and the series
- **AND** no CI job, merge or release waits on that state
- **AND** the next full CI run pairs head with the most recent release tag, whatever its live-cell state

#### Scenario: A release with no live-cell record shows not measured

- **WHEN** a release's GitHub Release has no attached live-cell summary
- **THEN** that release's live-cell state reads "not measured", never passed

#### Scenario: A slow shared runner is not a regression

- **WHEN** a full CI run lands on a runner that is slower for both arms
- **THEN** the verdict depends only on the paired ratios between head and the pairing base
- **AND** no absolute ceiling or stage budget is checked on the shared runner

#### Scenario: The absolute verdict is a record with a visible state

- **WHEN** a release is delivered
- **THEN** the live-cell series after the release, and a quiet workstation run where one ran, record the ceiling verdict, each target as met or missed, and each stage row against its budget
- **AND** each verdict records its state, its sample counts, its load and the principal it measured
- **AND** a verdict that did not run reads "not measured", never passed

#### Scenario: An encoder that cannot load is not a fast encoder

- **WHEN** the served query encoder cannot be loaded in the gate's environment
- **THEN** the encode and dense stages are reported as unknown, never as 0 ms
- **AND** the gate fails the series instead of passing it without those stages

### Requirement: The Reference Corpus And Query Mix Are Fixed

The workstation run and the full-CI paired comparison SHALL measure a
deterministic generated reference corpus, in a process that links a
reference SQLite, with:

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

A reference SQLite is the one that CI's system Python or a Cloud cell links:
today 3.45.1 in CI's system Python 3.12.3, and 3.46.1 on Cloud cells
(`python:3.12-slim` on trixie). Every run SHALL record its SQLite version. A
workstation run on another SQLite, such as the 3.53 that a local uv venv
bundles, SHALL give no ceiling verdict.

#### Scenario: A corpus below the profile is not the reference

- **WHEN** a gate runs on a generated corpus with fewer pages, chunks or units than the profile names
- **THEN** it reports the counts it measured
- **AND** it gives no ceiling verdict for that corpus

#### Scenario: A SQLite outside the reference is not the reference

- **WHEN** a workstation run links SQLite 3.53 from a local uv venv
- **THEN** it reports that version
- **AND** it gives no ceiling verdict, because its query plans are not the plans that CI or a Cloud cell runs

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

For a real recall with timing diagnostics enabled, the time that the
root-level stage intervals cover, plus `unattributed_ms`, SHALL NOT exceed
`total_ms`, and `unattributed_ms` SHALL NOT exceed fifteen percent of
`total_ms`. Wherever intervals are added up, at the root or inside a parent,
intervals that overlap SHALL count once: the covered time is their union.
Inside a parent stage, time that no child stage covers SHALL stay within the
larger of fifteen percent of the parent and 10 ms. Every stage that reports a duration SHALL be an interval
registered with the timing merge, never a manual difference written into the
table.

#### Scenario: A real recall satisfies the attribution bound

- **WHEN** an opt-in timed hybrid recall runs through the public leaf, not a hand-built timing object
- **THEN** the time that the root-level stages cover, overlapping intervals counted once, plus `unattributed_ms` does not exceed `total_ms`,
  nested stages being reported under their parent rather than counted again at the root
- **AND** `unattributed_ms <= 0.15 * total_ms` holds

#### Scenario: A costly step cannot hide inside a parent stage

- **WHEN** a sub-step inside a 90 ms `semantic.search` takes 40 ms and registers no interval of its own
- **THEN** the completeness check fails and names `semantic.search` with its uncovered time

#### Scenario: Concurrent branches are not uncovered time

- **WHEN** the encode branch and the lexical branch inside a 70 ms `semantic.search` overlap for 30 ms
- **THEN** the covered time of `semantic.search` counts those 30 ms once
- **AND** its uncovered time is its duration less the union of its children's intervals

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
