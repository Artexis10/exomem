## Purpose

The recall latency contract states what a governed recall may cost on the live
cell, forbids corpus walks on the read path, and defines how the numbers are
measured so a contended box cannot be mistaken for a regression.

## ADDED Requirements

### Requirement: Governed Recall Meets Fixed Latency Ceilings On A Quiescent Cell

On a quiescent cell of at least 8,000 governed pages with warm caches, a hybrid
recall without structured filters SHALL complete with p50 at or below 300 ms and
p95 at or below 600 ms; a keyword recall SHALL complete with p50 at or below
120 ms; a hybrid recall carrying one supported structured filter SHALL complete
with p50 at or below 400 ms. The ceilings are the capability's contract, not
calibrated from any runner, and a gate MUST NOT loosen them. A request that
returns the typed warming outcome is excluded from the percentiles and counted
separately; more than one warming outcome in a measured series SHALL fail the
gate.

#### Scenario: Warm hybrid recall on the reference corpus

- **WHEN** thirty novel hybrid recalls run back to back against a warm cell of at least 8,000 pages with the load average at or below 2.0
- **THEN** the p50 of their total elapsed time is at or below 300 ms and the p95 at or below 600 ms
- **AND** no recall in the series reports a corpus walk in any stage

#### Scenario: A filtered recall pays only an index lookup

- **WHEN** the same series runs with a `projects` filter that the index can answer
- **THEN** the eligibility stage reports an index outcome with a duration under 20 ms
- **AND** the p50 stays at or below 400 ms

#### Scenario: A contended measurement is refused, not reported

- **WHEN** the gate is started while the one-minute load average is above 2.0
- **THEN** it waits a bounded time for quiescence and otherwise exits without a result, naming the load it observed
- **AND** it never emits ceiling comparisons from samples taken under that load

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
NOT exceed fifteen percent of `total_ms`. Every stage that reports a duration
SHALL be an interval registered with the timing merge, never a manual
difference written into the table.

#### Scenario: A real recall satisfies the attribution bound

- **WHEN** an opt-in timed hybrid recall runs through the public leaf, not a hand-built timing object
- **THEN** `sum(stage.ms) + unattributed_ms <= total_ms` holds
- **AND** `unattributed_ms <= 0.15 * total_ms` holds

#### Scenario: A manual timing write fails the completeness check

- **WHEN** a stage writes its duration into the table without registering an interval
- **THEN** the completeness check fails by reporting that stage as double-counted
