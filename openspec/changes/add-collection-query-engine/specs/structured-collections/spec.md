## MODIFIED Requirements

### Requirement: One embedded collection store is the single source of truth
Each vault SHALL have exactly one embedded SQLite collection store that is the only canonical source for structured collections of every collection type, built-in (Records, Planning) or declared: their type declarations, manifests, items, item versions, per-row provenance, held candidates, and audit transitions. The store SHALL enforce collection-scoped item identity and declared natural-key uniqueness with database constraints, SHALL commit every mutation, including every row of a bulk mutation, in one transaction under the existing single-writer lease, and SHALL make its transaction, audit-effect, item-version, provenance and manifest-history tables append-only. Canonical values MAY use json-v1 or migration-owned typed-v1 encoding; a stable append-only version_identity spine SHALL identify every (row_id,row_version,encoding,payload_hash,txn_id,schema_version), and per-version sources SHALL reference that spine. Typed history and new identity tables SHALL have BEFORE UPDATE and BEFORE DELETE abort triggers. The forward schema SHALL preserve existing immutable JSON history, source refs, hashes and audit, require exact logical parity before mapping publication and fence every older reader before access. Typed dense rows SHALL NOT keep a duplicate full canonical JSON payload. Knowledge notes, entities, sources, evidence and episodes SHALL remain Markdown and SHALL NOT be stored in it. The `dataset` storage strategy SHALL remain a file-canonical, query-only adapter and SHALL NOT be imported into the store. Records, Planning and declared types SHALL keep their distinct kinds, typed schemas, natural keys, provenance, audit and governance; only the storage engine changes. The store SHALL require SQLite 3.38 or newer and a local filesystem where WAL journaling takes effect, and readiness SHALL refuse collection writes, never falling back to file-canonical writes, when either fails.

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

Store-backed collections SHALL declare `view_mode: items|summary`, defaulting to items. `items` SHALL retain the parent's 100,000-row cap and existing item/log representation, acknowledgement and edit-back guarantees, including the 2 MB log rendering cap. `summary` SHALL have no file-derived row limit and SHALL be bounded by store capacity, available space, declared index budgets and admitted query/import resources; describe SHALL identify that mode and its known bounds without an unmeasured capacity claim. Large observed collections SHALL use summary. SQLite SHALL be authoritative for Records and Planning in either mode and Markdown SHALL be generated views. The dataset strategy SHALL remain file-canonical/query-only and SHALL NOT gain summary mutations. A view-mode revision of existing rows SHALL require an explicit governed migration and existing parent GA proofs; creating the first NEW built-in Records summary collection SHALL NOT migrate existing Records.

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
