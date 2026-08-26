# structured-collections Specification

## Purpose
TBD - created by archiving change add-first-class-records. Update Purpose after archive.
## Requirements
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
The collection substrate SHALL keep collection mechanics independent from semantic meaning. `records` SHALL mean observed facts or events and `planning` SHALL mean intended future state; either profile SHALL reuse identity, schema, storage adapters, mutation, query, audit-chain, rendering, and direct-edit inspection mechanics through one profile-neutral implementation rather than a fork.

An explicit `records` manifest and every canonical source it declares SHALL be contained by exact portable `Knowledge Base/Records/` path segments. An explicit `planning` manifest and every canonical source it declares SHALL be contained by exact portable `Knowledge Base/Planning/` path segments. These profile-specific placement rules SHALL be checked after symlink-safe resolution and make structured-only recall classification deterministic; they do not constrain ordinary templates or links to governed artifacts elsewhere in the vault.

#### Scenario: Future Planning manifest resolves through the same loader
- **WHEN** a valid manifest declares `semantic_profile: planning`
- **THEN** the generic collection loader inspects its identity, schema, storage, links, templates, and views through the same contracts used for Records without applying Records semantics

#### Scenario: Records operations do not mutate Planning
- **WHEN** a Planning-profile manifest is supplied to the Records query, create, append, or update path
- **THEN** the Records product boundary refuses the operation while leaving the shared substrate and Planning files unchanged

#### Scenario: Planning operations do not mutate Records
- **WHEN** a Records-profile manifest is supplied to the Planning query, create, add, update, or triage path
- **THEN** the Planning product boundary refuses the operation while leaving the shared substrate and Records files unchanged

#### Scenario: Unknown profile does not become Records
- **WHEN** a manifest declares an unsupported semantic profile
- **THEN** the substrate reports that profile as unsupported and does not silently apply Records or Planning semantics

#### Scenario: Records source outside the Records layer refuses
- **WHEN** a Records or Planning manifest or declared canonical source resolves outside the exact layer required for that profile through case, separator, dot-segment, or symlink aliases
- **THEN** validation refuses before reading canonical item contents

### Requirement: Three portable canonical storage strategies
The substrate SHALL support chronological Markdown logs, one Markdown file per item, and CSV, TSV, or JSON datasets as declared canonical storage. Canonical data SHALL remain in those human-owned files; any SQLite state, cache, index, export, summary, or generated view SHALL be derived and rebuildable.

Chronological-log child rows SHALL declare a bounded `container_field` in addition to their delimiter and fields; that container SHALL be a declared array-of-object item-schema field, so adapters do not impose domain field names. Markdown-item reads SHALL accept exactly one leading UTF-8 BOM for frontmatter parsing while retaining the complete original byte sequence for source hashes, spans, body/newline behavior, and guarded-write preservation. A Markdown-item container snapshot SHALL cover the marked manifest plus the complete bounded recursive source inventory as ordered `(path, kind, exact-byte hash-or-null)` entries, including the canonical source directory, nested empty directories, unexpected regular files, and all item files; a missing source and an empty source SHALL differ.

#### Scenario: Chronological Markdown stays canonical
- **WHEN** a log-backed collection is queried or safely mutated
- **THEN** the declared Markdown log remains the sole canonical item history and no generated dataset is promoted implicitly

#### Scenario: File-per-item collection uses ordinary properties
- **WHEN** a Markdown-item collection stores a record
- **THEN** the record is an ordinary Markdown file with stable item and collection identifiers plus typed YAML properties and an optional readable body

#### Scenario: Recursive inventory drift invalidates mutation snapshot
- **WHEN** a direct edit adds, removes, or changes a nested file or empty directory after a caller read a Markdown-item collection
- **THEN** the collection snapshot changes and the commit-bound recursive census refuses a mutation carrying the prior container hash, including when the intended item is deeper in the same ancestor tree

#### Scenario: Dataset stays directly editable
- **WHEN** a dataset-backed collection uses CSV, TSV, or JSON
- **THEN** its rows remain readable and editable with ordinary tools, Exomem can query them without an opaque database, and this delivery refuses dataset append/update rather than silently reserializing the file

### Requirement: Collection-scoped item identity and exact source versioning
Every safely mutable item SHALL expose an identity tuple `(collection_uuid, canonical_item_key)` and an item version derived from its exact current source bytes. New agent-authored Markdown items SHALL receive an explicit UUID item key through the active semantic profile's declared ID property and marker contract. Query-only datasets MAY expose a bounded manifest-declared string key, but arbitrary dataset keys SHALL NOT be treated as global Exomem IDs. Standalone Records references SHALL retain `exomem://record/<collection-uuid>/<percent-encoded-key>`; standalone Planning references SHALL use `exomem://plan/<collection-uuid>/<percent-encoded-key>`. A reference parser SHALL require the namespace to match the selected profile.

#### Scenario: Explicit identity survives a non-semantic edit
- **WHEN** a user changes an item field, title, body, or path without changing its explicit item identifier
- **THEN** the item identifier and profile-specific reference remain stable and its item version changes

#### Scenario: Legacy deterministic identity remains queryable
- **WHEN** a legacy Markdown Record block has no explicit item identifier but its declared natural key is unique
- **THEN** the adapter serializes schema version plus natural-key fields in declared order using Unicode-NFC strings, normalized ISO dates/datetimes, explicit JSON nulls, and typed JSON scalars, then returns a deterministic collection-scoped compatibility key marked as inferred

#### Scenario: Corrected inferred natural key can change compatibility identity
- **WHEN** a user corrects a natural-key field on an unmarked legacy Record item
- **THEN** the inferred compatibility key may change and Exomem does not claim it is a durable substitute for an explicit item key

#### Scenario: Duplicate legacy natural key refuses update
- **WHEN** two authorized items in one collection resolve to the same canonical item key
- **THEN** both remain inspectable but targeted update or triage refuses with an ambiguity error and names no arbitrary winner

#### Scenario: Namespace mismatch refuses
- **WHEN** a Planning operation receives an `exomem://record/...` item reference or a Records operation receives an `exomem://plan/...` item reference
- **THEN** resolution refuses the profile mismatch without searching by the encoded key alone

### Requirement: Minimal typed item schemas
Collection schemas SHALL support required and optional open-vocabulary fields with bounded primitive types, enums, arrays, objects, date or datetime values, units metadata, and link fields, except that a schema SHALL NOT declare a schema-excluded frontmatter field name. The substrate SHALL impose only identity and schema-version mechanics universally; occurred time, status, units, provenance, relations, uncertainty, reconstruction, and lifecycle SHALL be present only when the collection schema makes them meaningful, and uncertainty SHALL NOT be expressed as a numeric confidence field because the schema-excluded set forbids that name.

#### Scenario: Domain fields validate before publication
- **WHEN** an item violates a required field, type, enum, identifier, or declared unit constraint
- **THEN** append or update refuses before any canonical file or history entry is published

#### Scenario: Optional schema inference is advisory
- **WHEN** Exomem infers a candidate schema from existing human-authored items
- **THEN** it returns a bounded proposal with provenance and does not make that proposal binding or rewrite historical items without explicit adoption

#### Scenario: A schema-excluded field name cannot be declared
- **WHEN** a manifest declares a schema-excluded name among its item schema fields or as its Markdown-log note field
- **THEN** collection create and revise refuse before any bytes are published, and the projected manifest authoring contract discloses the excluded names so a client authoring from the contract alone never has to guess one

#### Scenario: A schema declaring one before this contract stays parseable
- **WHEN** a manifest written before this contract declares a schema-excluded name
- **THEN** parsing, loading, discovery, resolution, and query continue to behave exactly as before, because the refusal lives at the write entry points and never in the parser

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

Every agent mutation SHALL require a concise reason and SHALL return a bounded receipt containing collection identity, item identity (null for create and lifecycle operations), operation-specific required before and after item/manifest/container hashes, affected canonical paths, committed outcome, and a transition correlation. An ordinary manifest SHALL carry the system-owned YAML mapping `record_audit: {version: <reader-version>, head: <transition-id>}` in either ordinary flow or block style. Version 1 remains valid for collections containing only create/append/update history. The first `revise` or `rebaseline` SHALL atomically set the manifest marker to version 2, and every later append, update, revise, or rebaseline SHALL preserve version 2. Every agent-touched canonical block/item SHALL carry exactly its latest content-free transition ID.

The existing activity log SHALL contain one strict versioned machine-parseable event per transition with its ID, predecessor, operation, collection/manifest/source/item correlation, before/after manifest/item/container hashes, replay payload hash where relevant, and sanitized rationale, without copying canonical item values. Create/append/update events remain closed version 1 events; revise/rebaseline are closed version 2 events. A reachable chain MAY therefore contain a version 1 prefix, a version 2 lifecycle transition, and later version 1 append/update transitions (`v1 -> v2 -> v1`) while the manifest reader marker remains version 2. A reader supporting marker version 2 SHALL dispatch each event by its own version, traverse the complete predecessor chain, and preserve any earlier rebaseline discontinuity through later mutations and process restarts. Create and lifecycle events SHALL have a null item key; create names the declared source, lifecycle events name the manifest as their canonical path, and append/update events SHALL have a normalized item UUID and a canonical item path valid for the declared storage strategy.

Inspection SHALL validate those rules for every reachable transition, reconstruct the predecessor chain from the manifest head, deduplicate exact events, refuse forks/conflicts as gaps, and distinguish `baseline`, `ok`, positive `gap`, bounded `history_incomplete`, and `acknowledged_gap`; direct human edits remain visible across later successful mutations. It SHALL use bounded descriptor-bound no-follow regular-file reads for the manifest, canonical marker sources, and activity history, bind markers to the same snapshot being inspected, cap markers, and SHALL never repair or invent history. Operational journals and governance receipts SHALL retain their existing distinct roles.

#### Scenario: Successful normal update records one audit event
- **WHEN** a guarded item update commits
- **THEN** the terminal becomes committed only after every planned replacement completes, and the response includes the canonical and matching audit identifiers and hashes

#### Scenario: Failed validation records no committed audit event
- **WHEN** schema validation or a drift guard refuses a mutation
- **THEN** no canonical item change or committed agent-mutation audit entry is written

#### Scenario: Abrupt interruption exposes an audit gap
- **WHEN** a process terminates after canonical replacement but before the activity-log replacement
- **THEN** canonical Records remain truth, report-only inspection detects the hash/audit mismatch, and Exomem does not invent or silently hide a history event

#### Scenario: Pre-publication interruption leaves no canonical-looking scaffold
- **WHEN** a simulated `BaseException` interrupts staging before the first canonical replacement during collection creation
- **THEN** Exomem removes its empty created directories and batch workspaces, rethrows the original exception without normalization, and an exact retry is not wedged by tool-owned residue

#### Scenario: Human audit-mapping reformat remains mutable
- **WHEN** a user reformats a valid manifest `record_audit` flow mapping into an equivalent YAML block mapping
- **THEN** the next mutation replaces that complete mapping node while preserving unrelated manifest bytes and produces valid YAML

#### Scenario: Normal mutation after revision preserves the reader floor
- **WHEN** a collection emits a version 2 revise transition and later commits a version 1 append or update event
- **THEN** the manifest retains `record_audit.version: 2`, the mixed event chain validates, and restart inspection reports the same healthy history

#### Scenario: Normal mutation after rebaseline preserves discontinuity
- **WHEN** a collection emits a version 2 rebaseline transition and later commits a version 1 append or update event
- **THEN** the manifest retains `record_audit.version: 2`, and inspection before and after restart reports `acknowledged_gap` with the same permanent discontinuity

#### Scenario: Successful Planning triage uses the shared audit engine
- **WHEN** a guarded Planning triage mutation commits
- **THEN** the response, `plan_audit` head, Planning item marker, and activity event carry one matching transition without any `record_audit` property

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

#### Scenario: Inspect reports an undeclared manual field
- **WHEN** a human adds a property that is not declared by the collection schema to an otherwise readable Record item
- **THEN** inspection reports a schema violation without dropping, rewriting, or silently adopting the property

### Requirement: The binding manifest contract is machine-discoverable
The collection substrate SHALL project a deterministic versioned manifest JSON Schema derived from the same constants and constraints enforced by the binding parser. The projection SHALL distinguish closed enums from open strings, include required fields and nested field grammar, and contain canonical minimal and complete examples that parse successfully.

#### Scenario: Closed enums stay aligned with validation
- **WHEN** supported profiles, collection versions, storage strategies, storage format versions, field types, saved-view operators, or aggregates change
- **THEN** the described contract and actionable validation details expose the same values
- **AND** parity tests fail if parser and contract diverge

#### Scenario: Lifecycle is described honestly
- **WHEN** the contract describes `lifecycle`
- **THEN** it identifies a required non-empty string constraint and an `active` example
- **AND** it does not invent a closed lifecycle enum that the parser does not enforce

### Requirement: Closed manifest validation failures are self-remediating
Manifest validation errors SHALL preserve their stable code and message while adding bounded machine-readable remediation facts. Closed enum errors SHALL name the exact field, received value, allowed values, and a minimal example. Missing required fields SHALL name the field, expected shape, and minimal example.

#### Scenario: Unsupported profile returns the allowed profiles
- **WHEN** a proposed manifest declares an unsupported semantic profile
- **THEN** the error retains `UNSUPPORTED_COLLECTION_PROFILE`
- **AND** it identifies `semantic_profile`, the received value, the complete allowed set, and `semantic_profile: records` as an example

#### Scenario: Missing collection version names the exact field
- **WHEN** a proposed manifest omits `collection_version`
- **THEN** the error retains `UNSUPPORTED_COLLECTION_VERSION`
- **AND** it identifies the missing field, allowed versions, and `collection_version: 1` as an example

### Requirement: Records Markdown items may declare managed readable presentation

A Records Markdown-item manifest MAY declare one closed, versioned, bounded `record_presentation` recipe. This newly reserved namespaced key SHALL NOT reinterpret unrelated legacy `presentation` extension data. Version 1 SHALL support bounded summary descriptors and bounded `table`, `notes`, and `details` sections. Parent references SHALL bind declared item-schema fields. Each table column SHALL declare a unique child field, optional label, exact scalar projection type, and optional link kind only for links.

The table contract SHALL be a derived render/egress allowlist, not another canonical schema. Undeclared child keys remain canonical but SHALL NOT enter managed tables or governed queries. Planning, other storage strategies, unknown keys/versions/kinds/types, duplicate fields, incompatible parent references, or excessive recipes SHALL refuse. YAML frontmatter remains sole canonical structured authority; presentation SHALL never be parsed back or used as a query source.

#### Scenario: Nested item gains a generic readable recipe
- **WHEN** a Records Markdown-item manifest declares summary, typed table projection, notes, and details
- **THEN** validation returns a normalized domain-neutral contract

#### Scenario: Standalone YAML and child files are not introduced
- **WHEN** a collection opts in
- **THEN** each parent remains one ordinary Markdown file without sibling YAML or per-child files

#### Scenario: Open child object requires explicit safe projection
- **WHEN** a legacy array has arbitrary objects but no table projection
- **THEN** its canonical values remain readable while managed rendering and Markdown-item expansion refuse

#### Scenario: Legacy extension key remains opaque
- **WHEN** an existing manifest has custom `presentation` data but no `record_presentation`
- **THEN** it retains prior parsing/mutation behavior and the custom data is not reinterpreted

#### Scenario: Invalid presentation refuses before mutation
- **WHEN** a recipe names an absent parent, scalar table, invalid/duplicate child column, or exceeds bounds
- **THEN** validation refuses content-safely and changes nothing

### Requirement: Managed presentation preserves authored Markdown and exact authority

An item opted in through valid `record_presentation` or `item_presentation` SHALL contain at most one bounded managed block with exact versioned markers and SHA-256 over canonical JSON of recipe identity plus selected canonical values. Rendering SHALL be deterministic, escaped, inference-free, and labelled generated. Persisted link columns SHALL serialize canonical literals independent of audience policy; whole-file authorization controls access to stored bytes.

Append/value update SHALL render in the guarded audited item batch. Guarded Records update MAY use `refresh_presentation: true` with empty changes, current item/container guards, and `why`. Reads, query, inspect, rebaseline, and reconcile SHALL NOT render. A guarded manifest revision that removes or replaces a presentation recipe SHALL transactionally remove or replace every owned block or refuse. A guarded `maintain_memory(mode="structured-files")` apply SHALL render only the exact blocks in its current preview plan.

Rendering SHALL source-splice exact bytes and preserve BOM, CRLF/LF, body separator/leading blanks, marker-like ordinary prose, and final-newline state. Duplicate/nested/malformed/oversized exact markers SHALL refuse every rendering mutation.

Semantic append replay SHALL hash canonical values plus exact authored body with one valid managed block removed. Renderer bytes/version SHALL not change semantic request identity; malformed/tampered blocks SHALL not qualify. Receipts/item versions still bind exact complete bytes.

#### Scenario: New nested Record is readable
- **WHEN** an opted-in item is appended with nested values and prose
- **THEN** it contains unchanged frontmatter, deterministic readable sections, and byte-preserved prose

#### Scenario: Existing item is backfilled audibly
- **WHEN** a missing block is refreshed with current guards and reason
- **THEN** canonical values remain equal and one audit receipt binds exact new hashes

#### Scenario: Direct YAML edit remains authoritative
- **WHEN** a human changes a selected canonical value without rendering
- **THEN** reads/query use YAML, inspect reports stale, and no read rewrites it

#### Scenario: Ambiguous markers are never guessed
- **WHEN** exact markers are duplicate, nested, malformed, or oversized
- **THEN** inspect reports malformed and rendering refuses

#### Scenario: Exact append replay ignores only valid renderer bytes
- **WHEN** committed append retries identical values/authored body after rendering
- **THEN** it replays without conflict; malformed/tampered markers receive no replay treatment

#### Scenario: Legacy byte shapes survive rendering
- **WHEN** rendering targets BOM, CRLF, blank-body, leading-blank, marker-like-prose, or no-final-newline vectors
- **THEN** exact bytes outside the managed span preserve those properties

#### Scenario: Policy does not rewrite stored presentation
- **WHEN** link release policy changes without canonical byte changes
- **THEN** expected managed bytes/item hash remain unchanged while query egress follows current policy

#### Scenario: Recipe removal cleans every owned block atomically
- **WHEN** a guarded complete manifest revision removes a presentation recipe from a collection whose items contain valid owned blocks
- **THEN** the manifest and exact block removals publish together or no file changes

#### Scenario: Migration renders only the approved plan
- **WHEN** structured-files apply carries the current plan identity and unchanged source snapshot
- **THEN** it writes only the previewed presentation transformations under one terminal receipt

### Requirement: Inspection validates presentation without trusting it

Inspection SHALL recompute expected bytes from the authorized canonical snapshot without reverse-parsing display. It SHALL return only non-current released items, sorted by item key, with total state counts and truncation metadata. Each entry SHALL contain item key, path, current version, and `missing|stale|malformed|unrenderable`. Type mismatch SHALL be `unrenderable`, MAY name content-safe table/column/child index, SHALL refuse refresh, and requires guarded canonical value update. Presentation-only findings SHALL retain valid lifecycle guards. Mixed-release authorization SHALL precede reading/reflection. Unselected-field edits need not stale the block.

#### Scenario: Hand-edited table cannot override YAML
- **WHEN** displayed value changes without frontmatter
- **THEN** query returns frontmatter and inspect reports mismatch

#### Scenario: Withheld item leaks no presentation fact
- **WHEN** policy withholds an item
- **THEN** inspect returns no item/path/version/marker/row fact for it

#### Scenario: Direct-edit repair remains two audited steps
- **WHEN** direct selected-value edit creates audit gap and stale block
- **THEN** rebaseline acknowledges canonical history without item rewrites and later guarded refresh repairs presentation

#### Scenario: Current items cannot crowd out broken items
- **WHEN** more items exist than the repair cap and only a late item is stale
- **THEN** current entries are omitted, stale is returned deterministically, and counts/truncation are honest

#### Scenario: Type drift has an honest remedy
- **WHEN** a child value no longer satisfies its declared projection type
- **THEN** inspect says `unrenderable`, refresh refuses, and guarded value update is required

### Requirement: Manifest acceptance is eager and path-independent

Every public path that accepts or loads a collection manifest SHALL use one complete manifest-validation contract. Validation SHALL resolve and normalize every saved view against the declared schema, including omitted optional filters, before reporting success. `validate`, guarded `create`, ordinary load, `inspect`, revision, and saved-view query SHALL NOT apply progressively stricter manifest rules. Repeated validation layers SHALL deduplicate equivalent diagnostics by stable code and location. Create-mode validation SHALL authorize the candidate manifest path before parsing supplied identity bytes, and every path SHALL project validation diagnostics through the ordinary L6 boundary.

#### Scenario: Saved view without filters survives the lifecycle
- **WHEN** a proposed manifest contains a saved view with columns and sort but omits optional filters
- **THEN** validation normalizes filters to an empty list and the unchanged manifest succeeds through create, inspect, and saved-view query

#### Scenario: Invalid saved view is rejected before create
- **WHEN** a proposed saved view references an unknown field or invalid query shape
- **THEN** read-only validation returns one actionable `INVALID_SAVED_VIEW` diagnostic and create performs no mutation

#### Scenario: Candidate path is denied before identity parsing
- **WHEN** create-mode validation supplies manifest text for a candidate path the caller may not govern
- **THEN** validation refuses before parsing or reflecting collection identity and returns no content-derived diagnostic

### Requirement: Existing manifests support guarded audited revision

The collection substrate SHALL provide a Records-native revision operation over complete proposed manifest text. Revision SHALL require a collection selector, `expected_manifest_hash`, `expected_container_hash`, and concise `why`. Inside the same-vault mutation boundary it SHALL authorize and re-read current state, validate the complete proposed manifest and every current item, recheck the guards, preserve collection identity, semantic profile, canonical source, and storage strategy, and atomically publish the manifest plus one content-free Records lifecycle event/receipt version 2. Existing Records append/update event and receipt version 1 bytes remain closed and unchanged. The revise event SHALL use `operation: revise`, a null item identity and item hashes, the manifest as canonical path, before/after manifest and container hashes, and sanitized rationale.

The proposed revision manifest MAY omit the system-owned `record_audit` mapping. If supplied, it SHALL exactly match the current marker. The system, not the caller, SHALL derive and atomically write the next `record_audit.version` and `record_audit.head` with the event and receipt.

Lifecycle event v2 SHALL contain exactly `version`, `transition_id`, `parent_id`, `operation`, `collection_id`, `manifest_path`, `source_path`, `canonical_path`, `item_key`, `before_manifest_hash`, `after_manifest_hash`, `before_item_hash`, `after_item_hash`, `before_container_hash`, `after_container_hash`, `payload_hash`, `rationale`, `continuity`, `acknowledged_gap_codes`, `gap_fingerprint`, `checkpoint_snapshot_hash`, and `minimum_reader_version`. Lifecycle receipt v2 SHALL contain exactly `_record_receipt`, `receipt_version`, `operation`, `collection_id`, `item_key`, `before_item_hash`, `after_item_hash`, `before_manifest_hash`, `after_manifest_hash`, `before_container_hash`, `after_container_hash`, `affected_paths`, `payload_hash`, `outcome`, `audit_correlation`, `continuity`, `acknowledged_gap_codes`, `gap_fingerprint`, `checkpoint_snapshot_hash`, and `minimum_reader_version`. Item identity/hashes are null for both operations. Revise requires continuity true, empty codes, null fingerprints, one manifest affected path, and `outcome: committed`. Compact/full terminal and L6 receipt projectors SHALL recognize only that closed shape. Exact request replay SHALL reuse the byte-identical stored committed receipt; only its enclosing mutation terminal SHALL report `status: replayed` and `mutated: false`.

Revision MAY repair invalid optional manifest contract details when the exact current bytes, stable collection identity, and continuous audit chain can be proven. It SHALL refuse direct-edit hash gaps, representation migration, schema coercion, unauthorized artifacts, ambiguous identity, audit forks, invalid canonical items, or stale guards. No failed revision SHALL change the manifest, audit head, canonical items, or activity history.

The governed selector SHALL authorize the current manifest path before Exomem opens or parses its bytes, with withheld and absent collections projected identically. Revision SHALL authorize the current declared source and every canonical item before compatibility validation, then authorize every path admitted by the proposed manifest before publication. If any artifact is withheld, the entire operation SHALL refuse without releasing its path, value, hash, count, identity, or gap diagnostics. Errors and receipts SHALL pass the ordinary L6 response projector.

#### Scenario: Valid view correction advances audit
- **WHEN** a caller validates and revises one saved view using current manifest/container guards
- **THEN** the new manifest is inspectable, all existing items remain valid and byte-identical, and one `revise` transition records the before/after manifest and container hashes

#### Scenario: Revision refuses incompatible schema
- **WHEN** a proposed schema would make any current canonical item invalid
- **THEN** revision refuses before publication and leaves all canonical bytes and audit history unchanged

#### Scenario: Revision cannot migrate representation
- **WHEN** a proposed manifest changes collection identity, semantic profile, canonical source, or storage strategy
- **THEN** revision refuses and directs representation migration to a separately specified workflow

#### Scenario: Withheld manifest is indistinguishable from missing
- **WHEN** a caller selects a collection whose manifest is not releasable to that audience and purpose
- **THEN** revision refuses before opening or parsing the manifest and returns the same projected result as an absent selector

#### Scenario: Mixed-release collection reveals no diagnostic details
- **WHEN** the manifest is releasable but one canonical item or proposed reference is withheld
- **THEN** revision refuses before publication and does not reveal which artifact, value, hash, count, or validation result caused the refusal

### Requirement: Valid out-of-band edits can be explicitly rebaselined

The collection substrate SHALL provide an explicit `rebaseline` mutation for structurally valid current canonical state whose audit hashes differ because of out-of-band edits. Rebaseline SHALL require `expected_manifest_hash`, `expected_container_hash`, the exact inspect-reported `acknowledged_gap_codes`, and concise `why`. It SHALL revalidate the complete collection, recheck the acknowledgement and guards under the mutation boundary, write no item content, and atomically publish a content-free checkpoint transition plus the system-derived new manifest audit head.

Rebaseline SHALL append a Records lifecycle event/receipt version 2 with `operation: rebaseline`, the prior head, `continuity: false`, sorted exact acknowledged gap codes, a deterministic `gap_fingerprint` over canonical JSON containing the prior head, codes, and guarded before-manifest/container hashes, a `checkpoint_snapshot_hash` over canonical JSON containing the sorted authorized pre-checkpoint manifest/item paths and hashes, before/after manifest and container hashes, and sanitized rationale. It SHALL copy no item values. Existing event/receipt version 1 remains closed and unchanged. Rebaseline receipt v2 requires non-empty codes, both fingerprints, continuity false, one manifest affected path, and `outcome: committed`.

For both lifecycle operations, `payload_hash` is SHA-256 over `exomem-record-lifecycle-request:v2\0` plus canonical JSON `{action, collection_id, before_manifest_hash, before_container_hash, proposed_manifest_hash, acknowledged_gap_codes, rationale}`. Gap fingerprints use `exomem-record-gap:v2\0`; checkpoint fingerprints use `exomem-record-checkpoint:v2\0`. Canonical JSON is UTF-8 with sorted object keys, no whitespace, and `ensure_ascii=false`. Transition IDs are independent 24-hex values. Fingerprint inputs exclude event bytes, transition ID, after-manifest hash, receipt, and terminal.

Inspection after rebaseline SHALL report audit status `acknowledged_gap`, never `ok`, while separately reporting the checkpoint snapshot as structurally valid and trusted from that checkpoint forward. Inspect, query history, and agent history SHALL preserve a bounded permanent discontinuity containing `provenance_continuity: false`, the prior head, acknowledged gap codes, rationale, checkpoint transition, and both fingerprints. Later valid mutations SHALL extend the checkpoint chain without erasing or relabelling that history. Rebaseline SHALL use the same authorize-before-read, complete-artifact admission, and L6 diagnostic projection rules as revision. It SHALL refuse schema violations, duplicate/ambiguous identity, malformed or forked audit history, unauthorized artifacts, stale guards, or acknowledgements that do not exactly match current gaps. It SHALL NOT invent missing item transitions.

#### Scenario: Direct manifest edit becomes an acknowledged checkpoint
- **WHEN** a human makes a valid manifest edit, inspect reports current manifest/container mismatch, and the caller rebaselines those exact gaps with current guards
- **THEN** no item content changes, current audit status becomes `acknowledged_gap`, and inspect/query/history expose the permanent discontinuity and `provenance_continuity: false`

#### Scenario: Rebaseline cannot bless invalid data
- **WHEN** inspect also reports a schema violation or duplicate item identity
- **THEN** rebaseline refuses and leaves the current canonical files and audit gap unchanged

#### Scenario: Gap drift invalidates acknowledgement
- **WHEN** canonical state changes after inspect but before rebaseline
- **THEN** the expected hashes or exact gap acknowledgement fail closed and no checkpoint is written

#### Scenario: Hidden gap cannot be acknowledged by inference
- **WHEN** the caller cannot receive every artifact and exact gap diagnostic required for the checkpoint
- **THEN** rebaseline refuses without revealing or accepting guessed gap codes

### Requirement: New lifecycle history establishes a minimum reader floor

The release SHALL establish Records reader contract version 2 before enabling lifecycle mutation. The first `revise` or `rebaseline` SHALL atomically change the existing manifest `record_audit.version` from 1 to 2 with the v2 event/head; that mapping is the durable per-collection minimum-reader marker. Every later append/update SHALL preserve the version 2 marker while continuing to emit the closed version 1 event and receipt bytes, so mixed `v1 -> v2 -> v1` history is valid. A v2 reader SHALL dispatch events by their own version, accept v1 history plus the closed v2 lifecycle events, scan the entire reachable chain, and preserve `acknowledged_gap` through later mutations and restarts. An additive new deployment-lock version/schema SHALL record/enforce `minimum_records_reader_version: 2`; the closed existing deployment-lock v2 shape SHALL remain unchanged. Supported rollback SHALL retain the v2 reader and its status semantics while disabling `revise` and `rebaseline`; it SHALL never deploy the predecessor v1 reader. The immediately preceding reader SHALL fail closed on the v2 manifest without rewriting it and SHALL NOT report its audit healthy.

#### Scenario: Rollback after first lifecycle transition preserves the reader
- **WHEN** deployment is rolled back after a collection has emitted `revise` or `rebaseline`
- **THEN** the old mutation selectors may be disabled but the compatible audit reader remains deployed and preserves the discontinuity

#### Scenario: Predecessor reader cannot bless unknown history
- **WHEN** the immediately preceding release reads a compatibility fixture containing the new transition
- **THEN** it refuses the v2 manifest/audited view
- **AND** it neither rewrites the collection nor reports audit status `ok`

#### Scenario: Deployment refuses a reader below the floor
- **WHEN** a hosted deployment or rollback candidate reports Records reader contract version 1 after the floor is established
- **THEN** readiness and promotion refuse it before serving the vault

#### Scenario: Append after revision keeps the upgraded marker
- **WHEN** append or update commits after a collection's first revise transition
- **THEN** the version 1 item event extends the chain without downgrading `record_audit.version: 2`
- **AND** restart inspection accepts the mixed history

#### Scenario: Append after rebaseline keeps the acknowledged gap
- **WHEN** append or update commits after a collection's first rebaseline transition
- **THEN** restart inspection still reports `acknowledged_gap` and the original discontinuity rather than `ok`

### Requirement: Lifecycle digests have shared deterministic vectors

The implementation SHALL preserve shared canonical vectors for the three v2 digest domains so independent clients and future readers cannot silently change serialization.

#### Scenario: Gap fingerprint vector
- **WHEN** the canonical input is `{"acknowledged_gap_codes":["current-container-mismatch","current-manifest-mismatch"],"before_container_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","before_manifest_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","prior_head":"0123456789abcdef01234567"}`
- **THEN** the `exomem-record-gap:v2\0` digest is `e96fe3ac9d4704d04c6d583795c68e9ac544f3be4061641b3c6d61aeb81a3c2e`

#### Scenario: Checkpoint snapshot vector
- **WHEN** the sorted path/hash input is `[["Knowledge Base/Records/Test/Items/item.md","cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"],["Knowledge Base/Records/Test/_collection.md","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]]`
- **THEN** the `exomem-record-checkpoint:v2\0` digest is `de55aff9ce1c3a75dbd045461fd2b9a95a415cb509a0c67d671e6ad42b24e478`

#### Scenario: Lifecycle request vector
- **WHEN** the canonical request is `{"acknowledged_gap_codes":["current-container-mismatch","current-manifest-mismatch"],"action":"rebaseline","before_container_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","before_manifest_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","collection_id":"11111111-1111-4111-8111-111111111111","proposed_manifest_hash":null,"rationale":"Acknowledge direct edit"}`
- **THEN** the `exomem-record-lifecycle-request:v2\0` digest is `349c5a30baf4922922c42512efbfee05607c18888e27ccd39a37deefd9358f01`

### Requirement: Structured manifests may declare shared item representation recipes

A versioned Markdown-item collection manifest SHALL accept optional `item_filename` and `item_presentation` recipes defined by the human-owned structured-files capability. Manifest validation SHALL eagerly validate recipe versions, field existence, field eligibility, renderer compatibility, path safety, managed-marker uniqueness, and worst-case bounded output without reading items outside the declared collection source.

#### Scenario: Invalid recipe fails before collection publication

- **WHEN** create or revision validation receives a recipe with an unknown version, absent field, mutable filename field, unsafe path projection, or unbounded presentation
- **THEN** validation refuses with exact recipe diagnostics and writes no manifest, item, template, or audit file

#### Scenario: Recipe is optional and path-independent

- **WHEN** a compatible existing collection declares neither shared recipe
- **THEN** it remains valid and retains its current paths and body behaviour until explicitly revised

### Requirement: Presentation ownership survives recipe removal and conversion

The substrate SHALL recognize managed presentation markers independently of the currently active recipe. Removing or replacing a recipe SHALL either transactionally remove or replace every owned managed block as part of the guarded manifest revision, or SHALL refuse the revision. It SHALL NOT publish a manifest state that leaves a managed block with no active owner.

#### Scenario: Recipe removal cannot orphan generated blocks

- **WHEN** a complete manifest revision removes its presentation recipe while current items contain owned blocks
- **THEN** the revision either includes their exact transactional cleanup or refuses before changing the manifest

#### Scenario: Conversion preserves authored Markdown

- **WHEN** a collection converts from a compatible profile-specific recipe to `item_presentation`
- **THEN** each old owned block is replaced by the new deterministic block in the same batch and Markdown outside the markers remains byte-identical

### Requirement: Inspection covers filenames and all managed presentation state

Targeted collection inspection SHALL scan canonical item paths and recognized managed markers even when the active manifest declares no representation recipe. It SHALL report bounded diagnostics for filename drift, projected path collisions, missing presentation, stale recipe digest, stale item version, orphan managed block, unrenderable selected value, unresolved relationship presentation, and authored changes inside managed authority. Inspection SHALL remain report-only.

#### Scenario: Orphan block is unhealthy without an active recipe

- **WHEN** an item contains a recognized managed block but its manifest declares no owning recipe
- **THEN** inspection reports `orphan_presentation` rather than declaring the collection healthy

#### Scenario: Healthy collection proves representation agreement

- **WHEN** manifest, items, audit, filenames, managed markers, and rendered content agree
- **THEN** inspection returns no representation diagnostics without rewriting any file

### Requirement: Representation transformations use current canonical collection mechanics

Preview and apply SHALL reuse collection discovery, profile validation, source snapshots, same-vault writer serialization, idempotency, audit publication, stable reference resolution, and governance projection. They SHALL NOT create a second item index or treat rendered Markdown as canonical input.

#### Scenario: Manual canonical edit appears in the next plan

- **WHEN** a human validly edits an item between two read-only representation previews
- **THEN** the second plan derives from the new canonical hash and receives a different source snapshot or plan identity

#### Scenario: Apply shares existing writer protection

- **WHEN** representation apply races another mutation in the same vault
- **THEN** the existing writer boundary serializes them and prevents torn file moves or presentation bytes
