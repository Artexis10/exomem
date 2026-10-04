## MODIFIED Requirements

### Requirement: One embedded collection store is the single source of truth

Older-reader admission and replication custody SHALL use the supported-client
boundary in Collection store snapshots are consistent and portable and
Persistent machine-local state lives outside the vault. Supported launchers
SHALL derive required compatibility from the existing authority marker before
fresh bootstrap, candidate admission or copied/restored-vault activation.
Unknown custody SHALL leave store activation/publication unavailable while
file collections and knowledge remain usable. No guarantee SHALL be inferred
for arbitrary manually launched historical executables or unmanaged sync
programs outside that boundary.

Each vault SHALL have exactly one embedded SQLite collection store that is the only canonical source for structured collections assigned store authority by Collection store migration is verifiable and reversible, of every collection type, built-in (Records, Planning) or declared: their type declarations, manifests, items, item versions, per-row provenance, held candidates, and audit transitions. Existing file-authoritative collections SHALL remain canonical in files until their declared migration; the new-collection slice SHALL NOT copy or redirect them into the store. The store SHALL enforce collection-scoped item identity and declared natural-key uniqueness with database constraints, SHALL commit every mutation, including every row of a bulk mutation, in one transaction under the existing single-writer lease, and SHALL make its transaction, audit-effect, item-version, provenance and manifest-history tables append-only. Canonical values MAY use json-v1 or migration-owned typed-v1 encoding; a stable append-only version_identity spine SHALL identify every (row_id,row_version,encoding,payload_hash,txn_id,schema_version), and per-version sources SHALL reference that spine. Typed history and new identity tables SHALL have BEFORE UPDATE and BEFORE DELETE abort triggers. The forward schema SHALL preserve existing immutable JSON history, source refs, hashes and audit, require exact logical parity before mapping publication and fence every older reader before access. Typed dense rows SHALL NOT keep a duplicate full canonical JSON payload. Knowledge notes, entities, sources, evidence and episodes SHALL remain Markdown and SHALL NOT be stored in it. The `dataset` storage strategy SHALL remain a file-canonical, query-only adapter and SHALL NOT be imported into the store. Records, Planning and declared types SHALL keep their distinct kinds, typed schemas, natural keys, provenance, audit and governance; only the storage engine changes. The store SHALL require SQLite 3.38 or newer and a local filesystem where WAL journaling takes effect, and readiness SHALL refuse store-authoritative collection writes, never falling back to file-canonical writes, when either fails.

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

#### Scenario: Typed version keeps its source identity
- **WHEN** an item changes from a JSON-backed version to a typed-backed version through a forward migration and a later governed update
- **THEN** both versions retain exact values/hash/transaction/source references through version_identity, missing-version source inserts fail their FK, and updates/deletes of either history encoding abort


### Requirement: Collection views are Markdown projections of the store
For `view_mode: items`, the following per-item/log rendering, stamping, acknowledgement, publication and reconciliation guarantees and inherited scenarios SHALL remain unchanged. The substrate SHALL render every collection manifest, item, log-layout collection, and held candidate as a Markdown view at the same vault-relative path the file-canonical layout used, using the existing item, log, filename and managed-presentation renderers and keeping the visible system identity properties. Views SHALL NOT be canonical. Every item and manifest view, and every log block, SHALL carry a view stamp naming the store identity, the rendering store instance, the row version and a payload-hash prefix. The stamp SHALL be a reserved system property that callers cannot write and the payload hash ignores.

The item, manifest and held views changed by a mutation SHALL be staged and fsynced inside the mutation's transaction and published after commit, before the mutation is acknowledged, so that any reader of the vault file sees the acknowledged write. A view SHALL never be published ahead of its committed row. Every publish, synchronous or asynchronous, SHALL be check-then-swap:
- move the current view aside atomically;
- install the staged view without replacing any file that appeared in between;
- hash the aside copy, and hold it as `VIEW_CONFLICT` with its bytes when it differs from the view's expected on-disk hash.

A publish SHALL NEVER overwrite a view edit the substrate has not classified. On every start and periodically, the substrate SHALL hash every view path before the projector runs, and SHALL send mismatches to edit-back classification, so detection does not depend on a file-system watcher. The staged view's hash SHALL be recorded inside the mutation's transaction, adding no second commit to the acknowledgement path. Log-layout views, history pages and type views SHALL be published asynchronously with bounded lag. Every view's pending state SHALL be recorded durably in the same transaction as the mutation, so that a crash after commit re-renders it on restart. A projection failure SHALL NOT turn a committed mutation into a refusal: it SHALL be retried, and reported as a receipt warning and in inspection.

Index synchronization of views SHALL NOT be on the acknowledgement path. Structured readers SHALL read the store rather than views. Vault file tools SHALL refuse to delete, move or recover a path owned by a collection view, with a remediation naming the collection operation. Views SHALL be normalized on re-render: values and the authored body SHALL be emitted exactly, while YAML formatting that is not data need not be preserved. Audit markers and manifest audit heads SHALL NOT be rendered.

For `view_mode: summary`, the substrate SHALL render the manifest and at most 16 readable summary/rollup pages of at most 64 KiB each and 1 MiB total, with no per-row item file, staging or fsync. Each page SHALL label collection, metric/window/source, released query basis and completeness. A summary mutation SHALL atomically commit values, version/source/audit, import checkpoint when applicable and durable pending page basis; changed manifest/held views SHALL retain their inherited publication guarantee. Summary pages SHALL publish asynchronously through the same no-clobber, hash/classify and durable retry mechanism and SHALL never claim current complete data while pending/stale. A committed publication failure SHALL be an acknowledged receipt warning and visible pending state, not a refusal. Reconcile SHALL scan only declared bounded page paths. Structured row tools SHALL read canonical rows and authorize by logical collection/item identity and the manifest subject without requiring a fictitious file path. Items-mode physical view paths SHALL remain unchanged; summary rows MAY have no physical view path.

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

#### Scenario: Summary rows do not create item files
- **WHEN** a million-row summary collection is imported and acknowledged
- **THEN** no per-row files or per-row file-sync work exist, the bounded manifest/pages remain readable, and row reads and edits use governed tools over canonical store rows

#### Scenario: Summary acknowledgement survives projection failure
- **WHEN** a batch commits but summary-page publication fails or the process stops
- **THEN** the committed rows/history/audit/checkpoint remain acknowledged once, the receipt/inspection reports pending projection and restart retries the bounded pages without claiming stale pages are current


### Requirement: Edited views return through the governed write path
For `view_mode: items`, the following guarded item/log edit-back rules and inherited scenarios SHALL remain unchanged. When a view file changes and its bytes differ from the last published or pending bytes, as found by the file watcher or the reconcile on the writer-lease holder, the substrate SHALL classify the view stamp first:
- a stamp from another store or from an instance outside this store's lineage SHALL set `COLLECTION_STORE_DIVERGED` and be held `VIEW_FOREIGN`;
- a stamp older than the item's current row version SHALL be held `VIEW_CONFLICT`;
- a stamp whose payload-hash prefix does not match the row SHALL be held `VIEW_INVALID`;
- only a view whose bytes are exactly the bytes parsed at import MAY be unstamped, and any other unstamped view SHALL be held `VIEW_INVALID`.

For a view stamped with the current row version, it SHALL, after the file has been quiet for a settle window, classify the edit deterministically without interpreting prose. A formatting-only edit SHALL be re-rendered without a transaction. A valid change to declared values or body, made against the item's current row version, SHALL be applied as an ordinary governed `update` (or Planning `update` or `triage`) through the same leaf functions, validation, governance precommit, audit transition and receipt as a tool call, with actor `owner:view-edit`, a reason naming the view, and a request identity derived from the view path, base row version and file hash so that a replay is a no-op; the view SHALL then be re-rendered. A valid manifest edit SHALL be applied as a governed `revise`. An edit that is ambiguous, schema-breaking, violates Planning lifecycle or hierarchy rules, changes a system property, conflicts with a newer row version, deletes a view, moves a view out of its collection's source root, adds an unbound file under a projection root, or is a file-sync conflict copy SHALL become a held view correction carrying the human's bytes and field-addressed diagnostics, and SHALL NEVER silently overwrite or discard either the store row or the human's edit. A move of a view within its collection's source root with an intact current stamp SHALL be a governed `view_move` transition that updates the item's view path, re-authorized for the new path, and SHALL NOT re-render the old path. A later valid edit of the same view SHALL supersede its held correction. History pages SHALL be read-only: edits to them SHALL be ignored and re-rendered.

Summary pages SHALL be generated read-only outputs, never a batch row-edit language. Edits SHALL be classified/held with original bytes and diagnostics before no-clobber re-render, without changing canonical rows or inferring deletes. Summary manifest edits SHALL still use governed revise with current stamps/guards. Individual summary rows SHALL be updated through the same Records/Planning governed tool leaf, schema/lifecycle validation, complete-authorized-state checks, row-version guards, audit and receipts as items-mode rows.

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

#### Scenario: Summary edit-back cannot invent row changes
- **WHEN** a user changes or deletes a number or row label in a summary page
- **THEN** the edit's bytes are held and reported, canonical rows are unchanged and the bounded page is re-rendered; a subsequent authorized tool update changes only the guarded target row


### Requirement: Declared storage strategies are view layouts over the collection store
The substrate SHALL support three declared storage strategies. For `markdown-log` and `markdown-items` the strategy SHALL name the layout of the collection's Markdown views, one chronological log file of item blocks or one Markdown file per item, while the canonical data of both SHALL live in the collection store. The `dataset` strategy (CSV, TSV, or JSON) SHALL remain file-canonical and query-only. Any cache, index, export, summary, replica, or generated view SHALL be derived from the canonical source it represents.

Chronological-log child rows SHALL declare a bounded `container_field` in addition to their delimiter and fields; that container SHALL be a declared array-of-object item-schema field, so adapters do not impose domain field names. For the log layout, the store SHALL keep the view's frame, meaning the bytes outside the declared item section, its UTF-8 BOM, its newline style and its final-newline state, and SHALL re-emit them byte-identically. Markdown parsing of views and legacy files SHALL accept exactly one leading UTF-8 BOM for frontmatter parsing. A log-layout view SHALL stay within its rendering size cap, and a write that would exceed it SHALL refuse with a remediation to use the items layout.

Store-backed collections SHALL declare `view_mode: items|summary`, defaulting to items. `items` SHALL retain the parent's 100,000-row cap and existing item/log representation, acknowledgement and edit-back guarantees, including the 2 MB log rendering cap. `summary` SHALL have no file-derived row limit and SHALL be bounded by store capacity, available space, declared index budgets and admitted query/import resources; describe SHALL identify that mode and its known bounds without an unmeasured capacity claim. Large observed collections SHALL use summary. SQLite SHALL be authoritative for store-backed Records and Planning in either mode and Markdown SHALL be generated views. The dataset strategy SHALL remain file-canonical/query-only and SHALL NOT gain summary mutations. Creating the first NEW built-in Records summary collection SHALL NOT migrate existing Records.

V1 SHALL refuse a change between items and summary for any populated collection with `VIEW_MODE_CHANGE_UNSUPPORTED`, through tool revise, manifest edit-back, declaration restore and migration entry points, before changing its manifest, authority, mapping, guards, cursor basis or files. Populated SHALL include retained current/lifecycle rows or item history; retiring all items SHALL NOT make a collection empty. No authority or file ownership cutover SHALL occur: pending publications, held corrections, offline edits and former view paths SHALL remain under their existing classification/reconcile rules. Parent GA proofs SHALL NOT enable an in-place mode conversion in v1. To migrate, the owner SHALL create a NEW collection in the target mode and explicitly import/copy authorized current values with source provenance and independently verified parity, subject to target capacity (including the items 100,000-row cap); identities belong to the new collection and old references SHALL NOT be silently retargeted. The original collection, files, history, pending publications and held corrections SHALL remain intact; held/unclassified edits SHALL NOT be silently imported as accepted rows or discarded. V1 mode conversion of a populated collection is deferred. An empty collection MAY revise mode only with no item history, pending publication, held correction or unclassified edit, under the ordinary guarded manifest revision.

#### Scenario: Log layout renders one readable history file
- **WHEN** a log-layout collection is queried or safely mutated
- **THEN** the store is canonical and its log view shows every item as a readable block in declared order, and no generated dataset is promoted implicitly

#### Scenario: File-per-item collection uses ordinary properties
- **WHEN** an items-layout collection stores a record
- **THEN** the record's view is an ordinary Markdown file with stable item and collection identifiers plus typed YAML properties and an optional readable body

#### Scenario: Dataset stays directly editable
- **WHEN** a dataset-backed collection uses CSV, TSV, or JSON
- **THEN** its rows remain readable and editable with ordinary tools, Exomem queries them from the file, and dataset append/update refuses rather than reserializing the file

#### Scenario: Summary capacity is bounded by the store
- **WHEN** a summary collection imports the 100,001st distinct natural key within measured store/resource bounds
- **THEN** no items-mode row-limit refusal or per-row file requirement applies, while the same attempt in items mode refuses at its 100,000-row cap

#### Scenario: Items mode keeps its original contract
- **WHEN** an existing collection omits view_mode or explicitly declares items
- **THEN** its row/log limits, per-item rendering/stamping, acknowledgement and guarded edit-back remain the inherited contract

#### Scenario: Populated items cannot become summary
- **WHEN** tool revise or offline manifest edit-back requests summary for an items collection with rows, an offline item edit, pending publication or held correction
- **THEN** it refuses VIEW_MODE_CHANGE_UNSUPPORTED before any cutover, leaves rows/history/manifest/mapping/guards/cursors/file ownership unchanged, and classifies the original edits and publishes pending views under the items contract

#### Scenario: Populated summary cannot become items
- **WHEN** a summary collection with 100,001 rows requests items, including while a summary-page edit or guarded row update is pending
- **THEN** it refuses VIEW_MODE_CHANGE_UNSUPPORTED before any cutover or file materialization, retains the summary authority and bytes, and subsequent page classification/tool guards behave exactly as before the refusal

#### Scenario: Mode migration creates a separate collection
- **WHEN** the owner explicitly copies accepted current data into a NEW collection of the other mode
- **THEN** target validation/capacity and independent value/source parity apply, target failure leaves the original rows/files/history/pending and held state intact, and neither success nor failure retargets old identities or disposes of original files


### Requirement: Collection store snapshots are consistent and portable
The live store's files SHALL NEVER be copied by file-level backup or export. The substrate SHALL produce consistent snapshots of the collection store only through the SQLite online backup API into a staging file that is switched to a single-file journal mode, integrity-checked and atomically renamed. It SHALL publish such a snapshot as a replica inside the vault after committed transactions, coalesced off the acknowledgement path, and synchronously on quiesce, writer-lease release, shutdown, upgrade handoff and portability export. The replica SHALL NEVER be opened for writing in place. A replica SHALL be adopted only when the single vault-side mode marker authorizes that same store_id, either as the default store authority after general migration or through explicit store-authoritative collection entries in mixed mode. Adoption SHALL load only those store-authoritative entries; a replica SHALL NOT promote file-authoritative collections. The marker and replica SHALL be backed up/exported as one validated routing basis; restore SHALL refuse store access when their identity/authority epoch or referenced committed head is incompatible, rather than falling back to files.

Every transaction SHALL advance a store-wide sequence and chained head hash. The writer-lease holder SHALL report `(store_id, instance_id, commit_seq, head_hash)` to the coordinator on renew and release. A new holder facing a head recorded by another instance SHALL adopt the replica only once it reaches that head, and until then SHALL refuse collection writes with the retryable `COLLECTION_STORE_SYNC_PENDING`, naming both sequences and the remedy. A store for which no foreign head was ever recorded SHALL never wait. An owner-only, preview-first `adopt-local` operation SHALL let the holder continue from local state, recording the fork point, and the other side's later-arriving changes SHALL be reconciled as held corrections.

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

#### Scenario: Mixed-authority takeover uses the store head
- **WHEN** file-authoritative Records A and Planning B coexist with store-authoritative summary C and another host acquires the lease
- **THEN** A/B still read and write their original files under the shared lease, C adopts only the marker-authorized store after the replica reaches the coordinator head, a lagging replica refuses C writes with COLLECTION_STORE_SYNC_PENDING, and no host imports A/B or opens the replica for writing


### Requirement: Collection store migration is verifiable and reversible
Existing file-canonical Records and Planning collections SHALL move to the store only through the declared offline general migration that imports every such collection and proves a round trip before changing their authority. Separately, the owner-approved S1 slice MAY create a NEW built-in Records summary collection with store authority while all existing Records and Planning collections retain file authority; this SHALL NOT require or imply general migration GA. The proof requires all of the following: item counts equal; every row, rendered and parsed back, yields equal values, body, identity and natural key; payload hashes equal the legacy derivation; the imported legacy audit chain has the same head and length; manifest text is byte-equal; and the legacy audit status is preserved, never upgraded. Import SHALL rewrite no vault file and SHALL record the current file bytes as the current views. A vault with duplicate identities, schema violations or unsupported versions SHALL NOT migrate until they are fixed. The vault's collection routing and shared-store adoption SHALL have one authority: a versioned vault-side mode marker that every reader, writer, discovery path, restart, backup/restore and adopter reads. With no marker, legacy collections remain file-authoritative. The marker SHALL name default_authority (file before general migration, store after it), store_id when any collection is store-authoritative, an authority epoch, and explicit entries binding collection_id and manifest path to authority: store and that store_id for NEW collections in mixed mode. Every unlisted existing collection in mixed mode SHALL route to files; datasets SHALL always remain file/query-only. Store registry entries and view stamps SHALL be checked against this marker, never used as another routing authority. A marker-authorized store collection SHALL refuse unavailable/diverged/unsupported store access, never fall back to its generated Markdown views. Caller arguments and manifest edits SHALL NOT change this routing basis. On a multi-host vault, migration SHALL refuse while the lease coordinator does not explicitly advertise `collections-store-v1`, with a message naming the exact release to upgrade the coordinator to. Store adoption SHALL advance a separate vault-specific `collections-store-v1` capability fence with generation CAS and lease-token invalidation in one coordinator transaction, reacquire before marker cutover, and record an optional state compatibility descriptor so older releases refuse before access. Governance versions 3/4 and file-only manifests SHALL remain unchanged; ignoring extra request fields SHALL NOT establish coordinator capability. The importer SHALL read legacy audit history with an uncapped streaming reader filtered per collection. It SHALL record each view's expected hash as the hash of the exact bytes it parsed. Non-managed installs SHALL migrate through an offline command with the same proof. The migration SHALL run under the managed standby-upgrade handoff: pre-import on the standby without ownership, re-verification of only the collections changed since pre-import (carrying forward the proofs of unchanged ones) within the cutover budget, abandoning the upgrade cleanly when it cannot, and atomic publication, so that writes pause only for the ordinary bounded handoff. The substrate SHALL provide a preview-first reverse export that renders the store into the legacy file layout with a content-free checkpoint transition per collection, so the legacy inspector reports `acknowledged_gap` for any collection written in store mode. Export SHALL set the mode marker to exported and tombstone the replica so no host can adopt it. Downgrade to a pre-store release SHALL require export first.


S1 SHALL implement the parent P1b.5 consistent snapshot/replica publisher and synchronous quiesce/release/shutdown/handoff/export flush, P1b.6 chained-head coordinator exchange/store-capability fence, lineage/takeover/divergence/adopt-local held-reconciliation, and applicable P1b.7 hosted snapshot export/staged restore, together with the schema/state compatibility and store-capability admission. These are shared safety prerequisites before S1 create or external access, not tests of functionality assumed to exist in P1a. Store-blind readers/writers SHALL refuse this mixed-mode vault at startup/lease acquisition, including on another host; the coordinator upgrade prerequisite SHALL apply to S1 without migrating legacy collections. General existing-file importer/reverse-export/GA work in P1b/P2/P3 remains deferred.

Within the serialized create/recovery operation, holding valid writer authority for each mutating phase and reacquiring it after any capability-fence cut, NEW collection creation SHALL prepare its complete store metadata/audit, durable publication intent, schema/state compatibility and separate store-capability fences and integrity-checked replica before atomically publishing the marker's new collection entry/epoch. The marker rename SHALL be the single routing cutover: before it A/B remain files and C is undiscoverable; after it C resolves only to the committed store and A/B still resolve to files. No cross-file/SQLite atomic transaction is claimed. A failure before store commit SHALL roll back without a manifest/directory/staging scaffold or routing change. A crash after commit but before marker publication SHALL recover the durable intent under the lease before admitting C access; exact retry SHALL complete the same creation once, never invent a second identity or discard committed history. A post-commit publication error SHALL report pending/unavailable with the recoverable receipt, never a failed mutation or file fallback. Generated manifest publication after cutover SHALL follow the inherited pending/no-clobber rules. Restore/copy SHALL validate marker, replica, schema and head together before routing C, and preserve A/B's file bytes and legacy audit/guards.

Slice rollback SHALL pause imports and disable records-summary-v1 reads for C while retaining its store-authoritative marker entry, canonical rows/history, preserved source and replica. It SHALL NOT set the whole vault to exported/file mode or remove C's routing entry. A/B continue their file-authoritative paths. Pre-GA re-import SHALL use a NEW explicitly authorized collection when necessary; incompatible binary downgrade SHALL require a compatible reader or validated pre-slice backup, never treat summary pages as canonical files.

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

#### Scenario: Coexisting file and store collections keep one authority each
- **WHEN** the owner creates summary Records C in a real vault containing file-authoritative Records A and Planning B
- **THEN** reads and guarded writes of A/B use unchanged legacy files/audit, C uses the external store, view edits cannot promote A/B or demote C, and a missing/unavailable C store refuses rather than reading its views

#### Scenario: Failed new-store create leaves routing unchanged
- **WHEN** creation of C fails before store commit or is interrupted after commit on either side of the marker rename
- **THEN** precommit failure leaves no C scaffold/entry, pre-cutover interruption exposes no C and recovers the same durable creation once, post-cutover interruption routes C only to its committed store with honest pending publication, and A/B authority and bytes never change

#### Scenario: Restart and restore preserve mixed authority
- **WHEN** a mixed A/B/C vault restarts or its consistent marker/replica and file backup is copied/restored on a host with no local store
- **THEN** A/B retain file reads/writes and original guards/audit, C is adopted from its identity/head-validated replica and accepts governed store reads/writes, mismatched/tombstoned snapshots refuse C access, and a supported launcher rejects an older reader before access even with fresh external state while a store-blind coordinator refuses lease acquisition

#### Scenario: Slice rollback preserves mixed authority
- **WHEN** records-summary-v1 is disabled after C has acknowledged imported rows
- **THEN** C jobs pause and slice reads return unavailable, its marker/rows/history/source/replica remain intact for authorized compatible resume, A/B reads/writes continue in files, and no whole-vault export or implicit re-import occurs
