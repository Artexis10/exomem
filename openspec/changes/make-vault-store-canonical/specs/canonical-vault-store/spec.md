## ADDED Requirements

### Requirement: One canonical store per vault
In store mode, each vault SHALL have one SQLite database, the per-vault collection store database, that is the single source of truth for every text file a governed write commits: pages, navigation indexes, logs, compiled history and the governed schema overlay. The database SHALL hold each such file's exact bytes, its parsed frontmatter, the structured collection tables, the lexical catalogue rows, the epistemic graph rows, an audit trail and a projection manifest. Binary media SHALL stay files that the store references by path and content hash. Dataset files that the structured-collections contract keeps file-canonical, and vault content outside the managed root, SHALL stay outside the store.

#### Scenario: A page has one authoritative copy
- **WHEN** a reader asks for a page in a store-mode vault
- **THEN** Exomem answers from the store's bytes for that page
- **AND** no answer depends on the Markdown file on disk

#### Scenario: Binary media stays on disk
- **WHEN** a governed write preserves a binary Evidence file
- **THEN** the file stays on disk and is durable before the transaction that records its path and content hash commits
- **AND** the store records that path and hash in the same transaction as the page that cites it

#### Scenario: A governed move of a binary renames it, then records it
- **WHEN** a delete, a move or a recovery in store mode moves a binary file
- **THEN** the file is renamed on disk first, and one transaction records it at its new path and removes its old record, bound to the old record as read and to an empty new path
- **AND** a refused transaction renames the file back, and a kill before the transaction leaves the old record to the next sweep, which pairs it with the moved file by its hash

### Requirement: One transaction per governed write
A governed write in store mode SHALL commit every file it changes, the lexical catalogue rows, the graph nodes, edges and dependencies derived from those files, one audit row per file and one projection intent per file in one `BEGIN IMMEDIATE` transaction. The write SHALL acknowledge only after that transaction is durable. Derivation SHALL run from the bytes being written, before the transaction, with the same derivation code that file mode uses. When a write adds a title that makes links on other pages resolve, the same transaction SHALL update those pages' graph rows.

#### Scenario: Search and graph see the write at acknowledgement
- **WHEN** a governed write that adds a page and a link returns success
- **THEN** a lexical search for a term in the page returns the page
- **AND** the graph returns the new edge from that page
- **AND** neither depends on a background drain, watcher event or repair

#### Scenario: A hard kill leaves no half-applied write
- **WHEN** the process is killed at any point while a governed write runs
- **THEN** after restart every file of that write shows either all of its old state or all of its new state, in page bytes, lexical rows and graph rows together
- **AND** no repair, rebuild or reconcile is needed to make the derived rows match the page bytes

#### Scenario: A new title resolves earlier links in the same transaction
- **WHEN** a governed write creates a page whose title other pages already link to
- **THEN** at acknowledgement the graph holds edges from those pages to the new page

#### Scenario: Derived rows match file mode exactly
- **WHEN** the same sequence of page bytes is written once in file mode and once in store mode from the same starting vault
- **THEN** the lexical page rows, semantic unit rows, graph nodes, graph edges, graph dependencies and parent references are equal, apart from columns that record write time or write order

### Requirement: Markdown is a byte-exact projection
In store mode, Exomem SHALL write each committed file to the vault as a byte-exact projection after the transaction commits. A projector SHALL make the projected file durable before it records the file's hash, size, modification time and revision in the projection manifest. At startup the projector SHALL remove its own unfinished temporary files and SHALL project every intent still queued. Several revisions of one file that queue before projection MAY coalesce into the latest one.

#### Scenario: Projection catches up after a kill
- **WHEN** the process is killed while projection intents are still queued
- **THEN** after restart the projector writes every queued file
- **AND** every projected file's bytes equal the store's bytes for that file
- **AND** no temporary projection file remains

#### Scenario: Exomem's own projection is not an outside edit
- **WHEN** the watcher or reconcile sees a file whose hash equals the manifest hash
- **THEN** Exomem treats the file as unchanged and imports nothing

#### Scenario: Projection lag is visible
- **WHEN** projection intents are queued
- **THEN** status reports the number of queued files and the age of the oldest one

### Requirement: Outside edits return through a base-revision check
In store mode, a file the store owns whose hash differs from the projection manifest, or a new text file of a kind the store owns, SHALL be treated as an outside edit. When the store revision of that file equals the manifest revision and no projection intent is queued for it, Exomem SHALL import the file as a governed write with outside-editor provenance. Otherwise Exomem SHALL keep the outside bytes as a held correction for review and SHALL NOT overwrite either version. An outside delete of a page SHALL become a held deletion, never a silent delete of canonical content. A binary file is canonical on disk and the store records only its hash and size, so an outside change, delete or rename of a binary SHALL import as an audited record change; Exomem SHALL hold a binary only when an Exomem delete of it is queued and the file differs.

#### Scenario: A clean outside edit imports
- **WHEN** a person edits a projected page in Obsidian and no Exomem write to that page is pending
- **THEN** the next watcher event or reconcile imports the new bytes in one transaction with their derived rows
- **AND** the audit row names the outside editor as the origin

#### Scenario: A concurrent outside edit is held
- **WHEN** a person edits a page on disk while a newer store revision of that page is not yet projected
- **THEN** Exomem keeps the person's bytes as a held correction
- **AND** the projector does not overwrite the person's file until the correction is resolved

#### Scenario: An outside delete is held
- **WHEN** a projected page disappears from disk without an Exomem delete
- **THEN** the store keeps the page and records a held deletion for review

#### Scenario: A held deletion clears when the page returns
- **WHEN** a person restores the file of a held deletion with the bytes that Exomem last projected
- **THEN** the held deletion is removed and nothing is imported

#### Scenario: An outside delete of a binary imports
- **WHEN** a binary file disappears from disk without an Exomem delete
- **THEN** the store removes the binary's record in an audited import, and holds nothing
- **AND** a binary renamed outside Exomem keeps its record under the new path, paired by its hash

#### Scenario: An outside media file gets its derived rows from the store thread
- **WHEN** a media file appears, changes or is renamed outside Exomem
- **THEN** the importer records the binary, and the sidecar page that media processing derives for it commits on the store thread with that page's lexical and graph rows, while the hash of the whole file stays on the importer's thread
- **AND** a media job's result that arrives later commits on the store thread, never from the media worker's thread

#### Scenario: The media store's eviction and restore are no outside change
- **WHEN** the media store evicts an original from disk and later restores it
- **THEN** the importer imports nothing, holds nothing, and leaves the binary's record and the sidecar page unchanged
- **AND** a missing stored original reads as evicted and keeps its record, as in file mode, while a person's rename of it pairs by hash and moves the record

### Requirement: A held item is resolved by an explicit choice
In store mode, an agent SHALL be able to list every held item and to resolve each one on request with the choice keep-mine or keep-store. Listing SHALL show the person's file as it is now and the store's current revision. A resolve SHALL refuse, and change nothing, when the held record, the file or the store page differs from what the listing showed. Keep-mine SHALL make the person's current file the stored page without overwriting a store change newer than the hold, and SHALL NOT queue a projection. Keep-store SHALL save the person's file bytes durably outside the vault before it queues the store's version, and SHALL NOT delete or overwrite an earlier saved copy. A resolve SHALL need the same write authority as a governed write of the bytes it commits, and SHALL refuse with `WRITE_REFUSED`, before it saves or commits anything, when the caller lacks it. A resolve SHALL NOT depend on the body kept in the held record. The listing SHALL leave out, without a count, a held item whose page the caller may not read, and SHALL decide that in one order. The owner with unrestricted access sees every held item. Any other caller is judged on the stored page's bytes, not on the person's file or the path alone. When the store holds no page, the person's file is judged the same way. When neither exists, and for a binary, the path alone decides. The listing SHALL mark a held binary, and a held file above 64 MiB, as not yet resolvable with no choices and with a sentence that names why. A resolve SHALL refuse a binary and SHALL name that it stays held until a later release resolves binaries, and SHALL refuse a file above 64 MiB with `VAULT_STORE_HELD_TOO_LARGE`.

#### Scenario: Keep mine adopts the person's file
- **WHEN** an agent resolves a held edit with keep-mine and the store page is still at the revision the hold saw
- **THEN** the stored page equals the person's current file bytes and no write to the file is queued
- **AND** the held record is gone and the audit row names the choice

#### Scenario: Keep mine refuses after a newer Exomem change
- **WHEN** the store page changed after the edit was held and an agent resolves it with keep-mine
- **THEN** the resolve refuses with a conflict and the stored page keeps Exomem's newer bytes

#### Scenario: Keep store saves the person's file first
- **WHEN** an agent resolves a held edit with keep-store
- **THEN** the person's exact bytes are saved under a dated folder in the vault's state folder before the store's page is queued for projection
- **AND** a second resolve of the same path on the same day saves to a new name

#### Scenario: A held item changed since it was listed
- **WHEN** the person edits the file again after an agent listed the held item
- **THEN** a resolve with the earlier listing refuses and names the list to read again

#### Scenario: A held delete is resolved
- **WHEN** an agent resolves a held deletion with keep-mine
- **THEN** the page leaves the store with no write to the vault
- **AND** a resolve with keep-store writes the store's page back to the file

#### Scenario: A held item the caller may not read is not listed
- **WHEN** a policy restricts the folder of a held page and a caller outside that policy lists the held items
- **THEN** the list leaves the item out and gives no count of left-out items
- **AND** a resolve by that caller refuses as if nothing were held for the path

#### Scenario: A tag scope decides a held item on the stored page
- **WHEN** a policy restricts pages by a tag, a held page's stored bytes carry no such tag and the person's file does
- **THEN** the owner and a caller outside that policy both see the item and the owner can resolve it
- **AND** when the stored bytes carry the tag, the caller outside the policy sees nothing and the owner still sees and resolves the item

#### Scenario: A held edit of a page that the store deleted is the owner's
- **WHEN** Exomem deleted a page while the person's edit of its file stayed, and a policy restricts the page's folder
- **THEN** the owner sees the item and resolves it with keep-store
- **AND** a caller outside the policy sees nothing, and under a tag scope that caller sees the item exactly when the person's file does not carry the tag

#### Scenario: A held binary is not resolved yet
- **WHEN** a held item is a binary and an agent lists or resolves it
- **THEN** the list marks it not yet resolvable and offers no choice
- **AND** a resolve refuses and says that binaries stay held until a later release resolves them
- **AND** under a tag scope the owner still sees the held binary, and a caller outside the policy does not

#### Scenario: A held file that is too large is not resolved
- **WHEN** a held file is above 64 MiB and an agent lists or resolves it
- **THEN** the list marks it not yet resolvable, offers no choice and says why
- **AND** a resolve refuses with `VAULT_STORE_HELD_TOO_LARGE`

### Requirement: Semantic units are first-class store rows
In store mode, once stage 2 ships, each semantic unit of a page SHALL be one store row with its identity, its page, its ordinal and exact byte span in the page bytes, its category, kind and text, its provenance, its verdict and its lifecycle. The identity SHALL follow the existing semantic unit identity rule: the authored anchor when the page carries one, otherwise the fingerprint-bound reference. The store SHALL derive the identity from the page bytes and SHALL NOT write anchors into a page. For the same bytes, the unit rows SHALL reproduce file mode's lexical semantic-unit rows and graph unit nodes. Typed edges SHALL be able to start or end at a unit as well as at a page. A governed write that changes a unit SHALL change the page bytes and the unit row in the same transaction.

#### Scenario: A link reaches the exact unit
- **WHEN** a page links to another page's unit by the anchor that unit carries
- **THEN** the graph edge ends at that unit, not only at its page

#### Scenario: Refuting one claim changes one unit
- **WHEN** a governed write marks one claim on a page as refuted
- **THEN** the page bytes and that unit's verdict commit in one transaction
- **AND** the other units on the page keep their identities and verdicts

#### Scenario: A unit without an anchor keeps today's identity
- **WHEN** a governed write adds a unit that carries no authored anchor
- **THEN** the unit's identity is its fingerprint-bound reference
- **AND** the committed page bytes are exactly the bytes the write supplied

### Requirement: Originals record their canonicity
In store mode, once stage 2 ships, every stored file SHALL carry a canonicity record with its origin, content hash, size, format and derivation: either original, or derived from a named content hash by a named transform. A governed write SHALL be able to supersede a file recorded as original with the true original. After it, the earlier file SHALL remain stored as derived from the true original, citations SHALL resolve to the true original, and the audit trail SHALL record the change.

#### Scenario: A compressed copy filed as original is corrected
- **WHEN** a compressed copy was filed as an original and the true original arrives later
- **THEN** one governed write records the true original as original and the copy as derived from it
- **AND** pages that cited the copy resolve to the true original
- **AND** the copy's bytes stay available

#### Scenario: Every original says how it is known to be original
- **WHEN** a reader inspects a stored file
- **THEN** it sees the file's origin, hash, size, format and derivation

### Requirement: Migration into the store is verified and reversible
Exomem SHALL migrate a file-mode vault into store mode offline. The migration SHALL load every page's exact bytes and the current derived rows, and SHALL verify before activation that the derived rows equal a fresh derivation from the loaded bytes and that a projection of every page reproduces the original bytes. Reverting to file mode SHALL first project every queued intent, including a full projection when projection was off. It SHALL refuse while held corrections or held deletions remain, unless they are exported for review. Exporting a held item SHALL move the person's held bytes into the export and project the store's version of that file, so the rebuild does not apply the held edit. It SHALL export the audit trail, the canonicity records and the path alias table, which file mode has no tables for. After those steps, stopping the store and rebuilding the file-mode sidecars from the projected Markdown SHALL restore file mode with the same pages.

#### Scenario: Migration preserves every page byte
- **WHEN** a vault is migrated and then fully projected to an empty directory
- **THEN** every projected file is byte-identical to the file it was migrated from

#### Scenario: Reverting rebuilds file mode from the projection
- **WHEN** an operator reverts a store-mode vault with no queued intents and no held items to file mode
- **THEN** the audit trail, the canonicity records and the path alias table are exported as files
- **AND** file mode rebuilds its lexical catalogue and graph from the projected Markdown
- **AND** search and graph answers match the store's answers for the same pages

#### Scenario: Reverting refuses while held edits are unexported
- **WHEN** an operator reverts a store-mode vault that holds an unreviewed outside edit
- **THEN** the revert refuses and names the held items
- **AND** no file-mode rebuild applies the held bytes

### Requirement: Knowledge writes share the store's availability
In store mode, knowledge writes SHALL take the collection store's writer lease and transaction owner. When the store cannot accept writes, because its engine is unsupported, its sync is pending, it has diverged or its custody is unknown, a store-mode knowledge write SHALL refuse with the store's reason. Store mode SHALL refuse to start or activate while multi-host writer-lease coordination is configured, because file replication does not carry the store. The collection store's in-vault replica SHALL NOT include page, lexical or graph tables; the Markdown projection is the in-vault copy of pages.

#### Scenario: An unavailable store refuses knowledge writes
- **WHEN** the store reports that it cannot accept writes
- **THEN** a store-mode knowledge write refuses with that reason
- **AND** no Markdown file changes

#### Scenario: The in-vault replica carries collections only
- **WHEN** the collection store publishes its in-vault replica in a store-mode vault
- **THEN** the replica holds the collection tables and no page, lexical or graph table

### Requirement: A change to the schema overlay settles the registry state in the background
In store mode, a commit that changes a source of the schema overlay (a page under `_Schema/` or the access policy file) SHALL record its revision and return without deriving every page again. A background job SHALL then derive the stored pages again, one bounded transaction per batch, and SHALL record the overlay revision that the derived rows reflect. Status SHALL report `rebuilding` with the target revision while the rows reflect an older overlay, `current` when they reflect the latest, and `unknown` when no record says or the record of the last overlay change cannot be read, and SHALL count the overlay paths that wait for the projector or a hold. A tick with no batch to run SHALL NOT take the vault's mutation boundary. A batch and its cursor SHALL commit together, so a kill leaves the rows behind the store and never ahead of it, and the next start SHALL resume at the cursor. The job SHALL run on the store thread's scheduler and SHALL add no thread. The same job SHALL give a store that an older build set up the derived tables that this build adds: until it has created them and derived every page, status SHALL report `unknown` with `owner_schema` as `behind`, and a reader of rows that the store does not hold yet SHALL refuse with `VAULT_STORE_DERIVED_BEHIND` and SHALL NOT answer as if no row matched. The job SHALL record the tables it creates, so a rollback drops them.

#### Scenario: A policy change returns before its rows and the job then settles them
- **WHEN** a commit changes the access policy of a store-mode vault
- **THEN** the commit returns while status reports `rebuilding` with that commit's revision as the target
- **AND** when the job finishes status reports `current`, and every derived table equals a fresh derivation from the stored bytes with the new policy

#### Scenario: A kill inside a batch leaves the rows behind the store
- **WHEN** the process is killed inside a batch of the job
- **THEN** no derived row is ahead of the recorded overlay revision
- **AND** the next start resumes the job and ends with the rows of a fresh derivation

#### Scenario: A write does not wait for the job
- **WHEN** a governed write commits between two batches of the job
- **THEN** the write commits at once, and the job derives that page with the policy that the vault holds when it finishes

#### Scenario: A store from an older build gains this build's derived tables
- **WHEN** this build opens a store-mode vault whose store an older build set up without the inbound-link tables
- **THEN** status reports `unknown` with `owner_schema` as `behind`, and `list_inbound_links` refuses with `VAULT_STORE_DERIVED_BEHIND` instead of an empty list
- **AND** after the job finishes, status reports `current`, the tables equal a fresh derivation, and a rollback drops them

### Requirement: Store-mode graph and link reads answer at the request's snapshot
In store mode, every reader of graph rows SHALL read the store's rows through the request's one snapshot, and SHALL NOT open the file-built graph sidecar, prove its readiness or schedule a rebuild. The snapshot revision and the registry state SHALL be the whole readiness model. Each commit SHALL write the inbound-link rows of the pages it gives bytes to or removes by the scan rule of file mode's inbound-link index, so `list_inbound_links`, the inbound check of a move or delete, and the commit's guard on the linking pages answer by one rule. A page that only the store holds SHALL count as a linking page and as a link target, whether or not its file exists. While the registry job runs or its state is unknown, a graph or link reader SHALL serve its snapshot's rows with the registry notice. For the same page bytes, a store-mode graph or link reader SHALL give the answer that file mode gives, for the owner and for a restricted caller.

#### Scenario: A graph read never touches the sidecar or a projected file
- **WHEN** a store-mode vault's graph sidecar is replaced by unreadable bytes and every projected page is damaged
- **THEN** `ask_memory` with the graph, `graph_context`, a relation filter, the correction context and `list_inbound_links` answer as file mode answered for the same bytes
- **AND** each tool call reads one store snapshot, and no stored page's file is read or stat'd

#### Scenario: A link from a page that has no file yet blocks a delete
- **WHEN** a store-mode commit stores a page that links to another page and the projector has not written it
- **THEN** `list_inbound_links` for the other page names it with its line and context
- **AND** a delete of the other page that read its links before that commit is refused by the commit's guard

### Requirement: A store-mode vault says so beside the vault
Activation SHALL record its store-mode claim in the collection store's `store_meta` and publish it before the transaction that installs the store's rows, and rollback SHALL remove it in the transaction that records file mode. Every decision to enter or leave store mode SHALL carry a number that is one above the largest number that the deciding root sees, under the lease, in its own store and in the replica under the vault; the larger number SHALL be the newer decision, a tie SHALL go to the replica's copy, and the marker SHALL follow the newest decision. A takeover SHALL decide nothing: it SHALL merge the replica's decision into the new tenure's store so that the newest decision wins whichever root made it, it SHALL carry a replica decision that it cannot read as it stands, and it SHALL keep the claim of the root that it runs on, never the replica's. A root SHALL force the hand-back to copy the replica under the vault when its own decision is newer than the replica's, because store mode changes no collection head. `exomem store-mode migrate` SHALL check, after it takes the lease and merges the replica, that no other root's store claim stands, SHALL record and publish its claim and read it back from the replica before it installs any row, and SHALL leave the vault in file mode and remove its claim when the replica does not carry it. `exomem store-mode release --apply --claim ID --why TEXT` SHALL decide file mode over the named claim of another root, on a file-mode root while no service holds the lease, and SHALL refuse when this root holds the claim, when no store claim stands or when the id is not the standing claim; a store marker with no decision SHALL be the claim `legacy`. A holder whose claim was released or superseded SHALL read as displaced and SHALL NOT reassert it. A file-mode service on another state root SHALL read the marker from the replica, whatever store schema wrote it, when it starts and SHALL refuse with a named code, the reason and the remediation, which names the release command and the claim id, when the replica says store. An absent or unreadable replica SHALL NOT block a file-mode start, and a vault that never migrated SHALL start as before. A state root whose own store carries a newer decision than the replica's, or whose claim the replica carries, SHALL start. A decision that cannot be read, or that disagrees with the marker, SHALL leave the order unknown: it SHALL refuse on a store marker, SHALL NOT force a copy, and a takeover SHALL carry an unknown replica decision as it stands, so no publication drops it. `exomem store-mode status` and the doctor SHALL show the standing decision and whether this root holds, is displaced from or is refused by it.

#### Scenario: Another state root refuses a store-mode vault
- **WHEN** a vault is migrated into store mode and a file-mode service starts on another state root over the same vault
- **THEN** the service refuses to start with `VAULT_STORE_ELSEWHERE` and names the rollback and the release command with the claim id

#### Scenario: Rollback releases the vault
- **WHEN** the vault is rolled back to file mode
- **THEN** the replica no longer says store and the file-mode service on the other state root starts

#### Scenario: A replica that cannot be read does not block a file-mode vault
- **WHEN** the replica is absent or unreadable
- **THEN** a file-mode service starts, and status names the unreadable replica

#### Scenario: The newest decision wins at a takeover
- **WHEN** a root rolled back and its clear replica was not published, and another root then takes the lease from a replica that still says store, whether it adopts the replica or continues from its head
- **THEN** the root that rolled back starts, and its next hand-back publishes the clear replica, after which the other root starts

#### Scenario: A rollback that published does not undo a later claim
- **WHEN** a root rolled back and published the clear replica, another root then migrated, and the first root takes the lease again
- **THEN** the replica that it publishes still says store, and the first root refuses to start

#### Scenario: An old replica does not lock out a root that saw the newer decision
- **WHEN** a sync restores a replica that says store, older than a rollback that a root already merged
- **THEN** that root starts

#### Scenario: A second root does not migrate a vault that another root holds
- **WHEN** the replica says store and a root that does not hold the store runs `exomem store-mode migrate --apply`, even when its preflight read a replica that had not yet gained the claim
- **THEN** the command refuses with `VAULT_STORE_ELSEWHERE` and the vault stays in file mode on that root

#### Scenario: A claim that no replica carries installs nothing
- **WHEN** the publication of a migration's claim fails
- **THEN** the root stays in file mode, another root's migration succeeds, and one root holds the vault

#### Scenario: The owner releases a claim whose holder is gone
- **WHEN** the owner runs `exomem store-mode release --apply` on a refused root with the claim id that the refusal names, and no service holds the lease
- **THEN** the replica says file, the root starts, and a wrong claim id refuses without a change

#### Scenario: A holder that returns after a release is displaced
- **WHEN** the released holder takes the lease again
- **THEN** the replica stays clear, status shows it displaced, and its rollback decides nothing over the release

#### Scenario: A replica that has the marker and no decision is released as the legacy claim
- **WHEN** the replica says store without a decision, the root has none, and the owner runs `exomem store-mode release --apply --claim legacy`
- **THEN** the replica says file and the root starts

#### Scenario: A decision that this build cannot read survives a takeover
- **WHEN** the replica carries a store marker whose decision this build cannot read, and a refused root commits as the new tenure
- **THEN** the next publication still says store and the root still refuses to start

### Requirement: Freshness requirements follow store ownership
For files the store owns, the requirements that keep derived sidecars in step with Markdown on disk SHALL NOT apply in store mode; a change on disk to such a file SHALL follow the outside-edit requirement of this capability, and caches SHALL key on store revision. For vault content outside the store, those requirements SHALL keep applying in store mode as they do in file mode.

#### Scenario: An outside edit to a store-owned page goes through the base-revision check
- **WHEN** a projected page changes on disk in store mode
- **THEN** Exomem handles it as an outside edit through the base-revision check
- **AND** no sidecar reindex or graph correction runs against the on-disk bytes outside that check

#### Scenario: Vault content outside the store stays fresh
- **WHEN** a note outside the managed root changes on disk in a store-mode vault
- **THEN** its vault-scope lexical rows refresh as they do in file mode
- **AND** the note is not imported into the store

### Requirement: Store-mode names read the store
In store mode, the collision check for a new page name SHALL read the store and never the projected files. A name SHALL therefore be taken from the moment its write commits, whether or not projection has run.

#### Scenario: Two quick creates that differ only in case
- **WHEN** two governed writes create pages whose titles differ only in case, and projection has not yet written the first page
- **THEN** the second page gets a numbered suffix
- **AND** the first page keeps its name
