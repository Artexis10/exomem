# Spec Delta

## Purpose

Define comparable latency evidence across public requests, inner operations and background work without confusing their boundaries or coverage. Reuse existing instruments and preserve governed outcomes while locating request cost.

## ADDED Requirements

### Requirement: Each latency bar has one machine-readable identity

The system SHALL define every latency bar in one versioned machine-readable source. Each bar SHALL identify its surface, operation, profile, boundary, population, reference, percentile, comparison, threshold, window, minimum sample count, source coverage and owning requirement. Reports SHALL identify the contract revision. Existing numerical bars SHALL retain their owning requirements without independent copies in consumers.

#### Scenario: Existing instruments use the same bar

- **WHEN** the rolling watch and an existing acceptance gate evaluate the same bar
- **THEN** both resolve its threshold and population from the same contract revision
- **AND** neither consumer keeps its own numerical default

#### Scenario: A boundary changes

- **WHEN** a measurement includes public response construction previously outside its interval
- **THEN** its boundary identity changes and the report records the new start and end
- **AND** it does not present the older interval as equivalent

### Requirement: Unknown thresholds remain pending

A bar without an established threshold SHALL carry a null threshold and an explicit pending state. The system MUST NOT substitute zero, a borrowed threshold, or a passing verdict. Establishing a pending bar SHALL require current measurements and a recorded engineering choice in its owning OpenSpec contract.

#### Scenario: Browse has no established ceiling

- **WHEN** browse latency has samples but its applicable threshold remains pending
- **THEN** the report shows measured percentiles, its population and coverage, and a pending bar
- **AND** it emits no ceiling pass or failure

### Requirement: Measurement boundaries remain distinct

Reports SHALL distinguish public dispatch, public leaf, caller elapsed time, inner operations, canonical-commit-to-response, immediate read, paired write-plus-read and background freshness. Each interval SHALL state its start and end. Inner spans and response timing fields MUST NOT substitute for complete public requests. A paired percentile SHALL use paired elapsed samples, never added percentiles.

#### Scenario: A short validation span conceals a slow acknowledgement

- **WHEN** validation meets its inner-operation bar but the complete public write exceeds its applicable bar
- **THEN** the report preserves both results under different boundaries
- **AND** the inner-operation result does not establish public-write acceptance

#### Scenario: Connector delay exceeds origin work

- **WHEN** a connector round trip exceeds the origin dispatch interval
- **THEN** the report shows the caller interval and origin interval separately
- **AND** unavailable transport or serialization attribution remains unknown

### Requirement: Profiles and populations cannot borrow acceptance

The system SHALL evaluate a bar only against its declared profile, population, reference and window. Warm and cold, model-free and served-model, owner and admitted principal, ordinary and explicit-heavy calls SHALL remain separate where their contracts differ. Reports SHALL expose exclusions, contention and unmeasured dimensions without treating them as healthy observations.

#### Scenario: Model-free CI passes

- **WHEN** the conversation activation CI profile meets `conversation-aware-activation`'s model-free reference bounds
- **THEN** the report records that profile's result
- **AND** live activation acceptance remains unmeasured until a matching live series exists

#### Scenario: A provisional p90 watch is healthy

- **WHEN** retained ordinary recall samples meet the existing watch's provisional p90 bar
- **THEN** the report identifies that percentile and retained population
- **AND** it makes no claim about a live p95 bar or the whole day's traffic

### Requirement: Live warm activation has a public-leaf bar

Live warm `activate_context` SHALL have p95 below 1,000 ms, measured from public activation-leaf entry through its returned packet. Its report SHALL bind the installed revision, served runtime profile, measured input population, reference state, observation window and coverage. MCP or REST dispatch and caller round trips SHALL appear separately. This bar SHALL preserve packet behavior and MUST NOT be met by increasing the CPU quota.

#### Scenario: Live activation reaches its bar

- **WHEN** an eligible live warm activation series is measured at the public activation leaf
- **THEN** its p95 is below 1,000 ms and the report identifies its complete measurement scope
- **AND** before/after checks show equivalent packet content and authorization behavior under the same CPU cap

### Requirement: Each request boundary is observed once

Existing MCP, REST and activation observations SHALL feed the existing latency watch through one shared summary contract. A request SHALL contribute once per boundary and population. Nested activation and middleware observations MUST NOT duplicate one boundary. Existing ledgers and activation logs SHALL retain their meanings; missing correlation SHALL be visible instead of silently merging unrelated calls.

#### Scenario: Activation passes through MCP

- **WHEN** one MCP activation call produces a middleware row and an activation row
- **THEN** its dispatch and activation-leaf intervals remain distinct
- **AND** either interval contributes only once to its own population

#### Scenario: A direct REST call completes

- **WHEN** an admitted REST command succeeds, refuses, or raises after its timed command entry
- **THEN** the applicable REST boundary records its outcome and elapsed time once
- **AND** refusals outside that entry appear as excluded coverage rather than zero-duration command calls

### Requirement: Coverage and outcome counts accompany percentiles

Each report SHALL state its window, source, observed count, eligible known timings, errors or refusals, unknown timings, partial readings and retention loss. A bounded ring SHALL identify retained coverage and evictions. An incomplete source MUST NOT claim all requests in the window. Insufficient samples, unknown timing and failed reads SHALL remain visible and MUST NOT count as zero or establish a pass.

#### Scenario: The ring evicts in-window calls

- **WHEN** the live ring discards calls that fall inside the requested window
- **THEN** its report states retained coverage and the eviction count or that the count is unknown
- **AND** it does not claim the percentile covers every request in that window

#### Scenario: A source fails partway through a read

- **WHEN** a ledger segment cannot be read or a timing row is malformed
- **THEN** the report exposes the failed or partial coverage and the affected count when known
- **AND** it does not present the remaining rows as the complete population

### Requirement: Observation stays advisory and private

Latency observation SHALL reuse existing operator surfaces and preserve their caller isolation. New latency evidence SHALL contain aggregate timings, closed outcome codes and counts, never queries, content, credentials or raw caller identities. Observation failures SHALL remain contained and visible through coverage or failure counts, without changing a request's result or adding synchronous disk reads.

#### Scenario: Observation fails

- **WHEN** the watch cannot process an otherwise successful request
- **THEN** the request returns its ordinary result
- **AND** the operator report exposes observation failure without inventing a healthy sample

### Requirement: Deadline handling preserves canonical custody

Deadline handling SHALL reuse `connector-request-budget` and the existing mutation terminal and durable-acknowledgement contracts. Optional work and waits SHALL honor the absolute deadline at cooperative boundaries and report skips, truncation or overruns. A running canonical commit MUST NOT be cancelled. Exact terminals, affected paths, component custody, derived batches, pending-read visibility and authorization scopes SHALL remain intact.

#### Scenario: A running stage exceeds its reserve

- **WHEN** a stage completes after the absolute deadline because it has no safe interruption boundary
- **THEN** the report records the overrun and later optional work yields
- **AND** the system does not claim its pre-stage reserve check imposed a hard interruption bound

#### Scenario: A write commits while the caller's budget expires

- **WHEN** canonical mutation begins before the request deadline and completes after it
- **THEN** its canonical terminal and exact unfinished component custody remain durable under the existing contracts
- **AND** latency handling neither starts a second mutation nor discards pending visibility

### Requirement: Measured gains preserve data and behavior

A claimed latency gain SHALL use isolated installed before/after arms on the same inputs and matching profiles. Evidence SHALL prove counts and sampled identities at each data hop before measuring derived results. Reports SHALL include per-case differences, confidence intervals, CPU and memory, packet or result identity, failures, quality and freshness separately. An interval including zero SHALL be reported as no measured change.

#### Scenario: A faster arm omits required work

- **WHEN** an arm shortens elapsed time by omitting a required result, changing authorization, or losing pending visibility
- **THEN** its identity or custody check fails and it cannot claim the latency gain
- **AND** moved background work remains visible in separate CPU and freshness evidence
