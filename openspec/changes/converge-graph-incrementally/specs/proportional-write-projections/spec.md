## MODIFIED Requirements

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
it changed, or the whole index when it cannot name them. The publication proof and the
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
