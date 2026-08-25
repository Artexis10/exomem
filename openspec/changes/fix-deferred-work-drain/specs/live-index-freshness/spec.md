## ADDED Requirements

### Requirement: Deferred Index Work Is Retried By The Server

Every deferred-index queue SHALL be drained by the running server without operator
intervention. A queue entry SHALL NOT depend on a human invoking a CLI command, nor on an
unrelated operation running as a side effect, in order to be retried.

The drain SHALL be bounded by the active compute mode's policy, and SHALL share the
reconciliation pass budget rather than opening an independent write path.
The periodic background drain SHALL also respect the mode's smaller live-batch cap;
the larger reconcile cap MUST NOT turn queued repair into one vault-sized publication.
If a bounded batch is incomplete, the drain SHALL isolate only a fixed small number of
receipts in that pass. Unattempted and unsuccessful receipts remain durable for later
passes, while an explicit unbounded operator drain may continue through the whole queue.
An operator-configured live-batch cap of zero SHALL still reserve one background
convergence slot when reconcile budget remains. When that effective cap is one and both
index queues contain work, repeated passes SHALL alternate durably between the queues.

#### Scenario: Queued semantic work drains without operator action

- **WHEN** semantic upserts are queued and the server runs with no operator commands issued
- **THEN** subsequent reconciliation passes drain queued entries
- **AND** the queue count strictly decreases across passes while entries remain
- **AND** the drain admits no more entries per pass than the active mode's cap allows

#### Scenario: Full-upsert work is reachable

- **WHEN** full upserts are queued
- **THEN** a code path exists that clears them
- **AND** that path is reached by server-side drain and by `exomem index`
- **AND** the queue does not grow monotonically across the life of a vault

#### Scenario: Drain does not extend write latency

- **WHEN** a mutation is committed while entries are queued
- **THEN** the mutation does not block on draining those entries

#### Scenario: Performance mode does not create a vault-sized background batch

- **WHEN** performance mode allows a reconcile admission larger than its live-batch cap
- **AND** deferred work remains after drift admission
- **THEN** the periodic drain admits no more than the live-batch cap
- **AND** the remaining receipts stay queued for later passes

#### Scenario: Incomplete background batch has bounded isolation

- **WHEN** a bounded full or semantic batch reports incomplete work
- **THEN** that pass retries only a fixed small number of individual receipts
- **AND** it does not replay every admitted receipt serially in the same pass
- **AND** unsuccessful and unattempted receipts remain durable and rotate across later passes

#### Scenario: Minimum background cap preserves cross-queue convergence

- **WHEN** the effective live-batch cap is zero or one
- **AND** full and semantic work remain queued
- **THEN** a background pass with remaining reconcile budget admits one receipt
- **AND** repeated passes alternate between both queues so neither can starve the other

#### Scenario: Explicit operator drain retains completion semantics

- **WHEN** an operator invokes an unbounded drain explicitly
- **THEN** incomplete batch isolation may continue through every admitted receipt
- **AND** the background-only isolation bound does not silently weaken explicit repair

### Requirement: Deferral Throttles Rather Than Halts

Deferral SHALL trade indexing throughput for latency, never for convergence. In every
compute mode including `quiet`, the per-pass admission for deferred work SHALL be non-zero,
so that a host left running with no operator action converges to an empty queue.

#### Scenario: Quiet mode still converges

- **WHEN** the compute mode is `quiet` and a backlog the size of the corpus exists
- **THEN** each reconciliation pass admits a non-zero, bounded number of entries
- **AND** the backlog reaches zero after a bounded number of passes
- **AND** the work runs within the quiet mode's resource posture

#### Scenario: Quiet deferral reporting matches quiet behaviour

- **WHEN** the watcher reports that it is deferring semantic indexing for a set of files
- **THEN** those files are in fact deferred by the downstream index path
- **AND** the reported deferral and the actual behaviour do not disagree

### Requirement: Queued Entries Are Reconciled Against Index State

A drain SHALL resolve each queued entry against the freshness check the indexer itself
trusts, and SHALL retire entries whose work is already satisfied. A queue SHALL NOT report
outstanding work for files that require no indexing.

#### Scenario: Already-satisfied entries are retired, not re-reported

- **WHEN** entries are queued for files already embedded and current
- **THEN** the drain retires those entries without re-embedding them
- **AND** the reported backlog count drops to reflect only real outstanding work

#### Scenario: Real work is never retired unperformed

- **WHEN** a queued entry names a file that genuinely requires embedding
- **THEN** the drain performs the work before retiring the entry

### Requirement: Backlog Is Reported Honestly

Status output SHALL NOT advertise a `next_action` that no code path performs. When a queue
reports a remediation, that remediation SHALL be either performed automatically or named as
an operator-runnable command.

Health diagnostics SHALL warn when a deferred queue exceeds a meaningful fraction of the
indexed corpus, at a severity an operator reading a summary will notice.

#### Scenario: Status names only real actions

- **WHEN** `status` reports a deferred queue with a `next_action`
- **THEN** that action is either performed by the server automatically
- **OR** it names a command the operator can run verbatim

#### Scenario: Doctor surfaces a corpus-scale backlog

- **WHEN** a deferred queue holds entries exceeding a meaningful fraction of indexed pages
- **THEN** `doctor` reports a warning identifying the queue and its size
- **AND** the vault is not reported as unqualifiedly healthy
