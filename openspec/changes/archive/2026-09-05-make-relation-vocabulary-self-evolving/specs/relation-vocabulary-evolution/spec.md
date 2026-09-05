## Purpose

Give reasoning agents a deterministic, governed way to discover, reuse, propose, and activate vault-local relation semantics without turning Exomem into an ontology-authoring agent.

## ADDED Requirements

### Requirement: Relation intent resolution is deterministic and non-authoritative

The system SHALL expose one read-only relation-intent resolution operation through the shared product command surface. Given a requested label and/or a plain-language semantic intent, it SHALL return the complete portable core vocabulary, a bounded relevance-ordered set of active and deprecated vault extensions, exact canonical and alias matches, inverse and immediate/terminal replacement metadata, parent-family evidence, deterministic lexical evidence from labels and descriptions, and bounded indexed recurrence evidence for similar unregistered observations. Exact canonical and alias collision checks SHALL cover the full registry. The response SHALL identify total, returned, and omitted extension and observation counts plus an opaque continuation or inventory route for every unexamined remainder. It MUST NOT invoke a reasoning model, choose a relation for the caller, mutate the registry, or author an edge.

#### Scenario: Existing portable relation remains visible despite different wording
- **WHEN** an agent asks how to express that a child organisation belongs to a parent organisation
- **THEN** the response includes `part_of` in the complete core vocabulary with its meaning, direction, and inverse
- **AND** the response does not propose or create `belongs_to`

#### Scenario: Existing extension alias is found before another relation is proposed
- **WHEN** an active extension already carries a normalized alias matching the requested label
- **THEN** that extension is returned as an exact alias match with its canonical key, parent, description, direction, status, and replacement metadata
- **AND** no registry or Markdown content changes

#### Scenario: Semantic evidence remains evidence
- **WHEN** description or lexical evidence makes several relations appear nearby
- **THEN** the response reports the contributing evidence separately for each candidate
- **AND** it does not assert that the highest-scoring candidate is semantically adequate

#### Scenario: Bounded candidate evidence names what was not examined
- **WHEN** a vault has more extension definitions than one resolver response may return
- **THEN** the response reports the total and returned counts, `extensions_truncated=true`, and a continuation or inventory route
- **AND** exact canonical and alias collisions from anywhere in the registry still appear even if descriptive candidates remain unreturned

### Requirement: Honest generic and abstaining outcomes remain first-class

Relation guidance SHALL state that the caller must choose the most specific registered relation that is truthful, may use `relates_to` when only a meaningful generic connection is justified, and may author no edge when no durable relationship is established. Resolution SHALL NOT fabricate directional semantics from topical proximity, require an extension proposal for every generic edge, or use a vocabulary-diversity quota.

#### Scenario: Legitimately generic connection stays generic
- **WHEN** two notes have a durable connection but the available evidence supports no more specific semantic claim
- **THEN** the resolver presents `relates_to` as an honest available outcome
- **AND** it emits no requirement to create a new relation

#### Scenario: Topical proximity does not become a directional claim
- **WHEN** two notes discuss the same topic without evidence of support, causation, implementation, or another directional relation
- **THEN** deterministic similarity evidence does not convert the connection into `supports`, `causes`, `implements`, or another specific predicate

### Requirement: Vault relation proposals use clean authoring labels and governed canonical identity

The system SHALL expose a read-only relation proposal operation for a reviewed semantic need. The operation SHALL normalize a clean requested label, choose `vault.<label>` as the default canonical key unless an explicit valid namespace is supplied, preserve the clean label as an alias when it differs from the canonical key, and require the caller to provide an explicit core parent, description, and direction. It SHALL return the current registry hash, a complete proposed extension change, validation findings, and the same duplicate-candidate evidence, total/returned counts, and continuation metadata used by resolution. A proposal MUST NOT mutate the registry or graph or imply that a truncated candidate page exhausted the vault vocabulary.

#### Scenario: Policy applicability produces a reviewable extension proposal
- **WHEN** an agent supplies the synthetic intent “an organisation policy applies to a specific employee case”, requested label `applies_to`, an explicit truthful description, parent, and direction
- **THEN** the response proposes canonical key `vault.applies_to` with clean alias `applies_to`
- **AND** it remains a proposal until a separate guarded save

#### Scenario: Incomplete semantic definition is refused
- **WHEN** a proposal omits its description, core parent, or direction
- **THEN** validation returns stable incomplete-proposal findings
- **AND** no registry or graph state changes

#### Scenario: Near-equivalent candidates remain a human-or-agent judgment
- **WHEN** an existing extension has a different label but overlapping description or aliases
- **THEN** the proposal response places that definition in its duplicate-candidate evidence
- **AND** lexical or semantic proximity alone neither vetoes a reviewed distinction nor silently creates a duplicate

### Requirement: Relation changes persist as explicit hash-guarded deltas

The system SHALL expose a governed relation-save operation that accepts a reviewed extension delta, the current registry hash, and a non-empty audit reason. It SHALL merge and validate the delta atomically so a caller need not echo the entire registry. Exact canonical or alias collisions, invalid ancestry, invalid replacement chains, incomplete definitions, and stale registry hashes MUST refuse the write without changing the registry. For an existing canonical extension, every meaning-bearing field and existing alias SHALL remain immutable in place; saves MAY add non-colliding aliases or deprecate an active key with a replacement that is active at that commit. Once deprecated, its status and immediate `replaced_by` link SHALL be immutable. Later deprecation of that immediate target MAY extend the preserved acyclic chain only when its terminal survivor remains active. A semantic change or alias retirement MUST use a new canonical key and deprecate/replace the old definition. The existing full-registry inference/save path SHALL remain readable and SHALL enforce the same semantic-continuity rules.

#### Scenario: Reviewed extension is accepted without whole-registry reconstruction
- **WHEN** a valid proposal is saved with its current registry hash and an audit reason
- **THEN** only the reviewed extension delta is merged into the vault registry
- **AND** unrelated registered extensions remain byte-equivalent in meaning

#### Scenario: Concurrent duplicate proposals converge through optimistic concurrency
- **WHEN** two agents propose equivalent new labels from the same registry hash and one save commits first
- **THEN** the second save is refused as stale
- **AND** a fresh resolve exposes the now-registered extension for reuse instead of silently adding another definition

#### Scenario: Existing canonical meaning cannot be rewritten in place
- **WHEN** a save attempts to change the parent, description, direction, inverse, origins, kinds, scope, or an existing alias of a registered extension
- **THEN** the save is refused without changing the registry or graph epoch
- **AND** the response directs the caller to create a new canonical key and deprecate the old definition when the semantic change is intentional

### Requirement: Accepted extensions become coherent graph semantics

The canonical batch writer SHALL recognize the exact relation-registry target and inject a new full-scope graph epoch floor/checkpoint around the caller-supplied registry YAML; registry callers MUST NOT append epoch files themselves. After that recoverable ordered batch completes, one full-marker convergence dispatcher shared by live synchronization and deferred restart drain SHALL attempt registry-only rebind when registry/source/schema proofs hold and otherwise run the full-rebuild fallback. The durable epoch/full marker SHALL remain sufficient for recovery if the process stops before an in-memory worker is registered, and an abrupt partial publication SHALL be classified through the existing recoverable epoch protocol. A drain awakened by the pre-commit marker MUST NOT start derived work until it has sampled a settled epoch under the canonical mutation boundary. After successful publication it SHALL compare-and-swap clear only the marker generation it covered. The mutation result SHALL report whether the derived graph is completed or pending. Until a graph snapshot carrying the new registry hash acknowledges that epoch, graph-backed reads MUST report a typed warming or pending state rather than serving the old vocabulary as current. Once published, the extension SHALL work for note-level and semantic-unit parsing, exact and alias resolution, graph context and traversal, ordinary recall graph fusion, relation filters, parent-family roll-up, provenance and explain output, schema audit, and relation review.

#### Scenario: Previously unregistered applicability observations activate
- **WHEN** a graph contains a raw `applies_to` observation and the reviewed `vault.applies_to` extension with alias `applies_to` commits
- **THEN** derived synchronization preserves raw `applies_to`, resolves canonical `vault.applies_to`, records its parent and registry hash, and makes the edge traversable after publication
- **AND** Markdown remains byte-identical

#### Scenario: Extension rolls up without losing identity
- **WHEN** `vault.applies_to` inherits from `relates_to`
- **THEN** an exact filter returns the canonical extension observation and a `relates_to` family filter may include it
- **AND** both results preserve the observation's raw label and exact canonical key

#### Scenario: Derived activation is pending under contention
- **WHEN** the registry commit succeeds but the graph publication cannot complete immediately
- **THEN** the mutation reports committed registry state and pending graph synchronization
- **AND** graph-backed reads fail closed with a bounded retryable state until a current snapshot is available

#### Scenario: Registry activation survives a stop before worker registration
- **WHEN** the canonical registry-and-epoch batch commits and the process stops before registering derived work in memory
- **THEN** restart or reconcile discovers the unacknowledged durable epoch and provisions rebind or full rebuild recovery
- **AND** no second registry write or Markdown census is required to make the new vocabulary current

#### Scenario: Live and deferred convergence retire one marker exactly once
- **WHEN** a full marker for a registry epoch is observed by live synchronization or restart drain
- **THEN** both routes use the same rebind-or-rebuild dispatcher and successful publication acknowledges the checkpoint
- **AND** compare-and-swap cleanup removes the observed marker without erasing a newer marker or leaving a redundant rebuild behind

### Requirement: Deprecation and replacement provide a migration path

An extension MAY be deprecated with a valid active `replaced_by` target while historical raw and canonical identities remain readable. Its immediate replacement link SHALL then remain immutable; if that target is later deprecated, validation SHALL preserve an acyclic chain with one active terminal survivor. Resolution and diagnostics SHALL report both the immediate replacement and terminal active survivor. A filter for the terminal active survivor SHALL include observations from its deprecated predecessor chain and identify `matched_via="replacement"` when applicable. A filter for any deprecated key SHALL return only that key's own observations, report the chain metadata, and MUST NOT include successor observations. Every result SHALL preserve its raw label and stored canonical key. The system MUST NOT rewrite Markdown or synthesize inverse edges as part of deprecation.

#### Scenario: Survivor query includes deprecated observations
- **WHEN** `vault.applicable_to` is deprecated in favor of `vault.applies_to` and historical Markdown still uses the old key
- **THEN** filtering for `vault.applies_to` includes the historical observation with replacement-match provenance
- **AND** the result still reports raw and canonical identity from the historical edge

#### Scenario: Deprecated narrow query does not expand into a broad survivor
- **WHEN** a deprecated extension points to an active broader relation such as `relates_to`
- **THEN** filtering for the deprecated key returns only historical observations under that key plus replacement guidance
- **AND** unrelated observations authored directly under the broader survivor are not included

#### Scenario: A replacement target can later be deprecated without rewriting history
- **WHEN** deprecated `vault.old_label` points immediately to `vault.middle_label` and the latter is later deprecated in favor of active `vault.current_label`
- **THEN** `vault.old_label.replaced_by` remains `vault.middle_label`, the chain is acyclic, and diagnostics report terminal survivor `vault.current_label`
- **AND** filtering for `vault.current_label` may include both predecessor histories while either deprecated-key filter remains historically exact

### Requirement: Relation evolution is isolated per vault

Core relation definitions SHALL remain portable and immutable, while extension resolution, recurrence evidence, proposal hashes, saved definitions, graph activation, and review results SHALL be derived only from the addressed vault or hosted tenant cell. No process-global cache or sidecar SHALL allow one vault's extension or unregistered vocabulary to affect another vault.

#### Scenario: Two hosted vaults use different clean aliases safely
- **WHEN** two tenant cells register different namespaced extensions or meanings under their own clean aliases
- **THEN** each cell resolves, filters, and traverses only its own registry and graph state
- **AND** neither cell's resolver response contains the other's extension metadata or recurrence counts

### Requirement: Historical relation audits remain cohort-based and non-authoring

Explicit relation audit and inference SHALL accept caller-supplied origin-date and page-type scopes and report sampled, included, excluded, and undated denominators for core, extension, deprecated, generic, unregistered, zero-authored-relation, and zero-body-connection counts. Origin date SHALL use recorded `created`, then `captured`, and SHALL NOT infer product eras from mutable `updated`, filesystem mtime, paths, or hardcoded release dates. The audit MUST NOT classify every disconnected page as defective, author a backfill edge, or promote a recurring raw label.

#### Scenario: Legacy and recent cohorts can be compared without changing either
- **WHEN** an operator supplies two synthetic origin-date ranges over the same vault
- **THEN** each report exposes its own page-type and relation denominators plus an explicit undated bucket
- **AND** no registry, Markdown, review disposition, or graph meaning changes

#### Scenario: Structural legacy vocabulary remains review evidence
- **WHEN** a historical cohort repeatedly contains structural labels such as `parent` or `sibling`
- **THEN** the audit reports bounded recurrence evidence without treating those labels as approved semantic extensions
- **AND** the existing proposal-first edge and vocabulary workflows remain the only adoption paths
