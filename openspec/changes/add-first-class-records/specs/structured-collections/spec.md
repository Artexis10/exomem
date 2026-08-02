## ADDED Requirements

### Requirement: Human-owned collection manifests
The system SHALL represent each explicit structured collection with an ordinary, human-readable Markdown manifest under the governed Knowledge Base. The manifest SHALL carry a stable collection identifier, title, semantic profile, schema version, storage strategy and canonical source, item-schema reference or inline schema, lifecycle, and optional templates, views, governance classification, and links. The manifest SHALL be the collection contract, not a copy of its items, and future `records` and `planning` profiles SHALL use this same contract.

#### Scenario: Manifest remains understandable without Exomem
- **WHEN** a user opens a collection manifest in an ordinary editor
- **THEN** its identity, purpose, canonical source, schema, templates, links, and storage strategy are readable without a plugin or hidden database

#### Scenario: Unknown manifest version refuses safely
- **WHEN** Exomem encounters a manifest version or storage format version it does not support
- **THEN** it refuses mutation and reports the unsupported version without changing the manifest or canonical items

#### Scenario: Duplicate collection identity is ambiguous
- **WHEN** two releasable live manifests declare the same stable collection identifier
- **THEN** discovery and mutation refuse the duplicate identity rather than choosing one by path order, while any withheld candidate remains indistinguishable from absence

#### Scenario: UUID discovery authorizes before parsing identity
- **WHEN** a caller resolves a collection UUID without supplying a manifest path
- **THEN** Exomem performs a bounded symlink-safe search under governed roots, authorizes each candidate path before parsing identity-bearing contents, and treats any derived catalog only as lookup acceleration

### Requirement: Separate semantic profiles over shared mechanics
The collection substrate SHALL keep collection mechanics independent from semantic meaning. `records` SHALL mean observed facts or events and `planning` SHALL mean intended future state; adding another profile SHALL NOT require a fork of identity, schema, storage, mutation, query, audit, rendering, or direct-edit inspection code.

#### Scenario: Future Planning manifest resolves through the same loader
- **WHEN** a valid manifest declares `semantic_profile: planning`
- **THEN** the generic collection loader can inspect its identity, schema, storage, links, and views without treating its items as Records

#### Scenario: Unknown profile does not become Records
- **WHEN** a manifest declares an unsupported semantic profile
- **THEN** the substrate reports that profile as unsupported and does not silently apply Records semantics

### Requirement: Three portable canonical storage strategies
The substrate SHALL support chronological Markdown logs, one Markdown file per item, and CSV, TSV, or JSON datasets as declared canonical storage. Canonical data SHALL remain in those human-owned files; any SQLite state, cache, index, export, summary, or generated view SHALL be derived and rebuildable.

#### Scenario: Chronological Markdown stays canonical
- **WHEN** a log-backed collection is queried or safely mutated
- **THEN** the declared Markdown log remains the sole canonical item history and no generated dataset is promoted implicitly

#### Scenario: File-per-item collection uses ordinary properties
- **WHEN** a Markdown-item collection stores a record
- **THEN** the record is an ordinary Markdown file with stable item and collection identifiers plus typed YAML properties and an optional readable body

#### Scenario: Dataset stays directly editable
- **WHEN** a dataset-backed collection uses CSV, TSV, or JSON
- **THEN** its rows remain readable and editable with ordinary tools, Exomem can query them without an opaque database, and this delivery refuses dataset append/update rather than silently reserializing the file

### Requirement: Collection-scoped item identity and exact source versioning
Every safely mutable item SHALL expose an identity tuple `(collection_uuid, canonical_item_key)` and an item version derived from its exact current source bytes. New agent-authored Markdown items SHALL receive an explicit UUID item key. Query-only datasets MAY expose a bounded manifest-declared string key, but arbitrary dataset keys SHALL NOT be treated as global Exomem IDs. Standalone record references SHALL use the namespaced form `exomem://record/<collection-uuid>/<percent-encoded-key>`.

#### Scenario: Explicit identity survives a non-semantic edit
- **WHEN** a user changes an item field or body without changing its explicit item identifier
- **THEN** the item identifier remains stable and its item version changes

#### Scenario: Legacy deterministic identity remains queryable
- **WHEN** a legacy Markdown block has no explicit item identifier but its declared natural key is unique
- **THEN** the adapter serializes schema version plus natural-key fields in declared order using Unicode-NFC strings, normalized ISO dates/datetimes, explicit JSON nulls, and typed JSON scalars, then returns a deterministic collection-scoped compatibility key marked as inferred

#### Scenario: Corrected inferred natural key can change compatibility identity
- **WHEN** a user corrects a natural-key field on an unmarked legacy item
- **THEN** the inferred compatibility key may change and Exomem does not claim it is a durable substitute for an explicit item key

#### Scenario: Duplicate legacy natural key refuses update
- **WHEN** two legacy blocks resolve to the same declared natural key
- **THEN** both remain readable but targeted update refuses with an ambiguity error and names no arbitrary winner

### Requirement: Minimal typed item schemas
Collection schemas SHALL support required and optional open-vocabulary fields with bounded primitive types, enums, arrays, objects, date or datetime values, units metadata, and link fields. The substrate SHALL impose only identity and schema-version mechanics universally; occurred time, status, units, provenance, relations, uncertainty, reconstruction, and lifecycle SHALL be present only when the collection schema makes them meaningful.

#### Scenario: Domain fields validate before publication
- **WHEN** an item violates a required field, type, enum, identifier, or declared unit constraint
- **THEN** append or update refuses before any canonical file or history entry is published

#### Scenario: Optional schema inference is advisory
- **WHEN** Exomem infers a candidate schema from existing human-authored items
- **THEN** it returns a bounded proposal with provenance and does not make that proposal binding or rewrite historical items without explicit adoption

### Requirement: Guarded Markdown collection mutation
Append and targeted update for Markdown-log and Markdown-item storage SHALL accept structured item data rather than an arbitrary whole-file replacement. Mutations SHALL re-read and resolve their exact target inside the existing same-vault mutation boundary, validate before staging, honor expected containing-file hash and expected item version where applicable, publish through guarded atomic replacement, preserve untouched bytes and newline style, and refuse stale or ambiguous targets. Dataset mutation is outside this delivery.

#### Scenario: Stale container hash refuses
- **WHEN** a direct human edit changes the containing canonical file after an agent read
- **THEN** a mutation carrying the prior expected hash refuses as stale and leaves every file unchanged

#### Scenario: Stale item version refuses
- **WHEN** the intended item changed after an agent read even if its identifier still resolves
- **THEN** targeted update refuses as stale rather than overwriting the newer item

#### Scenario: Untouched log bytes are preserved
- **WHEN** one Markdown block is appended or updated
- **THEN** content outside the exact insertion or item span, including notation, legend, whitespace, UTF-8 BOM, final-newline state, and CRLF/LF style, remains byte-identical

#### Scenario: Windows replacement failure rolls back safely
- **WHEN** an open-file or case-insensitive-path collision causes a staged replacement to fail on Windows-compatible semantics
- **THEN** caught-error rollback preserves the prior canonical files and the operation reports a non-committed outcome

#### Scenario: Same-vault mutations serialize
- **WHEN** two cooperating agent mutations target the same vault concurrently
- **THEN** they enter the existing same-vault writer boundary and cannot publish a torn or silently lost update

#### Scenario: Separate vaults do not contend
- **WHEN** mutations target different vault roots
- **THEN** collection serialization does not introduce a global cross-vault lock

### Requirement: Idempotent append and conflict-safe update
The substrate SHALL make exact append retries idempotent where a stable item identity is available. Reusing one identity with different content SHALL refuse as an identity conflict. Targeted update SHALL change only the resolved item and SHALL never fall back from a missing identifier to fuzzy text matching.

#### Scenario: Exact append retry produces one item
- **WHEN** a client retries the same append with the same collection, item identity, and normalized payload
- **THEN** the substrate returns the committed item without adding a duplicate

#### Scenario: Reused identity with different content refuses
- **WHEN** an append supplies an existing item identity with materially different content
- **THEN** it refuses with a record identity conflict and preserves the existing item

#### Scenario: Missing identifier does not select a similar item
- **WHEN** targeted update names an identifier that no longer exists
- **THEN** it refuses as missing or stale and does not update an item with similar text or fields

### Requirement: Auditable agent mutations and receipts
Every agent mutation SHALL require a concise reason and SHALL return a bounded receipt containing collection identity, item identity, operation, before and after item/container hashes, affected canonical paths, and committed outcome. The existing human-readable activity log SHALL record agent mutations using the fields `operation`, `collection_id`, `item_key`, `before_item_hash`, `after_item_hash`, `before_container_hash`, `after_container_hash`, and rationale without copying canonical item values. Direct human edits SHALL create detectable audit gaps rather than reconstructed events. Operational journals and governance receipts SHALL retain their existing distinct roles.

#### Scenario: Successful normal update records one audit event
- **WHEN** a guarded item update commits
- **THEN** the terminal becomes committed only after every planned replacement completes, and the response includes the canonical and matching audit identifiers and hashes

#### Scenario: Failed validation records no committed audit event
- **WHEN** schema validation or a drift guard refuses a mutation
- **THEN** no canonical item change or committed agent-mutation audit entry is written

#### Scenario: Abrupt interruption exposes an audit gap
- **WHEN** a process terminates after canonical replacement but before the activity-log replacement
- **THEN** canonical Records remain truth, report-only inspection detects the hash/audit mismatch, and Exomem does not invent or silently hide a history event

### Requirement: Bounded structured query and stable snapshots
The substrate SHALL support field and relation filters, date ranges, projection, deterministic sort, bounded pagination, count and bounded numeric/categorical aggregates, and grouped observed-state renderings over adapter-produced rows. Every response SHALL identify the collection and canonical source snapshot. Limits SHALL have a positive hard cap; zero or omitted limits SHALL never bypass that cap. A continuation SHALL refuse or restart explicitly when its source snapshot changed.

#### Scenario: Zero limit cannot dump a collection
- **WHEN** a client supplies a zero, negative, omitted, or excessive row limit
- **THEN** the engine applies documented bounded behavior and never returns an unbounded tail

#### Scenario: Changed source invalidates continuation
- **WHEN** a direct edit changes a canonical source between paginated requests
- **THEN** a continuation bound to the prior source snapshot refuses as stale rather than silently skipping or duplicating items

#### Scenario: Aggregate output is bounded
- **WHEN** a distinct, profile, group, or other reduction has high cardinality
- **THEN** the result applies explicit caps and reports truncation instead of returning unbounded values

### Requirement: Provenance-bearing query renderings
Generated tables, observed progress renderings, summaries, and export-shaped query responses SHALL identify their source collection, exact query or saved-view definition, source hashes, generation time, and derived status. They SHALL be returned as bounded query output, SHALL NOT be written as canonical data by `record_memory`, and SHALL NOT promote or migrate storage strategies.

#### Scenario: Progress view is visibly derived
- **WHEN** a client renders a collection progress view
- **THEN** both machine-readable and human-readable output identify the source collection/query/snapshot and `derived: true`

#### Scenario: Derived dataset response is not auto-promoted
- **WHEN** a Markdown-log query is exported as CSV or JSON
- **THEN** the returned content is labelled derived and the manifest continues to name the Markdown log as canonical

### Requirement: Manual-edit visibility and report-only inspection
Queries SHALL read current canonical files so ordinary editor and Obsidian changes become visible without AI mediation. Collection inspection SHALL detect out-of-band source changes, duplicate or missing identities, schema violations, missing templates, audit-history gaps, and stale saved-view provenance without rewriting canonical files. Generic derived-index repair SHALL remain owned by `maintain_memory(mode="reconcile", dry_run=false)`.

#### Scenario: Direct edit appears on next query
- **WHEN** a user adds, changes, or removes a valid item directly in an ordinary editor
- **THEN** the next fresh query reflects that canonical state and reports a changed source snapshot

#### Scenario: Inspect reports but does not repair canonical ambiguity
- **WHEN** manual edits create duplicate item identities or ambiguous legacy keys
- **THEN** `record_memory(action="inspect")` reports the exact issue and leaves canonical files untouched, while `maintain_memory` may repair only derived indexes
