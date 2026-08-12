## MODIFIED Requirements

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

### Requirement: Auditable agent mutations and receipts
Every structured-collection product mutation SHALL require a concise reason and SHALL return a bounded receipt containing collection identity, item identity (null for create), operation-specific required before and after item/container hashes, affected canonical paths, committed outcome, and a transition correlation. The profile contract SHALL supply the public audit property, per-item content-free correlation marker, standalone reference namespace, and operation label while one shared audit-chain implementation retains the same validation, publication, history, and direct-edit gap semantics.

Existing Records SHALL retain the YAML mapping `record_audit: {version: 1, head: <transition-id>}`, `record_id`, current Record item markers, `exomem://record/...`, and their serialized activity events. New Planning SHALL use `plan_audit: {version: 1, head: <transition-id>}`, `plan_id`, Planning item markers, `exomem://plan/...`, and Planning-labeled events. Every agent-touched canonical item SHALL carry exactly its latest content-free transition ID.

The existing activity log SHALL contain one strict versioned machine-parseable event per transition with its ID, predecessor, operation, collection/manifest/source/item correlation, before/after manifest/item/container hashes, replay payload hash where relevant, and sanitized rationale, without copying canonical item values. Inspection SHALL validate the active profile's correlation rules for every reachable transition, reconstruct the predecessor chain from the manifest head, deduplicate exact events, refuse forks/conflicts as gaps, and distinguish `baseline`, `ok`, positive `gap`, and bounded `history_incomplete`; direct human edits remain visible across later successful mutations. It SHALL use bounded descriptor-bound no-follow regular-file reads for the manifest, canonical marker sources, and activity history, bind markers to the same snapshot being inspected, cap markers, and SHALL never repair or invent history. Operational journals and governance receipts SHALL retain their existing distinct roles.

Existing Records create events SHALL retain a null item key and name the declared source; Records append/update events SHALL retain a normalized item UUID and a canonical item path valid for the declared storage strategy. Planning create events SHALL likewise have a null item key and name the declared Markdown-items source; Planning add/update/triage events SHALL carry the normalized Planning UUID in the generic event `item_key` and a canonical Markdown item path contained by that source. Guarded batch publication SHALL roll completed replacements back after caught errors. Abrupt process termination MAY leave canonical truth ahead of the activity event, which inspection SHALL expose as a positive audit gap; the substrate SHALL NOT claim transactional cross-file atomicity.

#### Scenario: Successful normal update records one audit event
- **WHEN** a guarded Record item update commits
- **THEN** the terminal becomes committed only after every planned replacement completes, and the response, manifest, item marker, and activity event use the existing Records names with matching canonical audit identifiers and hashes

#### Scenario: Successful Planning triage uses the shared audit engine
- **WHEN** a guarded Planning triage mutation commits
- **THEN** the response, `plan_audit` head, Planning item marker, and activity event carry one matching transition without any `record_audit` property

#### Scenario: Failed validation records no committed audit event
- **WHEN** schema, relationship, governance, or drift validation refuses a collection mutation
- **THEN** no canonical item change or committed agent-mutation audit entry is written

#### Scenario: Abrupt interruption exposes an audit gap
- **WHEN** a process terminates after canonical replacement but before the activity-log replacement
- **THEN** the canonical human-owned files remain truth, report-only inspection detects the hash/audit mismatch, and Exomem does not invent or silently hide a history event

#### Scenario: Pre-publication interruption leaves no canonical-looking scaffold
- **WHEN** a simulated `BaseException` interrupts staging before the first canonical replacement during collection creation
- **THEN** Exomem removes its empty created directories and batch workspaces, rethrows the original exception without normalization, and an exact retry is not wedged by tool-owned residue

#### Scenario: Human audit-mapping reformat remains mutable
- **WHEN** a user reformats a valid profile audit flow mapping into an equivalent YAML block mapping
- **THEN** the next mutation replaces that complete mapping node while preserving unrelated manifest bytes and produces valid YAML
