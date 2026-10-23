# Spec Delta

## Purpose

Define comparable latency evidence across public requests, inner operations and background work without confusing their boundaries or coverage. Reuse existing instruments and preserve governed outcomes while locating request cost.

## ADDED Requirements

### Requirement: One shared canary measures live public calls and recall availability

One packaged observer SHALL provide a read-only MCP canary mode for configured positive `ask_memory`, `activate_context` and `read_memory` cases. It SHALL measure the caller round trip through the parsed terminal response, retain every attempted outcome, and use the packaged warm programme bar. Warm public calls SHALL meet p95 below one second on the installed real-vault deployment. The report SHALL bind the deployment, contract, cases and measurement window. It SHALL identify warmth, contention and incomplete evidence. Natural write observations and isolated write acceptance SHALL remain separate from read-only production probes.

The canary SHALL report availability separately from latency. A timeout, unavailable response, malformed terminal, missing expected reference or missing expected material SHALL fail a positive case. A fast refusal SHALL NOT pass availability. The runner SHALL emit content-free JSON, a failing exit status and a structured warning for failed evidence. A standard systemd user timer SHALL run availability checks through an independently selected installed observer. The existing script SHALL delegate its MCP mode to that observer. The canary SHALL NOT change admission, initiate repairs or add an application scheduler. Local failed-unit, journal and doctor status SHALL NOT claim delivered notifications. Unmeasured operations, missing authorization and unverified notification delivery SHALL remain visible as incomplete work.

The selected check SHALL determine exit status. Availability SHALL require every configured case and positive tool coverage. Acceptance SHALL apply all latency and provenance requirements.

Case expectations SHALL support the existing indexed-path format and a strict Draft 2020-12 `response_schema` alternative without mixing formats. The runner SHALL compile schemas before connecting and validate the complete parsed response within the measured interval. Schema references MUST NOT retrieve network or filesystem resources. Conflicting dialect declarations in schema positions SHALL fail case loading without interpreting data-valued assertions as schemas.

An independent source-only author SHALL establish source identity, state, and nonblank material bound to the same result or parent. Membership SHALL NOT require rank unless source truth establishes rank. The author SHALL check missing identity, wrong identity, blank material and material attached to another parent. Schema syntax validation SHALL NOT substitute for those key checks. Frozen keys and failed series SHALL remain unchanged. Replacement keys SHALL start independently authored new series without exposing unused cases.

A response that violates a valid schema SHALL fail the case with `unexpected-response`. An expectation execution or resolution error SHALL report `invalid-expectation`, preserve the parsed outcome and stop the series. That observer failure SHALL leave availability and coverage incomplete and fail the check without claiming a service outage. Reports and warnings MUST NOT expose private schemas, constants, responses or exception details. Legacy expectation behavior and failure codes SHALL remain unchanged.

A mismatched attempt MAY retain `schema_failure` with only the validator keyword and SHA-256 of the first error's schema path encoded as compact JSON. This permitted projection MUST NOT include raw paths, instance paths, messages, schemas, constants or responses. The runner SHALL derive it from the same validation pass. Unavailable metadata SHALL NOT change the established mismatch.

The fingerprint SHALL support correlation within its frozen case and observer version. Different assertions MAY share a validator path through references or composition. Consumers MUST NOT treat the fingerprint as a unique assertion identifier, exhaustive diagnosis or product cause.

#### Scenario: A mismatch retains a safe validator path fingerprint

- **WHEN** a response violates a valid schema with private field names or constants
- **THEN** the attempt retains its existing failure and can correlate the first mismatch by keyword and schema-path fingerprint
- **AND** the report omits raw paths, private values and exception text

#### Scenario: Source-bound material appears after another result

- **WHEN** a membership expectation finds its source identity and nonblank material together after unrelated results
- **THEN** the case passes without requiring the result to occupy a fixed position
- **AND** material from another result or parent cannot satisfy that witness

#### Scenario: Rank is part of the authored contract

- **WHEN** a source-authored expectation requires rank and the complete witness moves to another rank
- **THEN** the case fails despite the witness remaining present

#### Scenario: The observer cannot evaluate an expectation

- **WHEN** an expectation requires an unavailable external schema resource
- **THEN** the observer performs no resource retrieval and retains the parsed outcome with `invalid-expectation`
- **AND** the check fails with incomplete availability and coverage, even after another case passed

#### Scenario: A fast refusal fails availability
- **WHEN** a positive recall case returns a typed warming refusal below the latency ceiling
- **THEN** the canary retains its elapsed time and refusal outcome
- **AND** availability and the release check fail

#### Scenario: Ready custody does not substitute for a real recall
- **WHEN** an external custody census reports ready but the live MCP response cannot return the case's required material
- **THEN** the canary reports the failed public case
- **AND** it does not repair or relabel the response as healthy

#### Scenario: An interrupted series cannot establish acceptance
- **WHEN** a case fails before the declared sample count completes
- **THEN** the report retains attempted cases and marks the remaining coverage incomplete
- **AND** it does not infer a passing percentile from the successful subset

#### Scenario: Healthy availability has insufficient latency evidence

- **GIVEN** every configured positive case returns its expected state, reference and material
- **WHEN** the observer checks availability with fewer samples than the latency contract requires
- **THEN** the selected availability check passes
- **AND** acceptance and latency evidence remain incomplete
- **AND** the observer does not report an availability failure for missing latency samples

#### Scenario: A later failed execution cannot inherit an old success

- **GIVEN** a previous canary report records successful availability
- **WHEN** the scheduled observer fails before it publishes a new report
- **THEN** offline status exposes the failed execution and the age of the previous report
- **AND** the previous result does not establish current healthy availability

#### Scenario: Observer identity does not attest the deployment

- **WHEN** an installed observer checks a separately versioned deployment
- **THEN** its report identifies observer provenance separately from caller-verified deployment provenance
- **AND** availability does not require matching package versions
- **AND** acceptance retains its deployment-proof requirement

#### Scenario: Missing authorization leaves monitoring unconfigured

- **GIVEN** no existing authorized credential is available for the configured endpoint
- **WHEN** the operator prepares the monitor
- **THEN** installation reports missing configuration without enabling an unusable scheduled check
- **AND** it does not issue a credential or change runtime admission

### Requirement: Ordinary warm public writes meet the subsecond target

Ordinary warm note, record, and planning mutations SHALL have public-request p95 below 1,000 ms without increasing the CPU cap. Each declared operation and transport SHALL include all eligible known terminal outcomes and at least 30 samples. Cold startup, contended requests, and explicit heavy work SHALL remain separate populations. Existing immediate-visibility and durable-custody requirements SHALL remain in force.

#### Scenario: An installed warm write series reaches its target

- **WHEN** comparable installed arms execute ordinary warm mutations on identical reference inputs
- **THEN** each declared operation and transport has p95 below 1,000 ms with its outcomes and coverage reported
- **AND** result identity, authorization, custody, and convergence checks pass

### Requirement: Each latency bar has one machine-readable identity

The system SHALL define every latency bar in one versioned machine-readable source. Each bar SHALL identify its surface, operation, profile, boundary, population, outcome-inclusion rule, reference, percentile, comparison, threshold, window, minimum sample count, source coverage and owning requirement. Reports SHALL identify the contract revision. Existing numerical bars SHALL retain their owning requirements without independent copies in consumers.

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

The existing `embeddings.encode` span SHALL report `execution_wait_us` for elapsed time entering the execution context, and `model_encode_us` for elapsed time evaluating and executing the `model.encode(...)` call expression after context entry. The latter includes argument evaluation and the backend call, and ends before context exit. The producer SHALL sum integer microseconds across batches in the same encode invocation. It SHALL record an interval when that interval starts, including when the interval raises, and SHALL omit a field when its interval does not start. These nested durations SHALL NOT be added as exclusive time or treated as native-kernel time.

The existing `recall.semantic.search` span SHALL retain its aggregate duration and count.
Its `entry_ms` field SHALL measure the semantic entry call, including completion when the caller does not defer it.
Its `finish_ms` field SHALL measure only a separate deferred completion.
The producer SHALL sum whole milliseconds, rounded down per entered interval, and omit fields for intervals that never start.
The nested `recall.semantic.preflight` span SHALL cover lazy imports, registry bases, snapshot setup and catalogue readiness before candidate collection.
The public timing table SHALL retain its existing aggregate, and nested durations MUST NOT be added as exclusive time.

#### Scenario: An encode exposes entered intervals without inventing skipped time

- **WHEN** an encode enters the execution context and calls `model.encode`
- **THEN** `execution_wait_us` covers execution-context entry and `model_encode_us` covers evaluation and execution of the call expression
- **AND** a failed entered interval remains reported while an interval never entered is absent
- **AND** the span retains its existing count and caller attribution

#### Scenario: Deferred completion remains distinguishable in a retained aggregate

- **WHEN** a semantic read enters selection and later invokes its deferred completion
- **THEN** the aggregate retains both intervals and reports their `entry_ms` and `finish_ms` fields separately
- **AND** a failed entered interval remains reported without inventing an unstarted phase
- **AND** preflight time remains nested inside the aggregate

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

Live warm `activate_context` SHALL have p95 below 1,000 ms, from public activation-leaf entry through its finalized terminal. Its population SHALL include every eligible terminal outcome with known timing. Reports SHALL bind the installed revision and the scope defined by the bar metadata. Dispatch and caller round trips SHALL appear separately. This bar SHALL preserve packet behavior and MUST NOT be met by increasing the CPU quota.

#### Scenario: Live activation reaches its bar

- **WHEN** an eligible live warm activation series is measured at the public activation leaf
- **THEN** its p95 is below 1,000 ms and the report identifies its complete measurement scope
- **AND** before/after checks show equivalent packet content and authorization behavior under the same CPU cap

### Requirement: Activation's carry stays bounded by the turn and the rarity cap

Activation's retrieval carry MUST NOT rebuild ranking statistics from the knowledge base for an owner whose configuration can withhold no page. It MUST NOT scan every page holding a turn word, and it MUST NOT test corroboration on every matched row. A word's rarity count SHALL stop one qualifying page past the window in which the count can still change a decision. A corroborated ranking SHALL check rows in rank order and stop once it holds the rows it needs. The carry SHALL decide the request's admission on every request and MUST NOT keep it across requests. Configuration SHALL decide which statistics a ranking uses, and the pages that exist MUST NOT. A restricted reader SHALL always filter and rank on the pages it admits. The owner SHALL rank on the catalogue's own statistics only when no governance policy, collection-store marker, bound collection writer or lifecycle marker exists and the owner's content access is unrestricted. With any of those configured, the owner SHALL filter and rank on the pages it admits, even while nothing is withheld. When a request configured for catalogue statistics finds a withheld page row in its snapshot, it SHALL rank on the pages it admits and log a closed reason code.

#### Scenario: An unresolved turn follows a catalogue write

- **WHEN** a turn that resolves nothing reaches both carry rungs right after a catalogue write, for an owner whose configuration can withhold no page
- **THEN** the carry rebuilds no ranking statistics from the knowledge base
- **AND** each word's rarity scan stops within the rarity window and the corroboration checks stop at the requested rows

#### Scenario: Bounded work answers as unbounded work did

- **WHEN** the carry counts rarity or checks corroboration within its bounds
- **THEN** its rarity decisions, the pages it discounts and its first ranked rows equal those of the unbounded computation

#### Scenario: The only withheld page is present or absent

- **WHEN** a restricted reader's admission, or the owner's under a governance policy, withholds exactly one page that holds the turn's words
- **THEN** both carry rungs return the same rows and scores as for the same vault without that page

#### Scenario: An excluded page leaves the owner on the catalogue

- **WHEN** the access policy excludes a page and no governance policy exists
- **THEN** the page has no catalogue row, the owner ranks on the catalogue's statistics, and a catalogue write rebuilds no statistics

#### Scenario: An owner whose configuration withholds nothing ranks on the catalogue

- **WHEN** the owner's configuration can withhold no page and the phrase rung or the title rung ranks the pages a turn names
- **THEN** it orders them by the catalogue's bm25 scores
- **AND** the packet can differ in the order of named domains, the order of listed named pages, which current pages a window crowded by retired pages holds, whether a single named page clears the minimum carry score, and which turn units the term budget keeps

### Requirement: Known terminal outcomes remain in their declared population

Every eligible terminal with known timing SHALL contribute to its declared population, including errors, refusals and abstentions, unless its owning contract explicitly excludes it. Reports SHALL identify outcome restrictions and their owner. A success-only percentile MUST NOT establish an all-request bar. Existing recall warming and contended-sample rules SHALL remain unchanged.

#### Scenario: Slow errors occupy the tail

- **WHEN** an all-request series contains 90 fast successes and 10 errors slower than its p95 threshold
- **THEN** all 100 known timings contribute and the all-request p95 breaches its bar
- **AND** a success-only percentile is labelled as restricted and cannot establish all-request acceptance

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

Each report SHALL state its window, source, observed count, eligible known timings, outcome-inclusion rule, errors, refusals, abstentions, unknown timings, partial readings and retention loss. A bounded ring SHALL identify retained coverage and evictions. An incomplete source MUST NOT claim all requests in the window. Insufficient samples, unknown timing and failed reads SHALL remain visible and MUST NOT count as zero or establish a pass.

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

Compact and session bootstrap SHALL serve a caller-local retained-ring breach
advisory with its bar identity and contract revision. This advisory MUST NOT claim
coverage or installed acceptance. Full and diagnostics bootstrap SHALL retain
spans and coverage. Doctor SHALL report its independent source coverage rather
than imply that its population exactly expands the bootstrap advisory.

#### Scenario: Observation fails

- **WHEN** the watch cannot process an otherwise successful request
- **THEN** the request returns its ordinary result
- **AND** the operator report exposes observation failure without inventing a healthy sample

### Requirement: Budgeted read-only activation completes within its deadline

Budgeted read-only activation SHALL return a packet or its existing budget abstention within the existing absolute deadline, including packet finalization. Deadline handling SHALL reuse the existing budget and safe deferral paths. An overrun SHALL be a failed deadline observation and an unresolved repair. Canonical mutation immunity MUST NOT apply to read-only activation.

#### Scenario: A read-only activation exhausts its budget

- **WHEN** a read-only activation cannot afford its remaining work within the absolute deadline
- **THEN** it returns its existing budget abstention, including finalization, within that deadline

#### Scenario: A reproduction exceeds the deadline

- **WHEN** a bounded isolated activation reproduction returns after its absolute deadline
- **THEN** its deadline observation is failed and the repair remains open
- **AND** merely reporting the overrun or skipping later work does not satisfy the deadline requirement

### Requirement: Deadline handling preserves canonical custody

Canonical mutations SHALL retain `connector-request-budget` and the existing mutation terminal and durable-acknowledgement contracts. A running canonical commit MUST NOT be cancelled. Exact terminals, affected paths, component custody, derived batches, pending-read visibility and authorization scopes SHALL remain intact.

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
