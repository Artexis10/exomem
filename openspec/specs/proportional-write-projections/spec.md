# proportional-write-projections Specification

## Purpose

Retain proven parsed state so small durable writes avoid repeating unrelated
Markdown parsing and link-dependency discovery while preserving governance.

## Requirements

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
to select affected sources without rereading unrelated Markdown bodies.

A refresh or drain that owns the stored topology SHALL prove the topology from the
delta, from the pages the delta affects, and from every page that holds a stored
edge to a moved page. It SHALL NOT read or re-normalise the Markdown bodies of pages
outside that set. A check that the dependency index is complete SHALL answer from a
proof that the sidecar records, and SHALL read the whole index only when none is
recorded. Any change to the file rows or the dependency rows SHALL retire the recorded
proof in the same transaction, whichever writer made it. A writer that keeps the index
complete SHALL record the proof again in its own transaction, after proving the sources
it changed when the proof was recorded at the start of its transaction, otherwise the whole
index. The publication proof and the
source-version proof remain required for every page the pass rewrites, independently of
discovery.

A change with no recorded file event SHALL converge on its event or on the periodic
reconcile. An edit that leaves the file's stat unchanged SHALL converge no later than
the next change to that page or the next whole-vault rebuild.

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

#### Scenario: A stored edge to a moved page is re-derived
- **WHEN** a page's title or existence changes and another source holds a stored edge to it, whichever name produced that edge
- **THEN** that source is re-derived under the new topology and graph output agrees with a full rebuild

#### Scenario: A change with no recorded file event converges later
- **WHEN** a page changes on disk and no file event is recorded for it
- **THEN** an owned refresh or drain does not read the vault to find it, and the graph converges on its event or on the periodic reconcile

#### Scenario: An edit that leaves the file stat unchanged converges by the next change or rebuild
- **WHEN** a page's bytes change while its file stat stays the same
- **THEN** the graph converges no later than that page's next change or the next whole-vault rebuild

#### Scenario: A recorded completeness proof does not outlive the rows it describes
- **WHEN** a repair, a schema migration, a purge, or a writer of another release changes a file row or a dependency row
- **THEN** the next completeness check reads the whole index and answers from the rows, not from the earlier proof

#### Scenario: A cold start does not read the whole dependency index
- **WHEN** an owned refresh or drain runs first after a process start, with the sidecar pages absent from the page cache, and the last writer recorded the completeness proof
- **THEN** the completeness check reads no dependency rows

#### Scenario: A writer that starts without a recorded proof does not record one for pages it did not touch
- **WHEN** a page that the writer does not change has no dependency coverage, and the writer finds no recorded proof when its transaction starts
- **THEN** the writer proves the whole index, finds the gap, and records no proof

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
- **WHEN** a foreground invocation exits through an exception, its entry is interrupted while registering activity, or a nested invocation completes
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

#### Scenario: A process forks while foreground activity exists
- **WHEN** a POSIX child starts after another parent thread held the activity mutex or registered foreground work
- **THEN** the child starts with a fresh activity mutex, empty holder map and empty background scope, so vanished parent threads cannot deadlock or delay its work
