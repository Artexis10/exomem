# epistemic-relation-registry Specification

## Purpose
Define every portable core relation in one versioned registry consumed by parsing, graph indexing, validation and suggestions, with governed namespaced extensions loaded from `Knowledge Base/_Schema/`. Raw and canonical relation identity stay distinct, unregistered labels are preserved without semantics, and inference and optional model suggestions only propose.

## Requirements

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

### Requirement: Recurring unregistered relations become proposal candidates

Relation inference SHALL expose recurring unregistered labels as incomplete
promotion candidates at a recurrence threshold defaulting to three. A candidate
SHALL retain the normalized observed label as a clean alias and use a
non-colliding namespaced canonical key. Its parent, description, and direction
SHALL remain unset for explicit caller judgment. Below-threshold observations
SHALL remain counted evidence, not proposed vocabulary. Inference MUST remain
read-only; persistence requires a separately reviewed complete proposal and the
existing hash, semantic-continuity, and observed-deletion guards.

#### Scenario: Label at threshold is proposed
- **WHEN** inference observes the same unregistered label three times under the
  default threshold
- **THEN** it returns a namespaced promotion candidate with its clean alias and
  explicitly incomplete semantic fields
- **AND** it creates or modifies no registry file

#### Scenario: Label below threshold stays observational
- **WHEN** a label is observed fewer than three times under the default threshold
- **THEN** its count and examples remain in the response without a candidate

#### Scenario: Proposal requires explicit guarded save
- **WHEN** inference returns recurring candidates without a separate save
- **THEN** it writes nothing
- **AND** a later reviewed save must satisfy completeness, current-hash,
  semantic-continuity, and observed-deletion validation

### Requirement: New authored-edge cohorts report their provenance and coverage

The census SHALL preserve its historical graph cohort and report a separate new authored-edge cohort split by endpoint class.
The cohort SHALL use server UTC time and exact governed write additions from the existing atomic operation log, including rotation archives.
Spelling-only edits, inherited predecessor edges, replay and graph rebuilds SHALL NOT create additions.
Page creation dates and graph rebuild timestamps SHALL NOT substitute for edge authoring time.

Every metric SHALL state its numerator, denominator, window, source and coverage.
Metrics SHALL cover generic share, referent outcomes and placement by actual basis.
Among the new edges that writers authored as a relation, meaning a `## Relations` bullet or a semantic relation and never a body wikilink, metrics SHALL also cover `specific_share`, the share that names a specific relation, and `plain_link_share`, the share that is a plain link.
Events logged before the generic-relation refusal keep their recorded edge reasons in the existing reason metrics, and no later event records a reason.
A general mutation rationale SHALL NOT count as an edge-review reason.
Missing history, unsupported writers, an event that names no relation, or missing instrumentation SHALL remain partial or unavailable, never counted zero.
Referent outcomes SHALL require correlated observations; a cleared review item SHALL NOT count as acted.

The default review target SHALL be fifteen percent generic among new authored edges that touch an entity over thirty UTC days.
The vault MAY configure the target and window; crossing the target SHALL emit only an audit or upkeep signal.
A write cannot add a generic relation, so the generic share covers cohorts that the refusal does not cover, such as bulk imports, and events logged before it.
Note-to-note edges SHALL have no target, and the census SHALL NOT refuse a write.

#### Scenario: An edit and graph rebuild preserve event identity

- **WHEN** a governed edit adds an edge to an old page, a replay repeats that edit, and the graph rebuilds
- **THEN** the new cohort counts one authored addition and the historical graph cohort keeps its existing meaning

#### Scenario: Rotation retains authored history

- **WHEN** a recorded edge addition moves into a rotation archive within the selected window
- **THEN** the census still includes it once with its recorded time and endpoint class

#### Scenario: Uninstrumented history is unknown

- **WHEN** retained history lacks the structured data required by a metric
- **THEN** that metric reports partial or unavailable coverage with its source instead of a measured zero

#### Scenario: The share of specific and plain-link edges is reported

- **WHEN** the window holds new authored relation edges, some specific and some plain links, and a body wikilink
- **THEN** `specific_share` and `plain_link_share` count only the authored relation edges and report their numerator, denominator and coverage

#### Scenario: A log that cannot classify an edge reports unavailable

- **WHEN** a logged edge names no relation
- **THEN** the specific and plain-link shares exclude it from their denominator and report their coverage, and report unavailable when no edge can be classified

#### Scenario: The configured target creates an audit review item

- **WHEN** the recorded generic share for edges that touch an entity exceeds the configured target
- **THEN** the `relation_generic_share` audit check returns an informational review item with the measurement, window, source and coverage
- **AND** raising the target above that share removes the item without changing any write permission
