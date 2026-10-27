# Spec Delta

## MODIFIED Requirements

### Requirement: One shared canary measures live public calls and recall availability

One packaged observer SHALL provide a read-only MCP canary mode for configured positive `ask_memory`, `activate_context`, `read_memory`, `search_memory`, `recall_context`, `read_records` and `read_plans` cases. It SHALL measure the caller round trip through the parsed terminal response, retain every attempted outcome, and select a configured latency bar per case, defaulting to the packaged warm programme bar. Warm public calls SHALL meet p95 below one second on the installed real-vault deployment. The report SHALL bind the deployment, contract, cases and measurement window. It SHALL identify warmth, contention and incomplete evidence. Natural write observations and isolated write acceptance SHALL remain separate from read-only production probes.

The canary SHALL report availability separately from latency. A timeout, unavailable response, malformed terminal, missing expected reference or missing expected material SHALL fail a positive case. A fast refusal SHALL NOT pass availability. The runner SHALL emit content-free JSON, a failing exit status and a structured warning for failed evidence. A standard systemd user timer SHALL run availability checks through an independently selected installed observer. The existing script SHALL delegate its MCP mode to that observer. The canary SHALL NOT change admission, initiate repairs or add an application scheduler. Local failed-unit, journal and doctor status SHALL NOT claim delivered notifications. Unmeasured operations, missing authorization and unverified notification delivery SHALL remain visible as incomplete work.

The selected check SHALL determine exit status. Availability SHALL require every configured case and positive tool coverage. Acceptance SHALL apply all latency and provenance requirements.

Case expectations SHALL support the existing indexed-path format and a strict Draft 2020-12 `response_schema` alternative without mixing formats. The runner SHALL compile schemas before connecting and validate the complete parsed response after the measured interval. The caller timer SHALL stop when the client returns its parsed terminal response or transport failure, before observer assertions run. Schema references MUST NOT retrieve network or filesystem resources. Conflicting dialect declarations in schema positions SHALL fail case loading without interpreting data-valued assertions as schemas.

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

Collection cases SHALL permit only nested `operation.kind` values `inspect` and `query`. The observer SHALL validate the generated operation shape before connecting. Imports, mutations, and malformed operations SHALL fail as `invalid-cases` without client calls.

Every selected bar SHALL resolve before connection and cover the MCP surface, the selected tool (explicitly or through its wildcard) and the case's `deep` flag. One coverage predicate on the bar SHALL decide this for observation-bar selection, the observer's pre-connection check and the latency summarizer, so an accepted case cannot report `unavailable` for coverage. A bar that names a typed tool explicitly SHALL report that tool, never `unknown-operation`. Invalid selections SHALL fail as `invalid-observer-or-contract` without fallback or client calls. Availability SHALL default to one sample. Acceptance SHALL default to the largest selected minimum; explicit sample counts SHALL retain their meaning. Each case SHALL apply its own bar and minimum independently.

The observer SHALL expose its timed call as `measure_call(client, case, timeout)` and its case loader as `load_cases`, so another runner measures with the same timer, assertions and failure codes. The timer SHALL stop at the parsed terminal or transport failure, and the case assertions SHALL run afterwards. A case MAY carry an optional `group` label that the observer ignores and never reports.

Each case SHALL report its selected `bar_id`, observed terminal count, and descriptive nearest-rank p50 and p95 over measured attempts, including failures. Transport failures SHALL contribute their elapsed time without counting as received terminals. Unattempted cases SHALL report zero observed terminals and null percentiles. Descriptive percentiles SHALL NOT replace acceptance requirements or establish improvement.

#### Scenario: A collection case attempts an import or mutation

- **WHEN** a collection case selects an import, mutation, or malformed nested operation
- **THEN** the observer reports `invalid-cases` before connecting or calling a tool

#### Scenario: Cases select different configured budgets

- **WHEN** two cases select valid MCP bars with different thresholds and sample minimums
- **THEN** each report retains its selected bar and applies that bar independently
- **AND** a default acceptance run uses the largest selected sample minimum

#### Scenario: A later case selects an incompatible bar

- **WHEN** any case names an unknown bar, or a bar for another surface, another operation or the other `deep` population
- **THEN** the observer reports `invalid-observer-or-contract` before connecting
- **AND** it does not substitute the default bar

#### Scenario: A bar names typed tools explicitly

- **WHEN** one bar lists `search_memory`, `recall_context`, `read_records` and `read_plans` and a case for each selects it
- **THEN** each case reports its own tool and a latency verdict from that bar
- **AND** no case reports `unknown-operation` or `unavailable` for coverage

#### Scenario: A runner measures a case without the canary loop

- **WHEN** a runner loads a case with a `group` label and times it through `measure_call`
- **THEN** it receives the elapsed time to the parsed terminal, the outcome and the failure code
- **AND** the observer's assertions run after the timer stops

#### Scenario: Observer assertions consume substantial time

- **WHEN** a parsed terminal arrives before a costly source or schema assertion runs
- **THEN** elapsed time ends at the parsed terminal and the assertion still determines availability

#### Scenario: A measured failure interrupts the configured cases

- **WHEN** a case records a failed measured attempt and later cases remain unattempted
- **THEN** descriptive percentiles include the failed attempt
- **AND** unattempted cases retain null percentiles and zero observed terminals

## ADDED Requirements

### Requirement: Paired MCP reads have a configured reference and a pending bar

The packaged latency contract SHALL declare the paired MCP read measurement as the reference `mcp-read-paired` and as one pending bar on the boundary `mcp-worker-socket-v1` that covers `read_records`, `read_plans`, `read_memory`, `search_memory` and `recall_context`. The reference SHALL carry the fixture profile digest (null until a profile fixes it), the repeat count, the minimum case count, the confidence, the multiplicity policy, the gating groups with their allowance in milliseconds, and the non-gating groups. An allowance SHALL be null until paired runs of identical code establish it. The number of gating groups SHALL come from this configuration, never from the groups that ran.

The reference SHALL set a p95 target of 100 ms for the six collection-store groups: records list, query and read, and planning list, query and read. That target SHALL be the private-sandbox acceptance bar and SHALL NOT gate any shared runner. The pending bar SHALL NOT observe live traffic, and the earlier bars and references SHALL stay byte-identical under the new revision.

#### Scenario: The pending bar cannot relabel live traffic

- **WHEN** a live MCP call to `search_memory` selects its observation bar
- **THEN** the selected bar is an MCP observing bar on the dispatch boundary
- **AND** the worker-socket bar is never selected without an explicit case bar

#### Scenario: A new revision keeps the earlier rows

- **WHEN** the contract moves from `request-latency-v4` to `request-latency-v5`
- **THEN** every earlier bar and reference line is byte-identical
- **AND** the new reference and bar are the only additions

### Requirement: A paired gate compares the working tree with the latest release on managed workers

A paired runner SHALL time the MCP reads of the working tree and of the latest release through each arm's own managed worker, over the worker's socket boundary. Each arm SHALL own its interpreter, vault, state root, lease coordinator and worker. The runner SHALL time through `measure_call`, so assertions, profiling and statistics stay outside the timer. The inactive worker SHALL stay stopped while the other is timed. The first arm SHALL alternate, and one unmeasured warm-up SHALL precede the measured repeats of each case.

A generated public fixture SHALL define the cases: collections for Records and for Planning with skewed row counts, legacy trackers, and note, search and recall cases. A profile file SHALL drive the generator, and its SHA-256 SHALL equal the contract's fixture profile digest once the contract sets one. Values constrained by a registry SHALL come from the shipped vocabulary packs at run time. Each arm SHALL load the collections through its own change tools before timing. A report SHALL bind the profile digest, the digest of all generated cases, the release tag and its commit.

For each gating group, the runner SHALL take the median over the repeats of the paired difference (head minus base) per case. It SHALL take the mean of the case medians, with a Student-t interval whose per-tail confidence is corrected for the whole family: the number of gating groups in the contract SHALL be the size of the family. A lower bound above the allowance SHALL fail the group. An upper bound at or below the allowance SHALL demonstrate it. Any other interval SHALL be inconclusive: it SHALL raise a visible warning and SHALL pass. List groups SHALL be reported and SHALL NOT gate. While an allowance is null the runner SHALL report the intervals and SHALL state that no allowance is set, and SHALL NOT invent one.

A broken instrument SHALL fail the run: a failed case, a missing pair, a group below the contract's case floor, a missing or undeclared group, a shard that did not report, or a contract digest that differs between fragments or from the working tree. A reduced run SHALL state that it is reduced and SHALL NOT report a pass.

The runner SHALL refuse to time an arm in two cases, each with a closed reason. It SHALL refuse an arm whose vault state directory was created during the run, because state is keyed by the vault path and a new path starts cold. It SHALL refuse an arm when any derived collection in its derived store is not ready, because the store retries a rebuilding collection under the writer lease and adds a tail to unrelated calls. It SHALL refuse an arm that does not become quiet.

The same runner SHALL accept a private prepared vault, its cases and two fixed arm roots outside the repository. It SHALL report each store group's head p95 against the configured target as meets or misses, and the host CPU pressure at the start and at the end of the run. Private inputs SHALL NOT enter the repository, the tests or CI.

#### Scenario: A group slower than its allowance fails

- **GIVEN** a gating group with an allowance and an interval whose lower bound is above it
- **WHEN** the fragments are joined
- **THEN** the group fails and the run fails

#### Scenario: An interval that straddles the allowance warns and passes

- **GIVEN** a gating group whose interval contains the allowance
- **WHEN** the fragments are joined
- **THEN** the group is inconclusive, with a warning
- **AND** the run passes

#### Scenario: No allowance is set

- **GIVEN** a gating group whose allowance is null
- **WHEN** the fragments are joined
- **THEN** the summary reports the interval and states that no allowance is set
- **AND** it reports no verdict number

#### Scenario: A missing pair breaks the instrument

- **WHEN** a case has fewer paired repeats than the contract requires
- **THEN** the run fails with a missing pair, whatever the other intervals say

#### Scenario: A derived collection is rebuilding

- **WHEN** an arm's derived store holds a collection that is not ready
- **THEN** the runner refuses to time that arm and reports a closed reason

#### Scenario: An arm's state is created during the run

- **WHEN** the vault state directory of an arm did not exist before its timed start
- **THEN** the runner refuses to time that arm and reports a closed reason

#### Scenario: A private store group misses its target

- **WHEN** a private run measures a store group whose head p95 exceeds the configured target
- **THEN** the summary reports that group as missing its target
- **AND** it records the CPU pressure at the start and at the end of the run
