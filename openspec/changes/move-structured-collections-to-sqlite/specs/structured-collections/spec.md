## ADDED Requirements

### Requirement: One embedded collection store is the single source of truth
Each vault SHALL have exactly one embedded SQLite collection store that is the only canonical source for structured collections of every collection type, built-in (Records, Planning) or declared: their type declarations, manifests, items, item versions, per-row provenance, held candidates, and audit transitions. The store SHALL enforce collection-scoped item identity and declared natural-key uniqueness with database constraints, SHALL commit every mutation, including every row of a bulk mutation, in one transaction under the existing single-writer lease, and SHALL make its transaction, audit-effect, item-version, provenance and manifest-history tables append-only. Knowledge notes, entities, sources, evidence and episodes SHALL remain Markdown and SHALL NOT be stored in it. The `dataset` storage strategy SHALL remain a file-canonical, query-only adapter and SHALL NOT be imported into the store. Records, Planning and declared types SHALL keep their distinct kinds, typed schemas, natural keys, provenance, audit and governance; only the storage engine changes. The store SHALL require SQLite 3.38 or newer and a local filesystem where WAL journaling takes effect, and readiness SHALL refuse collection writes, never falling back to file-canonical writes, when either fails.

#### Scenario: Natural-key uniqueness is a constraint
- **WHEN** two writers race to append items whose declared natural keys serialize equally under different identities
- **THEN** exactly one commits and the other refuses with the natural-key conflict naming the holder, and no read can ever observe two live items with that natural key

#### Scenario: A multi-row mutation is one transaction
- **WHEN** a bulk mutation of 500 rows fails validation or crashes part way
- **THEN** either all of its accepted rows, their versions, provenance and audit effects are durable, or none are

#### Scenario: Audit history cannot be rewritten in place
- **WHEN** any code path attempts to update or delete a committed transaction, audit effect, item version, provenance entry or manifest version
- **THEN** the store aborts the statement and the history is unchanged

#### Scenario: Datasets stay files
- **WHEN** a collection declares the `dataset` strategy
- **THEN** its CSV, TSV or JSON file remains canonical and query-only, and the store holds no rows for it

#### Scenario: Unsupported engine refuses rather than degrades
- **WHEN** the runtime SQLite is older than 3.38 or WAL journaling cannot be enabled on the state root
- **THEN** readiness reports the collection store unavailable with a remediation, collection writes refuse, and knowledge writes are unaffected

### Requirement: Guards are collection generations and row versions
Each collection SHALL carry a generation that increments exactly once per committed transaction touching it, and each item a row version that increments exactly once per change to that item. The wire guard and version fields (`expected_container_hash`, `before_container_hash`, `after_container_hash`, `expected_item_version`, `item_version`, `before_item_hash`, `after_item_hash`) SHALL keep their names and 64-lowercase-hex shape and SHALL be domain-separated SHA-256 digests of the collection generation and audit head, and of the item row version and payload hash, respectively. A guard check SHALL be decided inside the mutation transaction without reading or hashing any other item. A query `snapshot` SHALL be a digest over the caller's authorized row identities and versions and the manifest version, so a change to a row withheld from the caller SHALL NOT change that caller's snapshot or invalidate its continuation.

#### Scenario: Stale container guard refuses
- **WHEN** a mutation carries a container hash from before another committed transaction on the collection
- **THEN** it refuses as stale and nothing is written

#### Scenario: Stale item guard refuses
- **WHEN** an update carries an item version from before that item's latest change
- **THEN** it refuses as stale and the newer item is preserved

#### Scenario: Guard cost is independent of collection size
- **WHEN** a guarded append runs against a collection of 10,000 items
- **THEN** the guard is decided from the collection row and the target item only

#### Scenario: Hidden change leaves a released continuation valid
- **WHEN** only an item withheld from the caller changes between two pages of the caller's query
- **THEN** the caller's snapshot is unchanged and the continuation resumes

### Requirement: Collection views are Markdown projections of the store
The substrate SHALL render every collection manifest, item, log-layout collection, and held candidate as a Markdown view at the same vault-relative path the file-canonical layout used, using the existing item, log, filename and managed-presentation renderers and keeping the visible system identity properties. Views SHALL NOT be canonical. Every item and manifest view, and every log block, SHALL carry a view stamp naming the store identity, the rendering store instance, the row version and a payload-hash prefix. The stamp SHALL be a reserved system property that callers cannot write and the payload hash ignores.

The item, manifest and held views changed by a mutation SHALL be staged and fsynced inside the mutation's transaction and published after commit, before the mutation is acknowledged, so that any reader of the vault file sees the acknowledged write. A view SHALL never be published ahead of its committed row. Every publish, synchronous or asynchronous, SHALL be check-then-swap:
- move the current view aside atomically;
- install the staged view without replacing any file that appeared in between;
- hash the aside copy, and hold it as `VIEW_CONFLICT` with its bytes when it differs from the view's expected on-disk hash.

A publish SHALL NEVER overwrite a view edit the substrate has not classified. On every start and periodically, the substrate SHALL hash every view path before the projector runs, and SHALL send mismatches to edit-back classification, so detection does not depend on a file-system watcher. The staged view's hash SHALL be recorded inside the mutation's transaction, adding no second commit to the acknowledgement path. Log-layout views, history pages and type views SHALL be published asynchronously with bounded lag. Every view's pending state SHALL be recorded durably in the same transaction as the mutation, so that a crash after commit re-renders it on restart. A projection failure SHALL NOT turn a committed mutation into a refusal: it SHALL be retried, and reported as a receipt warning and in inspection.

Index synchronization of views SHALL NOT be on the acknowledgement path. Structured readers SHALL read the store rather than views. Vault file tools SHALL refuse to delete, move or recover a path owned by a collection view, with a remediation naming the collection operation. Views SHALL be normalized on re-render: values and the authored body SHALL be emitted exactly, while YAML formatting that is not data need not be preserved. Audit markers and manifest audit heads SHALL NOT be rendered.

#### Scenario: A committed append appears as a view
- **WHEN** an append is acknowledged
- **THEN** the item's view already exists at the path its filename recipe names, with its values, body and managed presentation, and a following `get_page` of that path returns them

#### Scenario: An agent write never overwrites an unseen edit
- **WHEN** a human edits an item view and, before the watcher has classified the edit (or while no watcher runs, or while the service is offline), an agent update to the same item commits
- **THEN** the publish finds the aside copy differs from the expected hash, holds it as `VIEW_CONFLICT` with the human's bytes and both versions, and installs the agent's version; nothing the human wrote is lost

#### Scenario: A file created between the two renames is not replaced
- **WHEN** an editor writes the view path after the current view was moved aside and before staging is installed
- **THEN** the no-clobber install fails, the editor's file is held exactly like a changed aside, and the view is retried

#### Scenario: Startup reconcile finds edits made while offline
- **WHEN** a view was edited while the service was stopped
- **THEN** the startup reconcile classifies the edit before any projection runs

#### Scenario: A view is never ahead of the store
- **WHEN** the process stops after a view is staged but before the transaction commits
- **THEN** no view shows the uncommitted values, and the staging file is removed by bounded crash recovery

#### Scenario: File tools cannot delete a view
- **WHEN** a file tool is asked to delete or move an item view
- **THEN** it refuses with `COLLECTION_VIEW_PATH`, and the store and view are unchanged

#### Scenario: Projection resumes after a crash
- **WHEN** the process stops after a commit but before the view is written
- **THEN** on restart the pending view is rendered and inspection reported it as pending until then

#### Scenario: Obsidian sees ordinary Markdown
- **WHEN** a user opens a collection directory in an ordinary Markdown editor
- **THEN** the manifest and every item are readable Markdown files with typed properties and bodies, without a plugin or database tool

### Requirement: Edited views return through the governed write path
When a view file changes and its bytes differ from the last published or pending bytes, as found by the file watcher or the reconcile on the writer-lease holder, the substrate SHALL classify the view stamp first:
- a stamp from another store or from an instance outside this store's lineage SHALL set `COLLECTION_STORE_DIVERGED` and be held `VIEW_FOREIGN`;
- a stamp older than the item's current row version SHALL be held `VIEW_CONFLICT`;
- a stamp whose payload-hash prefix does not match the row SHALL be held `VIEW_INVALID`;
- only a view whose bytes are exactly the bytes parsed at import MAY be unstamped, and any other unstamped view SHALL be held `VIEW_INVALID`.

For a view stamped with the current row version, it SHALL, after the file has been quiet for a settle window, classify the edit deterministically without interpreting prose. A formatting-only edit SHALL be re-rendered without a transaction. A valid change to declared values or body, made against the item's current row version, SHALL be applied as an ordinary governed `update` (or Planning `update` or `triage`) through the same leaf functions, validation, governance precommit, audit transition and receipt as a tool call, with actor `owner:view-edit`, a reason naming the view, and a request identity derived from the view path, base row version and file hash so that a replay is a no-op; the view SHALL then be re-rendered. A valid manifest edit SHALL be applied as a governed `revise`. An edit that is ambiguous, schema-breaking, violates Planning lifecycle or hierarchy rules, changes a system property, conflicts with a newer row version, deletes a view, moves a view out of its collection's source root, adds an unbound file under a projection root, or is a file-sync conflict copy SHALL become a held view correction carrying the human's bytes and field-addressed diagnostics, and SHALL NEVER silently overwrite or discard either the store row or the human's edit. A move of a view within its collection's source root with an intact current stamp SHALL be a governed `view_move` transition that updates the item's view path, re-authorized for the new path, and SHALL NOT re-render the old path. A later valid edit of the same view SHALL supersede its held correction. History pages SHALL be read-only: edits to them SHALL be ignored and re-rendered.

#### Scenario: A value edit in Obsidian becomes an audited update
- **WHEN** a user changes one declared field in an item view and saves
- **THEN** after the settle window the store holds the new value, one audit transition names the view edit, and the view is re-rendered

#### Scenario: Replaying the same edit adds nothing
- **WHEN** the watcher processes the same edited bytes twice, including across a restart
- **THEN** exactly one transition exists for that edit

#### Scenario: Edit against a stale view is held
- **WHEN** an agent update commits and, before the view is re-rendered, the user edits the old view
- **THEN** the user's bytes are kept in a held `VIEW_CONFLICT` correction naming both versions, the agent's committed value is unchanged, and the view is re-rendered to the current row

#### Scenario: Schema-breaking edit is held
- **WHEN** a user adds an undeclared property or writes a value of the wrong type in a view
- **THEN** the row is unchanged, a held `VIEW_INVALID` correction carries the edit and its field-addressed diagnostics, and inspection and attention report it

#### Scenario: A stale buffer cannot revert a newer version
- **WHEN** a second device or an editor buffer opened at row version 4 saves over a view whose item is at version 5
- **THEN** the save is held `VIEW_CONFLICT` with both versions and the human's bytes, and version 5 is not reverted

#### Scenario: A foreign store's view is not adopted
- **WHEN** a view stamped by a store instance outside this store's lineage appears
- **THEN** collection writes refuse `COLLECTION_STORE_DIVERGED` and the view is held `VIEW_FOREIGN`, never applied as an owner edit

#### Scenario: A moved view keeps its identity
- **WHEN** a user renames an item view within its collection folder and the editor rewrites links to it
- **THEN** a `view_move` transition records the new path and the old path is not re-rendered

#### Scenario: Deleting a view does not delete the item
- **WHEN** a user deletes an item view file
- **THEN** the item remains in the store, a held `VIEW_DELETED` correction is reported, and the view is re-rendered

#### Scenario: Typing does not create a flood of held corrections
- **WHEN** an editor autosaves several intermediate invalid states and then a valid one
- **THEN** only the valid state is applied and no held correction remains for that view

### Requirement: Collection audit is an append-only store table with a rendered history view
Every committed collection mutation SHALL be exactly one audit transition in the store, carrying its 24-lowercase-hex transition identifier, the operation, the actor, the sanitized reason, the before and after collection generation and manifest version, the commit time, the recorded receipt, and a hash chain over its predecessor, with one content-free audit effect per changed item naming its row, effect, and before and after versions and hashes. A bulk mutation SHALL be one transition with one effect per written row. Audit transitions SHALL copy no item values. The transition SHALL commit in the same transaction as the change it describes, so no committed change can lack its transition. Records and Planning mutations SHALL NOT write audit events to `Knowledge Base/log.md`. The substrate SHALL render, per collection, a read-only history page generated from the audit table, newest first and readable like an activity log: date, operation, actor, reason, and links to the changed item views. The page SHALL be bounded, with older transitions on numbered pages of a fixed number of transitions, and only the newest numbered page SHALL be rewritten. It SHALL be rendered for the intersection of the audiences that can read its path: an effect SHALL appear only when every such audience can read its item, and a transaction with any omitted effect SHALL be shown without its reason. Inspection SHALL verify the audit chain incrementally from a recorded verified-through point.

#### Scenario: Change and audit are atomic
- **WHEN** a mutation commits or is interrupted at any point
- **THEN** either both the change and its transition are durable, or neither is

#### Scenario: A bulk batch is one transition
- **WHEN** a bulk upsert writes 24 rows
- **THEN** one transition with 24 effects is recorded, and the collection audit status remains `ok`

#### Scenario: History page reads like a log
- **WHEN** a user opens a collection's history page after several mutations
- **THEN** it lists them newest first with operation, actor, reason and links to the changed items, and contains no item values

#### Scenario: Editing the history page changes nothing
- **WHEN** a user edits a history page
- **THEN** no audit transition changes and the page is re-rendered from the table

#### Scenario: Activity log no longer grows with Records writes
- **WHEN** a Records or Planning mutation commits
- **THEN** `Knowledge Base/log.md` is not read or rewritten by it

### Requirement: Collection store snapshots are consistent and portable
The live store's files SHALL NEVER be copied by file-level backup or export. The substrate SHALL produce consistent snapshots of the collection store only through the SQLite online backup API into a staging file that is switched to a single-file journal mode, integrity-checked and atomically renamed. It SHALL publish such a snapshot as a replica inside the vault after committed transactions, coalesced off the acknowledgement path, and synchronously on quiesce, writer-lease release, shutdown, upgrade handoff and portability export. The replica SHALL NEVER be opened for writing in place. A replica SHALL be adopted only when the vault-side mode marker names store mode and the same store identity.

Every transaction SHALL advance a store-wide sequence and chained head hash. The writer-lease holder SHALL report `(store_id, instance_id, commit_seq, head_hash)` to the coordinator on renew and release. A new holder facing a head recorded by another instance SHALL adopt the replica only once it reaches that head, and until then SHALL refuse collection writes with the retryable `COLLECTION_STORE_SYNC_PENDING`, naming both sequences and the remedy. A store for which no foreign head was ever recorded SHALL never wait. An owner-only, preview-first `adopt-local` operation SHALL let the holder continue from local state, recording the fork point, and the other side's later-arriving changes SHALL be reconciled as held corrections.

The coordinator SHALL persist the head atomically with a valid holder/token renew or ordinary release and retain it after release or expiry. A stale holder SHALL NOT replace it; operator release SHALL preserve the recorded head. The renewer SHALL use a trusted head provider rather than the opening-thread SQLite writer. Ordinary release SHALL flush the replica before relinquishing local writer authority. The collection-store capability fence SHALL remain separate from governance schema versions 3/4, and file-only vaults SHALL retain their existing admission behavior.

An explicit operator transition to a different store identity SHALL use the current generation and atomically advance it, clear only the former identity's head and revoke the lease token. Same-target/capability replay at the current or predecessor generation SHALL preserve head, holder and token. Generations SHALL NOT reset or disappear, and stale replay after intervening identity transitions SHALL conflict. These coordinator operations SHALL NOT perform or authorize export/re-migration; the adapter SHALL preserve the old identity/head in durable transition evidence before the cut and reacquire before marker cutover.

Each live store SHALL carry an instance identity and lineage. Replica publication SHALL be check-then-swap against the last replica this instance published. Any foreign replica or foreign view stamp SHALL set the divergence state immediately. A service on a copied or moved vault with no local store SHALL adopt the replica as a new lineage entry and be writable. It SHALL refuse collection writes with a divergence error, while serving reads and knowledge writes, when the local store holds transactions the replica lacks. A preview-first operator reconciliation SHALL turn every item changed by the divergent store after the fork point into a held view correction on the surviving store, so divergence never silently loses a write. Running more than one collection writer on one vault without the multi-host writer lease SHALL be explicitly unsupported, and `describe` and the doctor probe SHALL say so. Restore SHALL validate integrity, schema version and store identity, and SHALL surface any difference between the restored rows and existing views as held view corrections rather than overwriting either.

#### Scenario: Backup of the vault is consistent
- **WHEN** restic or a vault copy captures the vault while agents are writing
- **THEN** the captured replica opens, passes `integrity_check`, and reflects a committed state

#### Scenario: Takeover adopts the newer replica
- **WHEN** a second host acquires the writer lease, the coordinator holds the previous holder's head, and the vault replica has reached that head
- **THEN** it adopts the replica before accepting collection writes

#### Scenario: Takeover on a lagging replica waits, boundedly
- **WHEN** the coordinator holds a foreign head that the host's replica has not yet reached
- **THEN** collection writes refuse `COLLECTION_STORE_SYNC_PENDING` naming both sequences and the remedy, reads and knowledge writes continue, and writes resume once the replica arrives

#### Scenario: A single-host store never waits
- **WHEN** the only head ever recorded for the vault came from this instance, including after idle lease release and re-acquisition
- **THEN** collection writes proceed without any sync wait

#### Scenario: Adopt-local cannot lose the other side
- **WHEN** the owner runs adopt-local while a foreign head is pending, and the other host's replica later arrives
- **THEN** the fork point is recorded, writes proceed, and every item the other side changed after the fork point becomes a held correction

#### Scenario: Two services on one vault with different state roots
- **WHEN** a second service with its own state root starts writing the same vault without the lease
- **THEN** the first replica publish or view stamp it meets from the other instance sets `COLLECTION_STORE_DIVERGED` immediately

#### Scenario: Divergence fails closed
- **WHEN** the local store holds transactions absent from the replica
- **THEN** collection writes refuse with the divergence error and operator attention is raised

#### Scenario: Divergence reconciles into held corrections
- **WHEN** an operator reconciles two diverged stores
- **THEN** every item the losing store changed after the fork point is held as a correction on the surviving store with its values, and nothing is overwritten

#### Scenario: A copied vault is writable
- **WHEN** a service starts on a copy of the vault with no local store
- **THEN** it adopts the vault's replica as its live store and accepts collection writes

### Requirement: Collection store migration is verifiable and reversible
A vault SHALL move from file-canonical collections to the store only through a declared offline migration that imports every Records and Planning collection and proves a round trip before the store becomes canonical. The proof requires all of the following: item counts equal; every row, rendered and parsed back, yields equal values, body, identity and natural key; payload hashes equal the legacy derivation; the imported legacy audit chain has the same head and length; manifest text is byte-equal; and the legacy audit status is preserved, never upgraded. Import SHALL rewrite no vault file and SHALL record the current file bytes as the current views. A vault with duplicate identities, schema violations or unsupported versions SHALL NOT migrate until they are fixed. The vault's collection mode SHALL have one authority: a vault-side mode marker that adoption reads. On a multi-host vault, migration SHALL refuse while the lease coordinator does not explicitly advertise `collections-store-v1`, with a message naming the exact release to upgrade the coordinator to. Store adoption SHALL advance a separate vault-specific `collections-store-v1` capability fence with generation CAS and lease-token invalidation in one coordinator transaction, reacquire before marker cutover, and record an optional state compatibility descriptor so older releases refuse before access. Governance versions 3/4 and file-only manifests SHALL remain unchanged; ignoring extra request fields SHALL NOT establish coordinator capability. The importer SHALL read legacy audit history with an uncapped streaming reader filtered per collection. It SHALL record each view's expected hash as the hash of the exact bytes it parsed. Non-managed installs SHALL migrate through an offline command with the same proof. The migration SHALL run under the managed standby-upgrade handoff: pre-import on the standby without ownership, re-verification of only the collections changed since pre-import (carrying forward the proofs of unchanged ones) within the cutover budget, abandoning the upgrade cleanly when it cannot, and atomic publication, so that writes pause only for the ordinary bounded handoff. The substrate SHALL provide a preview-first reverse export that renders the store into the legacy file layout with a content-free checkpoint transition per collection, so the legacy inspector reports `acknowledged_gap` for any collection written in store mode. Export SHALL set the mode marker to exported and tombstone the replica so no host can adopt it. Downgrade to a pre-store release SHALL require export first.

#### Scenario: Import proves its round trip
- **WHEN** a vault with Records and Planning collections is migrated
- **THEN** every check of the round-trip proof passes for every collection before the store is published, and no vault file changed

#### Scenario: A failed proof leaves files canonical
- **WHEN** any collection fails a round-trip check
- **THEN** the vault stays file-canonical, the store is not published, and the report names the collection and the failed check

#### Scenario: Writes during pre-import are not lost
- **WHEN** an agent writes to a collection while the standby is pre-importing
- **THEN** that collection is re-imported and re-verified at handoff

#### Scenario: Legacy history beyond the file-mode bounds is imported
- **WHEN** a vault's activity logs exceed 8 MB across more than 128 archives and a collection's chain is deeper than 2048 transitions
- **THEN** every reachable event is imported and the proof's chain-length check passes

#### Scenario: An outdated coordinator blocks migration with a named release
- **WHEN** a multi-host vault's coordinator does not explicitly advertise `collections-store-v1`
- **THEN** preflight refuses with `COLLECTION_STORE_COORDINATOR_UPGRADE_REQUIRED` naming the exact release to upgrade the coordinator to, and the vault stays in file mode

#### Scenario: An exported vault cannot be re-adopted
- **WHEN** a migrated vault is exported and a host later starts with a stale local store or finds the tombstoned replica
- **THEN** the mode marker says exported and no replica or local store is adopted as canonical

#### Scenario: Reverse export restores files
- **WHEN** a migrated vault is exported back to files
- **THEN** collections never written in store mode are byte-equal to their pre-migration files, and written collections are legacy-valid and report `acknowledged_gap`

### Requirement: Collection store writes meet a latency budget
With the store canonical, the release acceptance harness SHALL measure, and the delivery SHALL meet: a guarded single append p95 under 20 ms end to end at 10,000 items, measured through the real dispatcher, idempotency ledger, writer lease, collection resolution, governance and synchronous item-view publication, with a per-stage timer and budget for each; a 500-row bulk upsert under 1 s end to end; and structured query results identical to the file-canonical path on the parity corpus, with query latency no worse than the file path at every measured size. A client guard refresh through `inspect` SHALL have p95 under 15 ms. The acknowledgement path SHALL NOT include reading other items, hashing the collection, discovering or parsing a manifest file, rendering or publishing views other than the changed item, manifest and held views, index synchronization of views, or reading or rewriting `Knowledge Base/log.md`.

#### Scenario: Append stays flat as the collection grows
- **WHEN** guarded appends are measured at 1,000 and 10,000 items through the real dispatcher, including a collection where row-level policy withholds a tenth of the rows, on Linux and on Windows NTFS
- **THEN** every p95 is under 20 ms, and no stage exceeds its budget

#### Scenario: Collection resolution does not read the manifest file
- **WHEN** an append resolves its collection
- **THEN** the contract comes from the store row and a cache keyed by manifest version, and no manifest file is read or parsed

#### Scenario: Bulk upsert of 500 rows
- **WHEN** 500 valid rows are submitted in one bulk upsert against a 10,000-item collection
- **THEN** the call completes in under 1 s

#### Scenario: Query parity
- **WHEN** the parity corpus queries run against the file path and the store
- **THEN** rows, order, totals, aggregates and rendered output are equal

### Requirement: Declared storage strategies are view layouts over the collection store
The substrate SHALL support three declared storage strategies. For `markdown-log` and `markdown-items` the strategy SHALL name the layout of the collection's Markdown views, one chronological log file of item blocks or one Markdown file per item, while the canonical data of both SHALL live in the collection store. The `dataset` strategy (CSV, TSV, or JSON) SHALL remain file-canonical and query-only. Any cache, index, export, summary, replica, or generated view SHALL be derived from the canonical source it represents.

Chronological-log child rows SHALL declare a bounded `container_field` in addition to their delimiter and fields; that container SHALL be a declared array-of-object item-schema field, so adapters do not impose domain field names. For the log layout, the store SHALL keep the view's frame, meaning the bytes outside the declared item section, its UTF-8 BOM, its newline style and its final-newline state, and SHALL re-emit them byte-identically. Markdown parsing of views and legacy files SHALL accept exactly one leading UTF-8 BOM for frontmatter parsing. A log-layout view SHALL stay within its rendering size cap, and a write that would exceed it SHALL refuse with a remediation to use the items layout.

#### Scenario: Log layout renders one readable history file
- **WHEN** a log-layout collection is queried or safely mutated
- **THEN** the store is canonical and its log view shows every item as a readable block in declared order, and no generated dataset is promoted implicitly

#### Scenario: File-per-item collection uses ordinary properties
- **WHEN** an items-layout collection stores a record
- **THEN** the record's view is an ordinary Markdown file with stable item and collection identifiers plus typed YAML properties and an optional readable body

#### Scenario: Dataset stays directly editable
- **WHEN** a dataset-backed collection uses CSV, TSV, or JSON
- **THEN** its rows remain readable and editable with ordinary tools, Exomem queries them from the file, and dataset append/update refuses rather than reserializing the file

### Requirement: Imported legacy audit gaps can be explicitly rebaselined

The collection substrate SHALL provide an explicit `rebaseline` mutation that acknowledges an audit gap imported from legacy file-canonical history. Rebaseline SHALL require `expected_manifest_hash`, `expected_container_hash`, the exact inspect-reported `acknowledged_gap_codes`, and a concise `why`. It SHALL revalidate the complete collection, recheck the acknowledgement and guards inside the store transaction, write no item content, and commit a content-free checkpoint transition. On a collection with no gap it SHALL refuse because the acknowledgement cannot match.

Rebaseline SHALL record a lifecycle transition and return receipt version 2 with `operation: rebaseline`, the prior head, `continuity: false`, sorted exact acknowledged gap codes, a deterministic `gap_fingerprint` over canonical JSON containing the prior head, codes, and guarded before-manifest/container hashes, a `checkpoint_snapshot_hash` over canonical JSON containing the sorted authorized pre-checkpoint manifest and item identities and versions, before/after manifest and container hashes, and sanitized rationale. It SHALL copy no item values. Rebaseline receipt v2 requires non-empty codes, both fingerprints, continuity false, one manifest affected path, and `outcome: committed`.

For both lifecycle operations, `payload_hash` is SHA-256 over `exomem-record-lifecycle-request:v2\0` plus canonical JSON `{action, collection_id, before_manifest_hash, before_container_hash, proposed_manifest_hash, acknowledged_gap_codes, rationale}`. Gap fingerprints use `exomem-record-gap:v2\0`; checkpoint fingerprints use `exomem-record-checkpoint:v2\0`. Canonical JSON is UTF-8 with sorted object keys, no whitespace, and `ensure_ascii=false`. Transition IDs are independent 24-hex values.

Inspection after rebaseline SHALL report audit status `acknowledged_gap`, never `ok`, and SHALL preserve a bounded permanent discontinuity containing `provenance_continuity: false`, the prior head, acknowledged gap codes, rationale, checkpoint transition, and both fingerprints. Later valid mutations SHALL extend the chain without erasing or relabelling that history. Rebaseline SHALL use the same authorize-before-read and complete-authorization rules as revision. It SHALL refuse schema violations, unauthorized items, stale guards, or acknowledgements that do not exactly match current gaps. It SHALL NOT invent missing transitions.

#### Scenario: Imported legacy gap becomes an acknowledged checkpoint
- **WHEN** a migrated collection reports an imported legacy gap and the caller rebaselines those exact gaps with current guards
- **THEN** no item content changes, the audit status becomes `acknowledged_gap`, and inspect/query/history expose the permanent discontinuity and `provenance_continuity: false`

#### Scenario: A store-native collection has nothing to rebaseline
- **WHEN** a store-native collection with `ok` status receives a rebaseline
- **THEN** it refuses because no gap matches the acknowledgement, and no transition is written

#### Scenario: Gap drift invalidates acknowledgement
- **WHEN** the collection changes after inspect but before rebaseline
- **THEN** the expected hashes fail closed and no checkpoint is written

#### Scenario: Hidden gap cannot be acknowledged by inference
- **WHEN** the caller cannot receive every item and exact gap diagnostic required for the checkpoint
- **THEN** rebaseline refuses without revealing or accepting guessed gap codes

### Requirement: Collection types are declared data over one mechanism
The substrate SHALL implement structured collections as one generic mechanism parameterized by a collection type declaration. A declaration SHALL state:
- an immutable type name, item type and placement layer;
- a kind from a closed vocabulary (initially `observed`, `intended`, `procedural`, `reference`);
- typed fields with the existing field types and limits, whether collections may extend them, and a natural key;
- an optional lifecycle: one state field, its states, initial state, allowed transitions, served states, and declarative per-state field constraints;
- optional named product-owned validators from a closed registry;
- a surfacing rule, a default audience (`owner` or `policy`), presentation recipes and saved views.

A declaration SHALL NOT carry code, model instructions, or unbounded patterns. A new type's placement segment SHALL be refused with `COLLECTION_TYPE_PLACEMENT_OCCUPIED`, naming the folder, when it already holds any file that is not a collection view, so rendered views never mix with hand-written notes. A file without a collection binding under a type's placement SHALL never be read as an item. Records and Planning SHALL be built-in declarations shipped with the product, changed only by release. Only built-in declarations MAY carry wire aliases for legacy property, receipt and error names. A collection manifest SHALL name its type, with `semantic_profile: records` and `semantic_profile: planning` accepted as aliases for the built-in types. The kind SHALL change only version semantics (and, in a follow-up change, compiler surfacing): identity, natural keys, guards, transactions, audit, governance, projection, edit-back, query, snapshots and migration SHALL be the same code for every type.

#### Scenario: A built-in type is a declaration, not a code path
- **WHEN** the Records and Planning declarations are loaded
- **THEN** both collection types resolve through the same type registry and generic operations as any declared type, and no mechanism branches on their names

#### Scenario: A declaration with code or an unknown kind is refused
- **WHEN** a proposed declaration names an unknown kind, an unknown key, an undeclared natural-key field, an unreachable state, or a placement that collides with an existing or reserved layer
- **THEN** validation reports field-addressed findings and nothing is saved

#### Scenario: A placement folder holding notes refuses
- **WHEN** a new type's placement `Knowledge Base/<placement>/` already holds a hand-written note or any other file that is not a collection view
- **THEN** the save refuses with `COLLECTION_TYPE_PLACEMENT_OCCUPIED` naming that folder, without naming or counting its files, and no type, collection or view is created

#### Scenario: A note added later is never read as a row
- **WHEN** a Markdown file with no collection binding is created under an existing type's placement
- **THEN** edit-back holds it as `VIEW_UNBOUND` and no item is created unless it is explicitly resumed

#### Scenario: Only built-ins carry wire aliases
- **WHEN** a declared type proposes a `wire` block or the name of a built-in type
- **THEN** it is refused, with `BUILTIN_COLLECTION_TYPE` for a built-in name

### Requirement: Declared collection types get every collection capability without code
A collection type saved through `schema_memory` SHALL be usable by the very next operation, with no code change, restart or release. Its collections SHALL immediately have:
- store-canonical items with natural-key uniqueness, generation and row-version guards, request-identity exactly-once and content replay;
- one audit transition per mutation, with a rendered history page;
- row-level governance with its default audience, with withheld indistinguishable from absent;
- Markdown views under its placement layer, with governed edit-back and held corrections;
- recall exclusion and hosted placement pinning as a structured layer;
- bulk upsert, query with saved views, snapshots and migration;

Lifecycle transitions SHALL be validated against the declared state machine, per-state constraints and named validators.

#### Scenario: A type declared in conversation is usable immediately
- **WHEN** an agent saves a new `procedural` type and a first collection of it, then adds an item and edits its view in an ordinary editor
- **THEN** the add commits with a receipt and audit transition, the view appears under the type's placement, the edit becomes a governed update, and the item is excluded from ordinary recall

#### Scenario: An illegal lifecycle transition refuses
- **WHEN** a transition names a target state that the declared state machine does not allow from the item's current state
- **THEN** it refuses with field-addressed diagnostics and nothing is written

### Requirement: Collection types are authored, versioned and migrated through schema_memory
`schema_memory` SHALL accept `subject: "collection-types"` with the operations `inventory`, `inspect`, `validate`, `diff`, `save-collection-type`, `history` and `restore`, and SHALL refuse `infer`.
- `diff` SHALL classify a proposal as `new`, `compatible`, `migrating`, `release-widening` or `refused`, and SHALL preview per-collection item impact with the first failing items and field paths.
- `save-collection-type` SHALL require `why`, SHALL require the current declaration hash when the type exists and forbid it when it does not, and SHALL refuse any proposal with findings.
- A `migrating` change SHALL carry a closed set of migration steps (`rename_field`, `map_values`, `default_value`, `drop_field`, `convert`, `recompute_natural_key`). The new type version, derived manifest versions, and every affected item rewritten with its own audit effect SHALL commit in one store transaction.
- A save SHALL refuse wholly if any item would fail validation or collide on a recomputed natural key, and SHALL require the caller's complete authorized view of every collection of the type.
- Changing the name, item type, kind or placement SHALL be refused.
- A `release-widening` change SHALL be saved only by the owner principal.
- Item identities SHALL never change, so references to items remain valid.
- Type versions SHALL be append-only and `restore` SHALL re-save a prior version under the same rules.
- The type SHALL be rendered as a read-only view whose edits are held as a type proposal, never applied.

#### Scenario: A compatible change touches no item
- **WHEN** a type gains an optional field
- **THEN** `diff` reports `compatible`, and the save records a new type version without rewriting any item

#### Scenario: A migrating change is atomic
- **WHEN** a type renames a field and makes another required with a default
- **THEN** every item of every collection of the type is rewritten with an audit effect in one transaction, or, if one item would fail, nothing changes and the failing item is named

#### Scenario: Stale declaration refuses
- **WHEN** a save carries a declaration hash from before another save of the same type
- **THEN** it refuses as stale and nothing changes

#### Scenario: Kind cannot be changed in place
- **WHEN** a proposal changes an existing type's kind
- **THEN** `diff` classifies it `refused` and the save refuses

### Requirement: Kind selects version semantics
Every change to an item SHALL be a new row version with one audit effect regardless of kind. The kind SHALL select:
- the effect label and history wording: `correction` for `observed`, `replan` for `intended`, `revision` superseding the previous revision for `procedural`, `edit` for `reference`, plus `transition`, `type_migration` and `view_move` for every kind;
- the default served version: for `procedural` and `reference`, the current version of items in served states only; for `intended`, items in active lifecycle states; for `observed`, current items newest-observation first.

A declaration's `surfacing` block SHALL be validated and stored. Serving declared types through kind-mapped compiler roles, and version-pinned links, are outside this change.

#### Scenario: A procedural edit is a revision
- **WHEN** a current recipe's steps are edited
- **THEN** the item's new row version is labelled a revision that supersedes the previous one, and only the new revision is served

#### Scenario: An observed edit is a correction
- **WHEN** a recorded execution's duration is changed
- **THEN** the new row version is labelled a correction of that observation, and storage, guards, audit and governance behave exactly as for the recipe

## MODIFIED Requirements

### Requirement: Human-owned collection manifests
The system SHALL represent each explicit structured collection with a human-readable Markdown manifest view at its `_collection.md` path under the governed Knowledge Base, whose canonical text and version history live in the collection store. The manifest SHALL carry a stable collection identifier, title, semantic profile, schema version, storage strategy and source, item-schema reference or inline schema, lifecycle, and optional templates, views, governance classification, and links. The manifest SHALL be the collection contract, not a copy of its items, and the `records` and `planning` profiles SHALL use this same contract. Editing the manifest view SHALL be a governed revision through the edited-view write path, never an out-of-band change.

#### Scenario: Manifest remains understandable without Exomem
- **WHEN** a user opens a collection manifest view in an ordinary editor
- **THEN** its identity, purpose, source, schema, templates, links, and storage strategy are readable without a plugin or database tool

#### Scenario: Unknown manifest version refuses safely
- **WHEN** Exomem encounters a manifest version or storage format version it does not support
- **THEN** it refuses mutation and reports the unsupported version without changing the stored manifest or items

#### Scenario: Duplicate collection identity is ambiguous
- **WHEN** two releasable live manifests declare the same stable collection identifier
- **THEN** discovery and mutation refuse the duplicate identity rather than choosing one by path order, while any withheld candidate remains indistinguishable from absence

#### Scenario: UUID discovery authorizes before parsing identity
- **WHEN** a caller resolves a collection UUID without supplying a manifest path
- **THEN** Exomem authorizes each candidate collection before its identity-bearing contents contribute to the result, and treats any derived catalog only as lookup acceleration

#### Scenario: Manifest view edit is a governed revision
- **WHEN** a user makes a valid edit to a manifest view
- **THEN** the store records a governed revise transition and a new manifest version, and an invalid edit becomes a held view correction

### Requirement: Guarded Markdown collection mutation
Append and targeted update for `markdown-log` and `markdown-items` collections SHALL accept structured item data rather than an arbitrary whole-file replacement. Mutations SHALL resolve their exact target inside one collection-store transaction under the existing same-vault writer lease, validate before writing, honor the expected container and item guards where applicable, and refuse stale or ambiguous targets. A caught error or process interruption SHALL roll the whole transaction back. Dataset mutation remains outside this delivery.

#### Scenario: Stale container hash refuses
- **WHEN** another transaction, including one applied from an edited view, committed on the collection after an agent read
- **THEN** a mutation carrying the prior expected container hash refuses as stale and leaves the store unchanged

#### Scenario: Stale item version refuses
- **WHEN** the intended item changed after an agent read even if its identifier still resolves
- **THEN** targeted update refuses as stale rather than overwriting the newer item

#### Scenario: Interruption leaves no partial mutation
- **WHEN** the process is interrupted during a mutation
- **THEN** after restart the store holds either the complete committed mutation with its transition or no trace of it

#### Scenario: Untouched log bytes are preserved
- **WHEN** one item block of a log-layout collection is appended or updated
- **THEN** the re-rendered log view keeps the frame outside the item section, including notation, legend, whitespace, UTF-8 BOM, final-newline state, and CRLF/LF style, byte-identical, and every other item block renders unchanged

#### Scenario: Windows replacement failure rolls back safely
- **WHEN** an open-file or case-insensitive-path collision makes a view replacement fail on Windows-compatible semantics
- **THEN** the committed store transaction is unaffected, the prior view file stays intact, and the view remains pending and is retried and reported

#### Scenario: Same-vault mutations serialize
- **WHEN** two cooperating agent mutations target the same vault concurrently
- **THEN** they serialize through the writer lease and the store transaction, and cannot publish a torn or silently lost update

#### Scenario: Separate vaults do not contend
- **WHEN** mutations target different vault roots
- **THEN** collection serialization does not introduce a global cross-vault lock

### Requirement: Idempotent append and conflict-safe update
The substrate SHALL make exact append retries idempotent where a stable item identity is available. When a caller omits the item identity and every field of the manifest's declared natural key is present in the validated values, the substrate SHALL derive the identity deterministically from the collection identity and the natural-key serialisation the read path already uses, and stamp it explicitly like any other item key; an explicit identity SHALL still win, and a payload that lacks a natural-key field SHALL receive a random identity as before. Reusing one identity with different content SHALL refuse as an identity conflict. An append whose derived or supplied identity differs from an existing item's while its serialised natural key equals that item's SHALL refuse as a natural-key conflict naming every such existing item; the store's natural-key uniqueness constraint SHALL enforce this. Targeted update SHALL change only the resolved item and SHALL never fall back from a missing identifier to fuzzy text matching. A mutation carrying a transport request identity SHALL record that identity and its terminal receipt in the same transaction as the write; a retry with the same identity SHALL return the recorded receipt without writing, and the same identity with a different request SHALL refuse. Both profiles SHALL inherit these rules through the shared mechanics.

#### Scenario: Exact append retry produces one item
- **WHEN** a client retries the same append with the same collection, item identity, and normalized payload
- **THEN** the substrate returns the committed item without adding a duplicate

#### Scenario: Re-stated append without identity replays
- **WHEN** a client appends the same observation twice without supplying an item identity and the payloads are identical
- **THEN** the second append returns the committed item as a replay and the collection holds one item

#### Scenario: Reused identity with different content refuses
- **WHEN** an append supplies an existing item identity with materially different content, or omits the identity and the derived identity already exists with different content
- **THEN** it refuses with a record identity conflict and preserves the existing item

#### Scenario: Natural-key twin of an older item refuses
- **WHEN** an append's natural-key values equal those of an existing item whose identity was minted before derivation existed
- **THEN** it refuses with a natural-key conflict that names the existing item and writes nothing

#### Scenario: Planning titles stop duplicating
- **WHEN** a Planning collection declares `[title]` as its natural key and an agent adds a work item whose title already exists
- **THEN** the add replays when the payload is identical and refuses otherwise, so the agent updates the existing item instead of filing a twin

#### Scenario: Missing identifier does not select a similar item
- **WHEN** targeted update names an identifier that no longer exists
- **THEN** it refuses as missing or stale and does not update an item with similar text or fields

#### Scenario: Commit before a lost response replays exactly once
- **WHEN** a mutation commits and the process stops before the transport ledger records the result, and the client retries with the same request identity
- **THEN** the store returns the recorded receipt and no second transition exists

### Requirement: Auditable agent mutations and receipts

Every agent mutation SHALL require a concise reason and SHALL return a bounded receipt containing collection identity, item identity (null for create and lifecycle operations), operation-specific required before and after item/manifest/container hashes, affected view paths, committed outcome, and a transition correlation. The receipt SHALL be recorded with the transition in the collection store. Each collection SHALL keep a reader version of 1 for collections whose history contains only create/append/update transitions, and 2 once a `revise` or `rebaseline` has committed, and every later mutation SHALL preserve version 2. The reader version SHALL be reported as today, but no manifest marker or item audit marker SHALL be written.

The collection store SHALL hold exactly one audit transition per committed mutation, as specified by the audit-table requirement. Transitions imported from the legacy activity log SHALL be kept verbatim and chained ahead of native transitions. Inspection SHALL verify the transition hash chain from the collection's audit head, and SHALL distinguish `baseline`, `ok`, positive `gap`, bounded `history_incomplete`, and `acknowledged_gap`. For store-native history, `gap` and `acknowledged_gap` SHALL arise only from imported legacy history, and `history_incomplete` only from governance. Inspection SHALL never repair or invent history. Operational journals and governance receipts SHALL retain their existing distinct roles.

#### Scenario: Successful normal update records one audit event
- **WHEN** a guarded item update commits
- **THEN** the response includes the transition correlation and hashes, and the store holds exactly one matching transition and effect

#### Scenario: Failed validation records no committed audit event
- **WHEN** schema validation or a guard refuses a mutation
- **THEN** no item change and no transition is written

#### Scenario: Abrupt interruption exposes an audit gap
- **WHEN** a file-canonical collection was interrupted before migration after an item replacement but before its activity-log event, and is then imported
- **THEN** the imported history reports that positive gap, and neither import nor the store invents or hides an event; after migration an interruption can no longer separate a change from its transition

#### Scenario: Pre-publication interruption leaves no canonical-looking scaffold
- **WHEN** a simulated `BaseException` interrupts collection creation
- **THEN** the store transaction rolls back, no manifest view, directory, or staging file is left behind, the original exception is re-raised unchanged, and an exact retry is not wedged by tool-owned residue

#### Scenario: Human audit-mapping reformat remains mutable
- **WHEN** a migrated manifest view still carries, or a user reformats, a legacy `record_audit` or `plan_audit` mapping
- **THEN** edit-back treats it as formatting, no transition is recorded for it, the next render omits it, and later mutations proceed

#### Scenario: Normal mutation after revision preserves the reader floor
- **WHEN** a collection commits a revise transition and later an append or update
- **THEN** the reported reader version remains 2 and inspection reports the same healthy history before and after restart

#### Scenario: Normal mutation after rebaseline preserves discontinuity
- **WHEN** a collection with an imported legacy gap commits a rebaseline and later an append or update
- **THEN** the reported reader version remains 2, and inspection before and after restart reports `acknowledged_gap` with the same permanent discontinuity

#### Scenario: Successful Planning triage uses the shared audit engine
- **WHEN** a guarded Planning triage mutation commits
- **THEN** the response and the store's transition carry one matching Planning-profile transition, with no `record_audit` or `plan_audit` property written anywhere

### Requirement: Manual-edit visibility and report-only inspection
Queries SHALL read the collection store. A direct edit of a view SHALL become visible only through the edited-view write path: as a governed transition once applied, or as a held view correction. Collection inspection SHALL report pending views, held view corrections, held candidates and coverage counts, schema violations, missing templates, imported legacy audit gaps, and stale saved-view provenance, without rewriting the store or any view, and without counting held candidates or corrections as items. Generic derived-index repair SHALL remain owned by `maintain_memory(mode="reconcile", dry_run=false)`.

#### Scenario: Direct edit appears on next query
- **WHEN** a user changes a valid item view in an ordinary editor
- **THEN** the next query after the settle window reflects the change, and the collection history shows the view-edit transition

#### Scenario: Inspect reports but does not repair canonical ambiguity
- **WHEN** a user copies an item view so that two files carry the same item identity, or makes an edit that would collide with another item's natural key
- **THEN** the store is unchanged, `record_memory(action="inspect")` reports the held view correction with its reference and diagnostics summary, nothing is repaired automatically, and `maintain_memory` may repair only derived indexes

#### Scenario: Inspect reports an undeclared manual field
- **WHEN** a human adds a property that is not declared by the collection schema to an item view
- **THEN** the row is unchanged and inspection reports a held view correction naming the field

#### Scenario: Inspection reports a held candidate without adopting it
- **WHEN** a collection has a held candidate
- **THEN** inspection reports it under coverage with its reference and diagnostics summary, the item count excludes it, and nothing is rewritten

### Requirement: Separate semantic profiles over shared mechanics
The collection substrate SHALL keep collection mechanics independent from semantic meaning. Every collection type, built-in or declared, SHALL reuse identity, schema, storage, mutation, query, audit, rendering, and edit-back mechanics through one type-neutral implementation rather than a fork. `records` SHALL be the built-in type of kind `observed`, meaning observed facts or events, and `planning` the built-in type of kind `intended`, meaning intended future state.

Every manifest and view of a collection SHALL be contained by the exact portable `Knowledge Base/<placement>/` path segments of its type: `Records` for `records`, `Planning` for `planning`, and the declared placement for a declared type. These placement rules SHALL be checked after symlink-safe resolution and make structured-only recall classification deterministic. They do not constrain ordinary templates or links to governed artifacts elsewhere in the vault. A typed facade SHALL refuse collections of another facade's type.

#### Scenario: Future Planning manifest resolves through the same loader
- **WHEN** a valid manifest declares `semantic_profile: planning` or `collection_type: planning`
- **THEN** the generic collection loader inspects its identity, schema, storage, links, templates, and views through the same contracts used for every type, without applying Records semantics

#### Scenario: Records operations do not mutate Planning
- **WHEN** a Planning collection is supplied to the Records facade's query, create, append, or update path
- **THEN** the Records facade refuses the operation, leaving the store and Planning views unchanged

#### Scenario: Planning operations do not mutate Records
- **WHEN** a Records collection is supplied to the Planning facade's query, create, add, update, or triage path
- **THEN** the Planning facade refuses the operation, leaving the store and Records views unchanged

#### Scenario: Unknown profile does not become Records
- **WHEN** a manifest names a collection type or semantic profile that is not registered
- **THEN** the substrate reports it as unsupported and does not silently apply Records, Planning or any other type's semantics

#### Scenario: Records source outside the Records layer refuses
- **WHEN** a manifest or view path of any type resolves outside the exact placement layer of its type through case, separator, dot-segment, or symlink aliases
- **THEN** validation refuses before reading item contents

## REMOVED Requirements

### Requirement: Three portable canonical storage strategies

**Reason**: Markdown logs and Markdown item files are no longer canonical for Records and Planning; the recursive file-inventory snapshot and its census guard are deleted.

**Migration**: Replaced by "Declared storage strategies are view layouts over the collection store"; datasets are unchanged.

### Requirement: Valid out-of-band edits can be explicitly rebaselined

**Reason**: View edits now become governed transitions or held corrections, so out-of-band edits can no longer create store-native audit gaps.

**Migration**: Replaced by "Imported legacy audit gaps can be explicitly rebaselined", with the same arguments, digests and v2 receipt, for gaps imported from file-canonical history.
