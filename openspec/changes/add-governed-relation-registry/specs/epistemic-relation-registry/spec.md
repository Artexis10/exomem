## ADDED Requirements

### Requirement: One versioned core relation registry
The system SHALL define every portable core relation in one versioned registry
consumed by semantic parsing, graph indexing, validation, suggestions, context,
and schema generation. A core relation definition SHALL include canonical key,
description, family, directionality, optional inverse, permitted origins, and
deprecation metadata. Vault configuration MUST NOT override or shadow a core
definition.

#### Scenario: Existing relation behavior survives consolidation
- **WHEN** the registry replaces the duplicated relation enums
- **THEN** every relation accepted before the change parses and indexes with the
  same canonical key and edge direction
- **AND** a parity test fails if parser and graph consumers expose different core
  sets

### Requirement: Governed namespaced relation extensions
The system SHALL load optional relation extensions from a generic governed YAML
file under `Knowledge Base/_Schema/`. Each extension MUST use a lowercase
namespaced key, map to exactly one core parent, describe its meaning, and pass
alias, inverse, node-kind, origin, status, and scope validation. Extension scope
MAY restrict valid projects or page types but MUST NOT redefine the extension's
meaning.

#### Scenario: Empty registry requires no user action
- **WHEN** a user never registers an extension or custom traversal profile
- **THEN** existing Markdown parsing and broad graph-context behavior remain
  compatible without setup prompts, mandatory validation, or corpus rewrites

#### Scenario: Domain relation refines a portable parent
- **WHEN** a valid extension `medicine.contraindicates` declares parent
  `contradicts` and is used inside its allowed scope
- **THEN** the graph records the extension key and its `contradicts` ancestry
- **AND** a traversal selecting the core parent can include the extension without
  treating the two labels as identical observations

#### Scenario: Extension cannot shadow the core
- **WHEN** a proposed extension key or alias collides with a core key or another
  active canonical key
- **THEN** validation refuses persistence with a stable collision finding and
  leaves the current registry unchanged

### Requirement: Raw and canonical relation identity remain distinct
Every derived typed edge SHALL retain the raw observed label, resolved canonical
relation when available, core parent for extensions, registry status, registry
version/hash, origin, source path, source anchor, and target-resolution status.
Alias resolution and registry updates MUST NOT rewrite Markdown automatically.

#### Scenario: Alias resolution remains inspectable
- **WHEN** Markdown uses a registered alias for an extension relation
- **THEN** the edge carries both the raw alias and canonical extension key with
  `registry_status="alias"`
- **AND** context can report how the observation was normalized

### Requirement: Unregistered observations are preserved without semantics
The parser SHALL preserve unregistered relation labels only in explicit typed
locations: semantic-block relation metadata or colon-bearing relation bullets.
Their derived edges SHALL carry raw label, target, and source provenance with
`registry_status="unregistered"`, but MUST NOT receive a core parent, inverse,
symmetry, confidence, or inferred epistemic meaning. Normal traversal profiles
SHALL exclude them; dedicated registry audit and explicit inference SHALL surface
them as advisory findings without adding them to default attention.

#### Scenario: Unknown relation is reviewable rather than dropped
- **WHEN** a page contains `- medicine.replicates [[Target]]` before that label is
  registered
- **THEN** graph diagnostics and corpus inference retain the observed edge and
  exact source anchor
- **AND** normal context warns about but does not traverse the edge as support or
  any other known family

#### Scenario: Navigation bullet does not become ontology noise
- **WHEN** ordinary Markdown contains `- See [[Target]]` with no registered `see`
  relation and no explicit typed-relation colon
- **THEN** normal wikilink indexing may retain the generic link but the registry
  does not create an unregistered typed edge or audit finding for `See`

### Requirement: Corpus relation inference is proposal-first
Relation inference SHALL report bounded frequency and example evidence for core,
extension, alias, deprecated, out-of-scope, and unregistered labels over an
optional project/page-type scope. An unregistered candidate SHALL have no parent
or description unless deterministic alias resolution supplies them. Saving a
registry proposal SHALL be explicit, complete, atomic, and expected-hash guarded
on overwrite.

#### Scenario: Broad corpus proposes but does not adopt vocabulary
- **WHEN** inference observes an unregistered relation repeatedly across a
  selected corpus
- **THEN** it returns counts, example paths/anchors, and an incomplete extension
  skeleton without changing the registry or graph meaning

#### Scenario: Incomplete or stale proposal cannot persist
- **WHEN** save is requested with an unset core parent or a stale expected hash
- **THEN** the write is refused atomically and the existing registry remains
  byte-identical

### Requirement: Optional model suggestions remain outside graph truth
The system SHALL permit an optional model to suggest an extension description
or core parent only when explicitly requested. The capability SHALL be
default-off, response-only,
attributed, and soft-failing. It MUST NOT write registry files, resolve an edge,
change graph traversal, or populate a saved proposal without the reviewed data
being sent back through the guarded deterministic save path.

#### Scenario: Model unavailable does not block deterministic inference
- **WHEN** model suggestions are requested but the optional model dependency is
  absent
- **THEN** deterministic frequencies and proposal skeletons are returned with an
  unavailable warning and no mutation

### Requirement: Deprecation preserves historical observations
Registry definitions SHALL support `active` and `deprecated` status plus an
optional valid `replaced_by`. Deprecated keys and aliases SHALL remain
resolvable for historical Markdown and SHALL surface review findings; deleting a
relation still observed by the corpus MUST be refused or represented as
deprecation.

#### Scenario: Deprecated relation remains readable
- **WHEN** existing Markdown uses a relation later deprecated in favor of another
  key
- **THEN** graph rebuild preserves the historical edge, marks it deprecated, and
  reports the replacement without rewriting the page
