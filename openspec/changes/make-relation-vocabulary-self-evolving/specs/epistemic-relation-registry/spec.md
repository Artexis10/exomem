## MODIFIED Requirements

### Requirement: Corpus relation inference is proposal-first
Relation inference SHALL report bounded frequency and example evidence for core,
extension, alias, deprecated, out-of-scope, and unregistered labels over an
optional project, page-type, and inclusive date scope. An unregistered candidate
SHALL remain semantically incomplete unless deterministic alias resolution
supplies an existing definition. Its promotion candidate SHALL nevertheless be
structurally usable: a normalized clean alias, a non-colliding `vault.<alias>`
canonical key, null reviewed-semantic fields, recurrence counts, and bounded
source examples. Saving a registry proposal SHALL be explicit, complete, atomic,
and expected-hash guarded on overwrite.

#### Scenario: Broad corpus proposes but does not adopt vocabulary
- **WHEN** inference observes an unregistered relation repeatedly across a
  selected corpus
- **THEN** it returns counts, example paths/anchors, and a namespaced promotion
  candidate whose parent, description, and direction are explicitly incomplete
- **AND** it does not change the registry or graph meaning

#### Scenario: Historical scopes can be compared without hardcoded release dates
- **WHEN** relation inference is called for two caller-supplied origin-date ranges
- **THEN** each response reports its sampled, undated, and excluded denominators and relation counts
- **AND** the system uses recorded `created` then `captured` provenance and does not infer product eras from `updated`, filesystem mtime, paths, private labels, or hardcoded dates

#### Scenario: Incomplete or stale proposal cannot persist
- **WHEN** save is requested with an unset required semantic field or a stale expected hash
- **THEN** the write is refused atomically and the existing registry remains byte-identical

### Requirement: Deprecation preserves historical observations
Registry definitions SHALL support `active` and `deprecated` status plus an
optional valid `replaced_by`. Deprecated keys and aliases SHALL remain
resolvable for historical Markdown and SHALL surface review findings; deleting a
relation still observed by the corpus MUST be refused or represented as
deprecation. The immediate replacement of a deprecated key SHALL be immutable.
Replacement chains SHALL be acyclic and terminate at one active survivor even
when an intermediate target is later deprecated; resolution SHALL report both
the immediate and terminal replacements. An active terminal-survivor query
SHALL include its deprecated predecessor chain with explicit replacement-match
provenance, while a deprecated-key query SHALL remain limited to that key's own
observations and report its chain. Both directions retain raw and canonical
observation identity.

#### Scenario: Deprecated relation remains readable
- **WHEN** existing Markdown uses a relation later deprecated in favor of another
  key
- **THEN** derived graph synchronization preserves the historical edge, marks it
  deprecated, and reports the replacement without rewriting the page

#### Scenario: Replacement filters bridge migration safely
- **WHEN** a caller filters by an active relation that replaces a deprecated key
- **THEN** observations under the active key and its deprecated predecessor are eligible with explicit replacement-match provenance
- **AND** filtering by the deprecated key does not include observations authored directly under the active replacement
- **AND** no returned edge is relabelled as if its original canonical identity had changed

#### Scenario: Immediate replacement history survives a later migration
- **WHEN** the active replacement of an older deprecated key is itself deprecated to a new active survivor
- **THEN** validation retains the older immediate link, accepts the acyclic chain, and reports the new terminal survivor
- **AND** no historical registry definition or authored observation is rewritten

## ADDED Requirements

### Requirement: Incremental extension saves preserve the complete registry

A relation extension save SHALL accept a validated delta against the current extension-registry hash, merge it under one guarded mutation, and supply only the resulting complete YAML document to the canonical batch writer. The batch writer SHALL recognize the exact protected registry target and inject its full-scope graph epoch floor/checkpoint so existing debt enqueue, fanout filtering, and handoff repair remain authoritative. The saved response SHALL identify added, alias-extended, and deprecated keys, the previous and new hashes, the audit reason, and derived-graph synchronization state. Validation and duplicate evidence MAY run before writer admission, but the hash recheck and canonical registry batch MUST occur inside the mutation boundary. Existing canonical keys SHALL NOT change meaning-bearing fields or lose aliases in place through either delta or full-document save; intentional semantic evolution SHALL use a new canonical key plus deprecation/replacement.

#### Scenario: Unrelated extensions survive an incremental save
- **WHEN** a vault with two active extensions saves a third extension as a delta
- **THEN** the resulting registry contains all three definitions
- **AND** the saved response reports only the reviewed delta as changed

#### Scenario: Meaning-bearing edit is refused without a graph obligation
- **WHEN** a save attempts to change an existing extension's meaning-bearing definition in place
- **THEN** the registry remains byte-identical and no new graph epoch is committed
- **AND** the finding identifies new-key-plus-deprecation as the migration route

#### Scenario: Registry and recovery demand commit together
- **WHEN** a valid extension delta crosses the canonical commit point
- **THEN** the resulting registry YAML, graph generation floor, and full-scope checkpoint are all durable
- **AND** a process restart can recover the unacknowledged graph obligation without relying on prior in-memory worker registration
