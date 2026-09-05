## MODIFIED Requirements

### Requirement: Explicit Relationship Vocabulary
The graph SHALL consume the portable core and governed vault-extension relation
registry rather than a duplicated relation enum. Accepted typed edges SHALL
retain origin and source provenance and SHALL NOT store authority or confidence
floats on notes. Explicit unknown relation observations SHALL be retained as
unregistered derived edges for audit and later re-resolution, but SHALL NOT be
accepted into normal traversal or assigned a family, inverse, direction, or
epistemic meaning until registered.

#### Scenario: Relationship provenance is returned
- **WHEN** a graph edge is derived from a note's frontmatter, semantic unit, or body
- **THEN** graph lookups return its raw and canonical relation identity, source path, origin method, and source anchor or span when available
- **AND** the relation can be traced back to the file content that produced it

#### Scenario: Unsupported relation label is not persisted
- **WHEN** an explicit typed location contains an unrecognized optional relation label
- **THEN** the graph persists its raw observation and provenance with `registry_status="unregistered"`
- **AND** indexing continues while normal traversal excludes that edge from accepted typed semantics

## ADDED Requirements

### Requirement: Registry changes rebind derived edges without a Markdown census

The graph synchronizer SHALL make `epoch_writes` recognize the exact protected relation-registry target and internally inject a graph generation floor and full-scope checkpoint around caller-supplied registry YAML. Registry callers MUST NOT append epoch files manually. One full-marker convergence dispatcher shared by live post-guard synchronization and `index_sync` deferred drain SHALL publish a new derived snapshot by re-resolving stored raw observations when the graph registry hash differs and schema, source freshness, instance identity, generation, and prior registry hash proofs hold; otherwise it SHALL run the existing recoverable full rebuild. Before dispatch, it SHALL sample the settled epoch and observed marker generation while holding the canonical mutation boundary, leaving pre-commit debt untouched when that boundary is busy. Rebind SHALL preserve edge keys, endpoints, raw labels, exact review-evidence inputs, source provenance, and unrelated metadata while updating registry-derived fields atomically. Successful rebind or rebuild SHALL acknowledge the checkpoint and compare-and-swap clear only the observed full marker. It MUST NOT parse or rewrite every Markdown page while the registry writer boundary is held. Startup and reconcile SHALL use the same dispatcher for a committed unacknowledged registry epoch even if no worker was registered before a process stop.

#### Scenario: Alias registration activates historical raw edges
- **WHEN** a current source graph contains unregistered raw `applies_to` edges and a registry with alias `applies_to` is committed
- **THEN** a derived rebind changes only the registry-derived fields for those edges and publishes one coherent current snapshot
- **AND** page content, source hashes, edge keys, endpoints, and provenance remain unchanged

#### Scenario: Unsafe rebind falls back visibly
- **WHEN** the previous graph is stale, has an incompatible schema, or does not carry the expected prior registry hash
- **THEN** the system does not publish a partially rebound graph
- **AND** it reports or schedules the existing recoverable full rebuild state

#### Scenario: Pre-commit full marker cannot race the registry batch
- **WHEN** the durable full marker is enqueued before canonical registry bytes are replaced
- **THEN** the convergence dispatcher cannot begin rebind or rebuild until it obtains the canonical mutation boundary and observes a settled epoch
- **AND** a successful publication compare-and-swap clears only the marker generation it covered, so newer graph debt survives

#### Scenario: Registry epoch survives every crash cut
- **WHEN** a caught failure occurs before the canonical commit point, an abrupt stop occurs during ordered floor/registry/checkpoint publication, the completed batch stops before worker registration, or private graph publication is interrupted
- **THEN** the caught failure rolls back registry and epoch, while an abrupt mid-batch newer-floor/older-checkpoint cut is classified recoverable rather than current
- **AND** every durable post-registry cut converges through rebind or full rebuild and acknowledges only a coherent snapshot carrying the committed registry hash

### Requirement: Relation filtering expands aliases, families, and replacements coherently

Every graph-backed relation filter SHALL resolve aliases to canonical identity, include extension descendants when a core family is selected, and expand valid acyclic deprecation chains only from their active terminal survivor to deprecated predecessors. A deprecated-key filter SHALL remain limited to that key's own observations and report both immediate replacement and terminal active survivor without pulling in successor observations. Explain and debug output SHALL preserve existing `matched_via="relation_type"` and `matched_via="parent_relation"`, add `matched_via="replacement"` for predecessor matches, and report alias normalization separately as requested and resolved relation identity while preserving stored raw and canonical fields. Match precedence SHALL be exact relation type, replacement, then parent family. Inverse metadata MUST NOT create an edge that was not authored or deterministically derived by an existing rule.

#### Scenario: Exact and family filters agree on one extension edge
- **WHEN** a stored `vault.applies_to` edge has parent `relates_to`
- **THEN** an exact extension filter and a core-family filter can each return it
- **AND** their match provenance distinguishes exact identity from family roll-up

#### Scenario: Replacement expansion reaches historical keys
- **WHEN** an active extension replaces a deprecated extension
- **THEN** filtering by the active extension can return both sets of observations while filtering by the deprecated key returns only its own
- **AND** replacement expansion does not overwrite their stored identities

### Requirement: Relation review queue assembly is graph-native and bounded

The batched relation-review queue SHALL assemble activation eligibility, coverage, deterministic structural candidates, authored-edge suppression, placeholder suppression, stable source ordering, and bounded source groups from one validated graph snapshot. The graph SHALL persist every versioned input needed to reproduce the exact public evidence object, ref, signal version, and fingerprint for each graph-representable deterministic generator, including raw authored body-wikilink target spelling and an internal occurrence order/location needed to select the same occurrence. Internal parity fields MUST NOT alter the public evidence shape. One queue request MUST NOT walk or parse the Markdown corpus, invoke embedding or model scoring, reconstruct the graph, or acquire the Markdown writer boundary. It SHALL preserve unchanged review identity and dismissal behavior, report coverage and truncation honestly, and leave embedding-proximity discovery available only through the explicit per-page suggestion operation.

#### Scenario: Small queue limit does bounded work at vault scale
- **WHEN** a current graph represents 3,600 eligible synthetic pages and the relation queue requests five source groups
- **THEN** candidate assembly uses one graph snapshot and a bounded number of indexed queries independent of the page count
- **AND** it performs zero full-vault Markdown parses and zero corpus cosine searches

#### Scenario: Concurrent mutations do not serialize the queue read
- **WHEN** mutation requests are active while a relation queue read begins
- **THEN** the queue reads a coherent published snapshot without taking the mutation boundary
- **AND** legitimate mutation-busy outcomes elsewhere do not force the queue into a corpus fallback

#### Scenario: Unavailable graph returns a typed state
- **WHEN** no current validated graph snapshot is available
- **THEN** the queue returns bounded `warming`, `pending`, or `unavailable` state with retry guidance and no candidate groups
- **AND** it does not fall back to a Markdown census or embedding work

#### Scenario: Indexed candidates preserve review fingerprints exactly
- **WHEN** an unchanged synthetic corpus is evaluated by the pre-change deterministic generators and by the graph-native batch
- **THEN** every candidate's public evidence object, stable ref, signal version, fingerprint, ordering, and dismissal visibility are byte-for-byte equivalent
- **AND** a body-wikilink candidate retains its authored target spelling and first-occurrence behavior while its public evidence remains exactly `source_path` plus `target`

### Requirement: Relation review decisions revalidate one hinted source

Each relation queue group and item SHALL carry its source path and source content hash. Accept and triage requests for newly returned items MUST echo that source path as a resolution hint. A hinted decision SHALL rederive and validate candidates only for that current source page, current graph snapshot, review fingerprint, and content hash where required. A hintless legacy request SHALL search only the bounded current queue prefix; if no exact ref is found there, it SHALL return refresh-required rather than widening work or scanning Markdown.

#### Scenario: Hinted acceptance stays page-local
- **WHEN** an agent accepts a current queue item with its ref, fingerprint, source path, page hash, and audit reason
- **THEN** the server rereads at most that source page before the governed edit
- **AND** it refuses drift without examining unrelated Markdown pages

#### Scenario: Legacy ref-only triage is bounded
- **WHEN** an existing client triages a relation ref without a source-path hint
- **THEN** the server resolves it only if it occurs in the bounded current queue prefix, otherwise returning refresh-required
- **AND** it performs no activation census, full-vault candidate generation, Markdown walk, or embedding work

#### Scenario: Hintless ref outside the bounded prefix requires refresh
- **WHEN** a pre-upgrade ref identifies a deterministic candidate outside the current bounded prefix or a retired embedding-proximity candidate and no source hint can resolve it safely
- **THEN** the server returns a stable refresh-required result and preserves any recorded review state
- **AND** it does not scan the corpus, widen the queue, or recompute embeddings to recover the proposal
