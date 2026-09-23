## ADDED Requirements

### Requirement: Event-Maintained Epistemic Graph

The system SHALL maintain the epistemic graph incrementally, on the same terms as the
inbound-link index: when a specific set of markdown files changes, the system SHALL
update only the affected files' nodes, edges, and parent references rather than
re-walking the entire knowledge base.

The resulting graph's content MUST be identical to what a full rebuild would produce
for the same vault state. When incremental maintenance is not possible — no graph
sidecar exists, the schema or registry version changed, the batch scope is full, a
lineage reset occurred, or the user invoked reconcile explicitly — the system MUST fall
back to the existing full-vault rebuild, exactly as before this capability existed.

Incremental graph work SHALL be driven from the durable per-path work queue rather than
from in-memory state, so a missed or interrupted drain is repaired by the next one
rather than lost. The existing periodic reconciliation SHALL continue to bound how stale
the graph can become from work that was queued but never drained.

#### Scenario: A single-file change patches only that file's graph entries

- **WHEN** one markdown file changes and the graph is notified of that change
- **THEN** only that file's prior nodes, edges, and parent references are removed and
  recomputed
- **AND** no other file is re-read

#### Scenario: A patched graph matches a full rebuild in content

- **WHEN** the same sequence of file changes is applied once via incremental drains and
  once via a full rebuild from the resulting vault state
- **THEN** the nodes, edges, and parent references are identical between the two

#### Scenario: Incremental maintenance not possible falls back to a full rebuild

- **WHEN** no graph sidecar exists, or the schema or registry version changed, or a
  lineage reset occurred
- **THEN** the graph is computed by a full-vault rebuild, exactly as before this
  capability existed

#### Scenario: An undrained queue is bounded by reconciliation

- **WHEN** paths are queued for graph repair and no drain call site fires
- **THEN** the periodic reconciliation drains them, bounding how stale the graph becomes

### Requirement: A graph proof cools the event registry only on evidence it is behind the disk

A graph proof MAY mark the event registry externally pending only on positive evidence
that the registry is behind the disk (Class C, #508 §1 as amended here):

1. the freshness identity supplied to a whole-vault pass does not name the resolver
   bytes the pass reads; or
2. the recall projection moved across an in-flight whole-vault pass, and at the pass's
   end the registry's recall map differs from the direct-disk stat map on a path that
   neither the registry's complete history since its checkpoint nor a standing
   path-scoped watcher mark accounts for; or
3. the recall policy version or access fingerprint changed across the pass.

Movement the registry accounts for — a governed write the service committed, or an
external event the watcher recorded or marked — SHALL NOT mark the registry externally
pending or invalidate it. A pass that cannot stabilize under it is a publication
failure (Class B): the graph's own recovery state and retry memo, as for any other
publication failure. A comparison that cannot complete, because the registry is not
live or its history is incomplete, proves nothing and is Class B too.

A Class C mark SHALL name the unexplained paths when the proof can enumerate them and
SHALL be unscoped only when it cannot. It is allocated once per proof, and its clearers
are unchanged.

The recall corpus SHALL read each page it walked by the spelling the walk found, as the
freshness identity names it, so a page whose name is not in NFKC form -- a macOS-origin
NFD name on a byte-exact file system -- is in the resolver, and is never evidence under
(1). Classifying reserved names still reads the NFKC, case-folded form.

#### Scenario: Governed writes during every attempt of a whole-vault pass

- **WHEN** governed writes commit during every attempt of a whole-vault pass until its
  re-target budget is spent
- **THEN** the pass fails as a publication failure
- **AND** no external-pending epoch is allocated, recall stays live and the corpus cache
  stays warm

#### Scenario: An unrecorded edit marks exactly its paths

- **WHEN** a page's bytes change during a whole-vault pass without any recorder
  observing it
- **THEN** the pass raises Class C and the external-pending mark names exactly that page

#### Scenario: A page with a decomposed name does not block a whole-vault rebuild

- **WHEN** the vault holds a page whose name is NFD on a byte-exact file system
- **THEN** a whole-vault pass reads it into the resolver, publishes, and the graph shows
  no drift

#### Scenario: An incomplete registry history is not evidence

- **WHEN** a whole-vault pass ends with the registry not live
- **THEN** the pass fails as a publication failure and nothing is marked
