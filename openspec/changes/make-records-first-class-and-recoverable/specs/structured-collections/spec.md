## MODIFIED Requirements

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

## ADDED Requirements

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
