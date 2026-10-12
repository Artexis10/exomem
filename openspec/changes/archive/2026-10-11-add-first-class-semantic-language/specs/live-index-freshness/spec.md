## ADDED Requirements

### Requirement: Event-Maintained Semantic Unit Indexes
In-process writers and the live watcher SHALL update semantic-unit lexical, optional vector, and graph records from the same parsed parent state used for contract evaluation. Every record SHALL carry the same deterministic `parent_generation`, current parent source hash, and parser schema version. A parent update SHALL replace that parent's unit records transactionally within each sidecar, and self-write suppression MUST NOT suppress the writer-owned semantic-unit update.

#### Scenario: Self-write updates units once
- **WHEN** an Exomem writer commits a parent page and refreshes its semantic-unit records
- **THEN** watcher echo suppression prevents duplicate work while the committed unit records remain current

#### Scenario: External edit replaces parent-owned records
- **WHEN** the watcher observes an external edit that adds, changes, or removes semantic units
- **THEN** it reparses that parent and atomically replaces its unit records without rereading unrelated pages

### Requirement: Query-Time Generation Validation Prevents Mixed Stale Reads
Before returning or traversing a semantic-unit candidate, recall/context SHALL compare its parent source hash/generation with the current on-disk parent and SHALL reject a missing parent, hash mismatch, schema mismatch, or mixed generations across joined lexical/vector/graph records. A bounded current-parent hash cache MAY be used only when invalidated by filesystem identity/stat changes. Independent sidecar transactions MAY temporarily omit fresh candidates, but MUST NOT expose stale identity/content/category/relation state as current. Rejected records SHALL mark deterministic index drift/degradation for reconcile.

#### Scenario: Partial sidecar update cannot mix generations
- **WHEN** Markdown and lexical rows are current but vector or graph rows still carry the prior parent generation
- **THEN** recall may return current lexical results with degradation but does not join or surface the stale vector/graph state

#### Scenario: Committed file invalidates all old rows immediately
- **WHEN** Markdown commits before any derived sidecar transaction completes
- **THEN** old rows fail the current on-disk source-hash check and are not returned as current

#### Scenario: Missed delete cannot leak a unit
- **WHEN** a parent is absent on disk but a sidecar still contains its prior records
- **THEN** query-time validation rejects those records and reconcile records/removes the orphaned generation

### Requirement: Move Delete Trash And Recovery Maintain Unit Freshness
Move, delete, trash, and recovery events SHALL update parent paths and semantic-unit records consistently across lexical, optional vector, and graph sidecars. Recovery SHALL rebuild current units from restored Markdown; no stale old-path or deleted-category result may remain.

#### Scenario: Move has no old-path unit hits
- **WHEN** a parent page moves with unchanged content
- **THEN** unit hits use the new path, anchored identity remains stable where possible, and the old path returns no unit records

#### Scenario: Trash and recovery round trip
- **WHEN** a page is trashed and later recovered
- **THEN** its units disappear from active recall while trashed and are rebuilt from restored Markdown on recovery

### Requirement: Reconcile Proves Sidecar Parity
Reconcile SHALL compare on-disk semantic-unit parses and parent generations with lexical, optional vector, and graph records, repair missing/stale/mixed-generation/orphaned parent-owned rows, and report semantic-contract drift separately from index drift. Repeated reconcile over unchanged state SHALL be idempotent.

#### Scenario: Missed watcher event is healed
- **WHEN** an external semantic-unit edit occurs without a watcher event
- **THEN** reconcile detects the file/index mismatch, replaces the affected parent records, and reports the repair

#### Scenario: Contract violation is not rewritten
- **WHEN** reconcile finds valid index drift plus invalid semantic Markdown
- **THEN** it repairs all deterministically derivable records, preserves the Markdown, and reports the semantic violation for review

### Requirement: Sidecar Versions And Fallbacks Are Explicit
Semantic-unit schema changes SHALL bump affected sidecar versions and trigger rebuilds. When an index is absent, stale, mixed-generation, disabled, or unavailable, recall SHALL fall back to deterministic parse/lexical behavior where bounded and SHALL report degradation rather than returning stale unit identity.

#### Scenario: Old sidecar rebuilds without Markdown migration
- **WHEN** Exomem opens a vault with a prior sidecar schema
- **THEN** it rebuilds derived semantic-unit data from Markdown and does not rewrite the source files
