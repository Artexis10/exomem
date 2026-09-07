## Purpose

Retain proven parsed state so small durable writes avoid repeating unrelated
Markdown parsing and link-dependency discovery while preserving governance.

## ADDED Requirements

### Requirement: Warm writer preparation reuses current corpus metadata

When a complete, current semantic corpus is resident, preparing a writer's
detached link resolver SHALL reuse its path and title metadata without reading
unrelated Markdown bodies, including when the separate writer resolver cache is
cold. Reuse MUST prove the corpus's freshness and configuration identity.
Unprovable or stale metadata SHALL follow the established fresh preparation
path. Pending destinations SHALL remain local to the prepared snapshot.

#### Scenario: First creation preview after corpus warmup
- **WHEN** the semantic corpus is current and the writer resolver cache is cold
- **THEN** a creation preview resolves links identically to a fresh full resolver without rereading unrelated Markdown, and leaves no pending destination in shared resolver state

#### Scenario: Corpus or configuration changed
- **WHEN** retained metadata does not match the requested freshness or its governing configuration
- **THEN** preparation declines that metadata and cannot normalize links using a stale title, deleted target, or incomplete path set

#### Scenario: Broad writer metadata and ordinary recall differ
- **WHEN** a target belongs to a namespace excluded from ordinary recall
- **THEN** writer resolution retains its established behavior without admitting that target's metadata to the ordinary recall projection

#### Scenario: Semantic construction omitted an unreadable non-governed file
- **WHEN** a resident semantic corpus omits a file still represented by the broad writer resolver's path and stem
- **THEN** reuse is declined so link resolution and collision behavior remain equivalent to fresh preparation

#### Scenario: Explicit disk freshness differs from observed event state
- **WHEN** a caller supplies the new disk freshness after editing a title without publishing an event, or supplies an older key against a newer corpus
- **THEN** entries with a different freshness are declined and preparation uses the correct fresh fallback

### Requirement: Topology discovery uses durable raw dependencies

The graph projection SHALL retain source-bound raw link dependencies, including
unresolved targets, and a completeness record for each indexed source. Dependency
discovery for a target creation, deletion, or title change SHALL use these records
to select affected sources without rereading unrelated Markdown bodies. Existing
publication and source-version proofs remain required independently of discovery.
The resulting public graph SHALL equal a full rebuild over the same canonical
state and SHALL retain its established unresolved-link presentation.

#### Scenario: Forward reference becomes resolvable
- **WHEN** a new page resolves an earlier source's raw link
- **THEN** repair includes that source and the new page without reading unrelated bodies to discover the dependency, including after a process restart

#### Scenario: Topology changes introduce or remove ambiguity
- **WHEN** a creation, retitle, deletion, or rename changes stem or title resolution
- **THEN** affected sources are reconsidered under the established path, stem, title, alias, and anchor rules and graph output agrees with a full rebuild

#### Scenario: Unresolved links remain internal dependencies
- **WHEN** a body link has no resolvable target
- **THEN** its dependency remains available for future repair without adding a new public graph node or edge solely to represent that dependency

#### Scenario: Outside target change has no provable prior topology
- **WHEN** an outside-KB retitle or deletion can change an ambiguity and its old resolver state cannot be proved
- **THEN** bounded repair declines and full recovery preserves graph equivalence

### Requirement: Dependency publication is atomic and recoverable

Dependency records SHALL be derived from the same admitted source bytes as their
graph rows and committed atomically with those rows. Deletion and policy removal
SHALL remove the corresponding dependency records. A missing, old, incomplete,
or source-mismatched dependency projection SHALL NOT authorize a current graph
acknowledgement; existing recovery custody SHALL remain responsible for repair.

#### Scenario: Transaction fails during dependency replacement
- **WHEN** an incremental transaction fails between replacing dependencies and publishing graph rows
- **THEN** neither a partial dependency set nor a new acknowledgement becomes visible, and pending work remains recoverable

#### Scenario: Old sidecar is opened
- **WHEN** a sidecar predates the dependency projection or lacks complete source coverage
- **THEN** bounded repair declines it and recovery rebuilds the projection before claiming currentness

#### Scenario: Source becomes excluded from recall
- **WHEN** an indexed source is deleted or changes into a policy-excluded source
- **THEN** its dependency records and graph rows are removed together and cannot contribute to subsequent recall

#### Scenario: A positive lookup conceals incomplete coverage
- **WHEN** one dependent source matches but a second dependent source lacks valid coverage
- **THEN** the entire bounded result is refused and recovery retains responsibility for all affected sources

#### Scenario: Isolation repair encounters corrupt dependency rows
- **WHEN** dependency source identities or stored target/key structure are corrupt
- **THEN** isolation repair removes quarantined rows and their affected coverage claims atomically, while preserving legitimate unresolved target text

#### Scenario: Full rebuild encounters orphan dependencies
- **WHEN** a full rebuild opens a sidecar containing dependencies for a deleted source
- **THEN** rebuild replaces the complete dependency projection and the orphan cannot survive or influence later discovery

### Requirement: Faster preparation preserves durable write semantics

Optimized writes SHALL retain canonical Markdown durability, semantic authoring
validation, identity collision checks, source closure, stale-draft rejection,
and durable deferred-work custody. Preview success SHALL NOT mutate canonical
files or publish pending destinations. The shared governed leaves SHALL continue
to define equivalent behavior across MCP, CLI, and REST.

#### Scenario: Concurrent source or draft change invalidates preparation
- **WHEN** a required source or destination changes after preparation
- **THEN** commit performs the existing revalidation or refusal and does not acknowledge an invalid durable write

#### Scenario: Media work is pending during an ordinary write
- **WHEN** the shared write path runs while media-derived graph work is pending
- **THEN** its speedup leaves artifact custody and graph generation fencing intact

### Requirement: Background scans give bounded priority to foreground requests

The existing graph and due-state background workers SHALL yield briefly at
per-page scan checkpoints while another thread serves a foreground request for
the same vault. Foreground activity SHALL cover the complete shared dispatcher
invocation and SHALL unwind on every return or exception. This activity is an
advisory process-local hint, never an authorization or publication proof.
Each checkpoint SHALL use a monotonic 50 ms waiting budget, request sleeps of
at most 5 ms within the remaining budget, and request no further sleep once
the deadline expires. Operating-system scheduling may overshoot a requested
sleep. The checkpoint SHALL then resume its existing work unit even during
continuous foreground traffic. Activity locks SHALL be released before
sleeping or invoking waiter callbacks.
Explicit graph waiters SHALL dynamically bypass these pauses. Synchronous
foreground work, the background thread's own nested invocation, and requests
for another vault SHALL NOT introduce a pause.

#### Scenario: Foreground request overlaps graph and due-state warming
- **WHEN** an ordinary request runs while those workers scan the same vault
- **THEN** their scan checkpoints can yield within the fixed budget throughout the request, including terminal handling and retrieval, while all admission and publication proofs remain required

#### Scenario: Nested request raises or is cancelled
- **WHEN** a foreground invocation exits through an exception or a nested invocation completes
- **THEN** its activity counters unwind exactly and no completed invocation leaves a phantom foreground holder

#### Scenario: Other vault or synchronous work runs
- **WHEN** a foreground request targets another vault, or a scan runs synchronously outside a background scope
- **THEN** the scan proceeds without cooperative delay and without per-page filesystem or database activity to discover foreground requests

#### Scenario: Background worker invokes a foreground command
- **WHEN** a background-scoped worker enters a nested foreground invocation while another request is also active
- **THEN** that nested invocation suppresses its thread's background scope and never pauses as background work, restoring the scope on exit

#### Scenario: Foreground traffic remains continuous
- **WHEN** another thread continuously serves requests for the same vault
- **THEN** each background checkpoint requests no further waiting after its fixed deadline and resumes the existing work unit instead of suppressing work indefinitely

#### Scenario: Caller explicitly waits for registered graph work
- **WHEN** an explicit response waiter joins a registered graph builder, including during an active cooperative pause
- **THEN** the builder bypasses further waiting within that pause and subsequent checkpoints until its explicit waiters leave, preserving existing join and publication semantics

#### Scenario: Foreground activity ends
- **WHEN** requests finish after delaying background scans
- **THEN** the existing workers complete through normal recovery and produce graph and due-state projections equivalent to synchronous construction without dropping queued work or weakening source-version proofs
