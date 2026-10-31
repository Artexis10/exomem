## MODIFIED Requirements

### Requirement: Topology discovery uses durable raw dependencies

The graph projection SHALL retain source-bound raw link dependencies, including
unresolved targets, and a completeness record for each indexed source. Dependency
discovery for a target creation, deletion, or title change SHALL use these records
to select affected sources without rereading unrelated Markdown bodies.

A refresh or drain that owns the stored topology SHALL prove the topology from the
delta, from the pages the delta affects, and from every page that holds a stored
edge to a moved page. It SHALL NOT prove the topology over the whole vault. The
publication proof and the source-version proof remain required for every page the
pass rewrites, independently of discovery.

A change with no recorded file event SHALL converge on its event or on the periodic
reconcile. An edit that leaves the file's stat unchanged SHALL converge only on the
next change to that page.

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

#### Scenario: An edit that leaves the file stat unchanged converges on the next change
- **WHEN** a page's bytes change while its file stat stays the same
- **THEN** the graph converges when that page next changes, not before
