# derived-receipt-compatibility Specification

## Purpose
Preserve exact mutation replay and unfinished optional derived work when an application rolls back to a compatible earlier release.

## Requirements

### Requirement: Read compatibility is separate from execution capability

A compatibility release SHALL recognize the documented optional catalogue component without executing it.
It SHALL retain unsupported required components as errors.
It SHALL NOT infer that an arbitrary unknown component is optional.

#### Scenario: Catalogue work reaches an earlier application
- **WHEN** a receipt includes the documented catalogue component
- **THEN** the compatibility release reads the receipt and executes only its supported components

#### Scenario: An undeclared component reaches the reader
- **WHEN** a receipt includes a component outside the supported protocol
- **THEN** the release reports unsupported receipt state without mutating canonical data or claiming completion

### Requirement: Historical replay preserves the original mutation proof

A compatibility release SHALL re-prove and replay retained receipts under the existing content, identity, guard and supersession rules.
A missing catalogue row in an older receipt SHALL mean that the batch owes no catalogue work.
Catalogue compatibility SHALL NOT repeat a canonical mutation or weaken exact preparation matching.

#### Scenario: A completed receipt is replayed after rollback
- **WHEN** the earlier application receives the same mutation with a retained catalogue-bearing receipt
- **THEN** it validates the original proof and returns the supported existing result without repeating the canonical mutation

#### Scenario: An old batch has no catalogue row
- **WHEN** the compatibility reader loads a receipt prepared before the catalogue extension
- **THEN** it treats that absent optional row as not required

#### Scenario: The earlier application prepares a new batch
- **WHEN** the compatibility release writes a receipt into the original narrow schema
- **THEN** it inserts only the original nine component rows and does not widen that schema or store a synthetic catalogue row

#### Scenario: Source bytes invalidate historical proof
- **WHEN** current bytes no longer satisfy a retained receipt's proof
- **THEN** existing refusal or supersession behavior applies without a compatibility bypass

### Requirement: Optional custody survives the earlier application's lifetime

The earlier application SHALL preserve unfinished catalogue work and its custody without claiming, dispatching, completing or deleting it.
It SHALL leave that work recoverable by a capable release, including after restart.
Diagnostics SHALL distinguish deferred optional work from executable work and completed work.

#### Scenario: Catalogue work is delayed or claimed
- **WHEN** the store retains delayed, ready or claimed catalogue work during rollback
- **THEN** the earlier application preserves its obligation and does not spend its attempts or steal its claim

#### Scenario: A capable release returns
- **WHEN** a capable release opens the store after earlier-application writes
- **THEN** it recovers retained catalogue work through normal custody and freshness rules

#### Scenario: Recovery fails for supported work
- **WHEN** recovery records a failure while the batch also retains catalogue work
- **THEN** the earlier application leaves the catalogue attempts, backoff and claim unchanged

### Requirement: Acknowledgement depends on the application's supported projections

The compatibility release SHALL preserve acknowledgement and recall proofs for its supported projections.
Deferred catalogue work SHALL NOT independently delay those acknowledgements or hide otherwise admitted recall.
Supported work that remains incomplete SHALL retain its existing pending behavior.

#### Scenario: Supported projections finish before optional catalogue work
- **WHEN** all projections required by the earlier application prove the committed bytes
- **THEN** its normal acknowledgement and recall become available while catalogue custody remains recoverable

#### Scenario: A required supported projection remains pending
- **WHEN** a projection required by the earlier application lacks its proof
- **THEN** catalogue compatibility does not turn the pending projection into a successful acknowledgement

#### Scenario: The process loses the retirement callback
- **WHEN** the process exits after its final supported component commits and before its visibility retirement callback runs
- **THEN** a bounded retry after restart retires the proven visibility while preserving the ready parent and its optional custody

### Requirement: Rollback qualification names installed artifacts

A qualified rollback target SHALL name distinct application, source and official artifact identities.
Qualification SHALL exercise installed earlier and capable applications against the same invented state through the maintained handoff.
Source simulation or an empty queue alone SHALL NOT establish installed rollback compatibility.

#### Scenario: The compatibility artifact passes the journey
- **WHEN** installed applications pass historical replay, deferred custody, ordinary writes and forward recovery
- **THEN** the evidence identifies that exact artifact as the supported rollback target

#### Scenario: Only source simulation is available
- **WHEN** evidence lacks the installed compatibility artifact or retained-history replay
- **THEN** artifact qualification remains incomplete and no compatible rollback is claimed
