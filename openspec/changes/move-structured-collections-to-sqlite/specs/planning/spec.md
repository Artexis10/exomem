## ADDED Requirements

### Requirement: Planning is a built-in collection type and plan_memory is its typed facade
`planning` SHALL be a built-in collection type of kind `intended`, placement `Planning`, carrying the core Planning fields, lifecycle vocabulary, six horizon views and the product-owned `planning.hierarchy.v1` validator, with default audience `policy`. `plan_memory` SHALL be a typed facade over the generic collection operations: `add` to `add`, `update` to `update`, `triage` to `transition` with its allowed field changes, and `inspect`, `query`, `create`, `validate`, `revise` and `rebaseline` to their generic operations. Its arguments, `plan_id` naming, `_plan_receipt` receipts, `STALE_PLAN_ITEM`, `STALE_PLAN_CONTAINER` and `PLAN_ID_CONFLICT` codes, and exact inspection shape SHALL be unchanged. It SHALL refuse collections of any other type.

#### Scenario: Planning wire is unchanged by the facade
- **WHEN** add, update, triage and inspect are issued against a Planning collection before and after the facade is introduced
- **THEN** arguments, receipts, error codes and the exact inspect key set are identical

#### Scenario: Hierarchy rules run as a named validator
- **WHEN** a work item is added with a parent that is not an initiative
- **THEN** the `planning.hierarchy.v1` validator refuses it with the same Planning refusal as today

#### Scenario: plan_memory refuses a declared type
- **WHEN** a collection of a declared type is supplied to `plan_memory`
- **THEN** it refuses as an unsupported collection for the Planning facade, and nothing changes

## MODIFIED Requirements

### Requirement: Human-owned Planning collections
An explicit Planning collection SHALL have a `_collection.md` manifest view under exact portable `Knowledge Base/Planning/**` path segments and SHALL declare `semantic_profile: planning`. Its storage strategy SHALL be `markdown-items` under the same exact layer. Its manifest, items and audit SHALL be canonical in the vault's collection store, and each item SHALL be rendered as a human-readable UTF-8 Markdown view with typed YAML properties and an optional readable body. No index, replica, history page or view SHALL become canonical. Edits a user makes to Planning views SHALL reach the store only through the governed edited-view write path.

#### Scenario: User works without Exomem or a plugin
- **WHEN** a user opens or edits a Planning item view with an ordinary editor
- **THEN** the intent remains understandable without an agent, Obsidian, plugin or database tool, and a valid edit becomes a governed Planning update

#### Scenario: Unsupported Planning storage refuses
- **WHEN** a Planning manifest declares chronological-log or dataset storage in this delivery
- **THEN** Planning validation and mutation refuse it without changing the store or any view

#### Scenario: Planning path cannot bypass structured policy
- **WHEN** a Planning manifest or source path resolves outside exact `Knowledge Base/Planning/` path segments through case, separator, dot-segment, or symlink aliases
- **THEN** validation refuses before any item contents are read

### Requirement: Guarded Planning mutation and audit
Planning `create`, `add`, `update`, and `triage` SHALL require a non-empty single-line reason of at most 512 UTF-8 bytes, run in one collection-store transaction under same-vault writer serialization, validate the final schema and hierarchy before commit, honor exact container and item guards, and return the exact receipt below. A caught error or process interruption SHALL roll the whole transaction back, leaving neither the change nor its transition. Each committed mutation SHALL be exactly one audit transition in the store with a 24-lowercase-hex transition identifier. No `plan_audit` manifest property or item audit comment SHALL be written. An exact add retry with the same plan ID and normalized payload SHALL be idempotent; reusing the ID with different content SHALL refuse. Missing IDs SHALL never fall back to fuzzy title or body matching.

For update, each non-null `changes` value SHALL replace one authored top-level property. Null SHALL delete only optional `health`, `window_start`, `window_end`, `area`, `parent`, `progress_evidence`, `execution`, `tags`, or a manifest-declared optional domain field. Null SHALL refuse for required core fields, required domain fields, and all system/audit fields. The complete resulting item SHALL validate before commit; deleting a date, relationship, or optional domain field is not represented by persisting a null value.

Every successful mutation or exact add replay SHALL return exactly `_plan_receipt: "exomem.planning-mutation"`, `receipt_version: 1`, `operation`, `collection_id`, `plan_id` (null for create), `before_item_hash`, `after_item_hash`, `before_container_hash`, `after_container_hash`, `affected_paths`, `payload_hash`, `outcome`, and `audit_correlation`. Hashes are lowercase SHA-256 or null as appropriate; operation is `create`, `add`, `update`, or `triage`; outcome is `committed` or `replayed`; transition correlation is 24 lowercase hex. Planning transitions SHALL record the Planning operation names `plan_create`, `plan_add`, `plan_update`, or `plan_triage`. Create transitions SHALL have a null item key; add, update and triage transitions SHALL carry one effect naming the normalized item UUID.

#### Scenario: Exact add retry creates one item
- **WHEN** a client retries one add with the same identity and payload
- **THEN** Planning returns the existing committed item without adding a duplicate

#### Scenario: Stale update preserves direct edit
- **WHEN** a user's view edit committed before an agent update carrying prior hashes
- **THEN** the agent update refuses and the user's committed values remain unchanged

#### Scenario: Failed semantic validation publishes nothing
- **WHEN** hierarchy, schema, governance, or guard validation refuses
- **THEN** no item change, transition, or committed terminal is recorded

#### Scenario: Caught publication error rolls back
- **WHEN** a caught error occurs part way through a Planning mutation's transaction
- **THEN** the transaction rolls back completely and no committed receipt is returned

#### Scenario: Abrupt interruption remains detectable
- **WHEN** the process terminates during a Planning mutation, or a file-canonical Planning collection carried a positive audit gap when it was imported
- **THEN** after restart the store holds either the complete change with its transition or neither, and an imported gap remains reported by inspection

#### Scenario: Same-vault writes serialize
- **WHEN** cooperating agents mutate one Planning collection concurrently
- **THEN** the writer lease and the store transaction prevent torn or silently lost updates, while separate vaults remain independent

### Requirement: Manual-edit inspection is report-only
Planning queries SHALL read the collection store under authorization. Inspection SHALL return exactly `kind`, `report_only`, `contract`, `snapshot`, `source_versions`, `diagnostics`, `audit`, and `saved_views`. `kind` SHALL be `collection`, `report_only` true, and `contract` SHALL contain exactly `collection_id`, `path`, `title`, `semantic_profile`, `schema_version`, and `storage`; `storage` SHALL contain exactly `strategy: markdown-items`, `source`, and `format_version: 1`. Source-version entries SHALL be exact `{path, hash}` mappings naming item view paths and row digests. Diagnostics SHALL be a list of at most 64 exact `{code, reason}` mappings. They SHALL report unsupported versions, schema violations, invalid states or dates, parent and area problems, missing templates, pending views (`PROJECTION_PENDING`), and held view corrections (`VIEW_CONFLICT`, `VIEW_INVALID`, `VIEW_DELETED`, `VIEW_UNBOUND`, `VIEW_CONFLICT_COPY`). Audit SHALL contain exact `status` (`baseline`, `ok`, `gap`, or `history_incomplete`) and `gaps`; saved views SHALL expose exact `{name, definition, identity}`. Inspection SHALL NOT change the store or any view, and SHALL NOT invent history. Generic derived-index repair SHALL remain under explicit `maintain_memory(mode="reconcile", dry_run=false)` and SHALL repair only rebuildable state.

#### Scenario: Direct edit is visible with audit gap
- **WHEN** a user changed one valid Planning item outside Exomem before migration, or changes a view with a valid edit after it
- **THEN** a pre-migration edit is imported with its positive audit gap reported and not repaired, and a post-migration edit shows on the next query after the settle window with its view-edit transition and audit status `ok`

#### Scenario: Manual invalidity is human-repair only
- **WHEN** a direct view edit introduces an invalid date, dangling parent, hierarchy cycle, or changed plan ID
- **THEN** the store is unchanged, inspection reports a held view correction with its diagnostics that only a human resolves by a valid edit, resume, or discard, and other Planning mutations for the collection continue
