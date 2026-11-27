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

#### Scenario: A governed move of a binary records it, then renames it
- **WHEN** a delete, a move or a recovery in store mode moves a binary file
- **THEN** one transaction moves the binary's record to its new path, bound to the old record as read and to an empty new path, and queues a move intent there that names the old path
- **AND** the writer renames the file right after the transaction, so the caller finds it at its new path on return
- **AND** a refused transaction moves no file

#### Scenario: A lifecycle write killed around its transaction converges
- **WHEN** the process is killed while a store-mode trash, move or recovery runs, before its transaction, inside it, or after it and before the writer's last rename
- **THEN** a fresh start finds every page and binary of the write on one side, its old paths or its new ones, with each binary's record naming the file at its path, whether the projector or the importer runs first
- **AND** the projector renames each binary whose move is queued, and never unlinks or replaces a binary to finish a move
- **AND** the importer records no file at the old path of a queued binary move
- **AND** once the projector has drained, the folders on disk are those that file mode leaves: a folder trash or recovery has removed the moved folder's own tree and nothing above it, a single file's write has removed no folder, and no folder that holds anything has gone
- **AND** a file or folder that the store does not own may stay at its old path, with none of its bytes lost
- **AND** the next write through the served door succeeds

### Requirement: One transaction per governed write
A governed write in store mode SHALL commit every file it changes, one audit row per file, one projection intent per file and one derivation intent per file in one `BEGIN IMMEDIATE` transaction, and SHALL acknowledge only after that transaction is durable. That transaction SHALL NOT write lexical, graph, identity, inbound-link, prior-path or page-fact rows; they follow from the derivation intents. The vault mutation boundary of a governed write SHALL hold only the checks that bind the write and that transaction. The number of statements the transaction runs SHALL NOT grow with a page's units or links, with the pages that link to it or with the size of the vault. A request that the store thread serves SHALL acknowledge once that transaction is durable, before the store thread derives its intents. A governed trash or recovery, an upload and a request that the store thread does not serve SHALL derive their own intents after they release the boundary and before they acknowledge; when that derivation fails or passes its deadline, the write SHALL acknowledge with its derived state `pending` or `failed`. The full-detail reply of a store-mode governed write SHALL carry the writer's result under `diagnostics.leaf_result`, the state of its lexical, identity and graph rows under `diagnostics.derived_components` and the derived state as `derived_sync` under `diagnostics.operational`, the shape of file mode's derived acknowledgement; its compact reply SHALL NOT change.

#### Scenario: The boundary hold does not grow with derivation
- **WHEN** a governed write runs once with an instant derivation and once with a derivation slowed by seconds
- **THEN** the two boundary holds differ by less than half the added derivation time
- **AND** the slow write's rows are derived after it releases the boundary

#### Scenario: A full reply says where the derived rows stand
- **WHEN** a store-mode delete returns at full detail
- **THEN** its trash path is under `diagnostics.leaf_result`
- **AND** `diagnostics.derived_components` names the lexical, identity and graph rows with their state

#### Scenario: A page's size reaches the commit only through its bytes
- **WHEN** one governed write stores a page with one unit and another stores a page with 400 units and 400 links
- **THEN** both run the same number of statements while they hold the vault mutation boundary

#### Scenario: A hard kill loses no derivation
- **WHEN** the process is killed at any point while a governed write or its derivation runs
- **THEN** after restart every file of that write shows all of its old state or all of its new state, in page bytes, audit rows, projection intents and derivation intents together
- **AND** each page whose derived rows are older than its bytes has a derivation intent, which the store thread derives before it starts other work, with no rebuild, repair or reconcile

#### Scenario: Derived rows match file mode exactly
- **WHEN** the same sequence of page bytes is written once in file mode and once in store mode from the same starting vault, and store mode has derived every intent
- **THEN** the lexical page rows, semantic unit rows, graph nodes, graph edges, graph dependencies and parent references are equal, apart from columns that record write time or write order

#### Scenario: A governed page's trash keeps its tombstone and receipt around the transaction
- **WHEN** a store-mode delete or recovery moves a page that a governance policy restricts
- **THEN** the receipt intent and the tombstone are written under the governance folder before the transaction, and the receipt settles after it from the store's rows and the vector and CLIP sidecars
- **AND** reconcile settles a kill on either side of the transaction from the store, never from the Markdown projection

#### Scenario: An active catalog publication follows the transaction that it names
- **WHEN** a store-mode delete, move or recovery runs on a vault with an active governance catalog publication
- **THEN** the catalog change is prepared before the writer's validation returns, the change and its log entry commit in one transaction, and the catalog publishes after it
- **AND** a publication that fails after the transaction reports the catalog publication as uncertain

### Requirement: Derived rows follow their page's derivation intent
In store mode, the store thread SHALL derive the rows of each path that a derivation intent names, from its stored bytes or its unowned file, with the code that file mode uses. Each transaction SHALL take whole revisions in order, up to 32 pages unless one revision holds more, delete their intents, re-derive the graph rows of pages whose links they re-resolve, and advance the store revision. The store thread SHALL derive due intents before other work, without the vault mutation boundary.

#### Scenario: An intent that failed before runs apart from a later write
- **WHEN** an intent whose derivation failed before falls due beside the intents of a later write
- **THEN** it derives in a transaction of its own
- **AND** the later write's pages derive together in one transaction

#### Scenario: A write can cite the page that the write before it created
- **WHEN** a governed write creates a Source and the next governed write cites it by its reference
- **THEN** the second write resolves the Source and commits

#### Scenario: A new title resolves earlier links in its derivation
- **WHEN** a governed write creates a page whose title other pages already link to
- **THEN** the transaction that derives the page also writes the edges from those pages to it

#### Scenario: No canonical commit copies the write-ahead log
- **WHEN** the store's write-ahead log passes the size at which SQLite would copy it into the store
- **THEN** the store thread copies it between its work
- **AND** no governed write's commit runs that copy

### Requirement: Link and identity reads do not trust rows that lag their page
While a page's derivation intent is pending or failed, every store-mode read of inbound-link or identity rows SHALL take that page's rows from its stored bytes by the rule that its derivation applies, in place of the rows that the store holds for it. No link check, link rewrite or reference resolution then trusts a row that the page no longer gives.

#### Scenario: A link from a page whose derivation did not land blocks a delete
- **WHEN** a committed page links to another page and its derivation failed or waits with `no_room`
- **AND** a delete of the other page runs without `force_orphan`
- **THEN** the delete refuses with `INBOUND_LINKS` and names the linking page, as when the page's rows are derived

#### Scenario: A reference names where a moved page is while its rows lag
- **WHEN** a page is moved and the derivation of that move failed
- **THEN** a memory reference to the page resolves to its new path

### Requirement: Derivation lag is visible and bounded
One intent SHALL stand for each path, keeping its oldest queue time. A failed derivation SHALL keep its intent and retry with backoff, then be marked failed with a reason code, and SHALL NOT hold back other intents. Status SHALL report the derived state as `current`, `behind`, `failed` or `unknown`, with counts and the oldest age. A reader whose snapshot holds unfinished intents SHALL say so, naming no path. A clean stop or handoff SHALL derive due intents within its bound.

#### Scenario: A failed derivation keeps the page and says so
- **WHEN** the derivation of a committed page fails
- **THEN** the page's bytes stay stored and a read by path returns them
- **AND** status reports `failed` with the count and the reason code, and names no path
- **AND** a later retry or the next start derives the page

#### Scenario: An original's derived rows wait for room above the upload floor
- **WHEN** a stored original's page fits above the vault's upload floor and the rows derived from it do not
- **THEN** the write commits the page and acknowledges with its derived state `pending`
- **AND** the derivation writes none of those rows and keeps the intent with the reason code `no_room`
- **AND** a retry after room returns derives them

#### Scenario: A reader during a drain is told what lags
- **WHEN** a search's snapshot holds a committed page's pending derivation intent once any wait for it has ended
- **THEN** the search answers from its snapshot's rows with the derived-behind notice, the pending count and the oldest intent's age
- **AND** the notice names no path

#### Scenario: A clean stop leaves no due intent
- **WHEN** the service stops cleanly with committed pages whose intents are due
- **THEN** the stop derives them within its bound before it publishes the replica
- **AND** the next start finds none of them due

#### Scenario: A drain that keeps failing does not spin
- **WHEN** a drain step raises on each attempt
- **THEN** the store thread retries it no sooner than its retry delay
- **AND** it warns once until a drain succeeds

#### Scenario: A governed trash settles in its request
- **WHEN** a governed page is trashed or recovered in store mode
- **THEN** the request derives the transition's intents after it releases the boundary, then settles the receipt in a second short hold that reads their state
- **AND** the receipt settles without a reconcile, and the boundary hold does not grow with the derivation

#### Scenario: A governed trash whose rows stay underived says so
- **WHEN** a governed trash's rows are still pending or failed when its request settles it
- **THEN** its reply carries the not-exact warning and the content stays tombstoned until reconcile

### Requirement: A read reflects the writes acknowledged before it, or says what lags
While a store thread in its process derives rows, a store-mode read request SHALL wait at its door, holding no store connection, for at most one second and half its remaining budget, until no intent at or below the revision it first read is pending. If intents stay pending, a ranked read SHALL answer with the derived-behind notice, and an unrestricted caller's direct page read that finds no page SHALL refuse with `VAULT_STORE_DERIVED_BEHIND`.

#### Scenario: Search and graph see the write after acknowledgement
- **WHEN** a governed write that adds a page and a link returns success and a lexical search for a term in the page starts after that
- **THEN** the search returns the page and the graph returns the new edge
- **AND** neither depends on a watcher event or repair

#### Scenario: A write replies before its rows are derived
- **WHEN** a governed write that the store thread serves runs once with an instant derivation and once with a derivation slowed by seconds
- **THEN** the two replies differ by less than half the added derivation time
- **AND** each reply says that its derived state is `pending`

#### Scenario: A slow derivation is reported, not hidden
- **WHEN** a governed write's derivation has not finished when a later search's wait ends
- **THEN** the search answers with the derived-behind notice, its counts and its age, and with no path, title or content
- **AND** a read of the new page's path, reference or short id returns the page from its stored bytes, with the page's own reference and short id, not a missing page
- **AND** the owner's read of a moved page's old path refuses with `VAULT_STORE_DERIVED_BEHIND`, not as a missing page

#### Scenario: The lag tells a restricted caller nothing about withheld pages
- **WHEN** a restricted caller reads a withheld page and an absent one while derivation intents are pending
- **THEN** both reads answer `NOT_FOUND` alike, as they do with no intent pending

#### Scenario: A recall during the lag answers and says what lags
- **WHEN** a recall's wait ends before a save's rows are derived, and its conversation cites the new page's short id or a ref that names no page
- **THEN** the recall returns its packet with the derived-behind notice
- **AND** no ref, hint, brief seed or anchor refuses for the lag

#### Scenario: A read with no drainer answers at once
- **WHEN** derivation intents are pending and no store thread in the reader's process derives them
- **THEN** the read answers at once with the derived-behind notice

#### Scenario: A waiting reader holds no store connection
- **WHEN** a search waits for the derivation of an earlier write
- **THEN** it holds no store connection while it waits

#### Scenario: A failed derivation holds no reader back
- **WHEN** a page's derivation failed and waits for its retry
- **THEN** a read does not wait for it

#### Scenario: A reader does not wait for later writes
- **WHEN** a write commits after a read began
- **THEN** the read does not wait for that write's derivation

#### Scenario: A write and a drain never wait for rows
- **WHEN** a write, a drain, the store thread or an internal read such as a warm-up or a cache build reads the store while derivation intents are pending
- **THEN** it reads at once, without waiting for any derivation intent

### Requirement: Markdown is a byte-exact projection
In store mode, Exomem SHALL write each committed file to the vault as a byte-exact projection after the transaction commits. A projector SHALL make the projected file durable before it records the file's hash, size, modification time and revision in the projection manifest. At startup the projector SHALL remove its own unfinished temporary files and SHALL project every intent still queued. Several revisions of one file that queue before projection MAY coalesce into the latest one.

#### Scenario: Projection catches up after a kill
- **WHEN** the process is killed while projection intents are still queued
- **THEN** after restart the projector writes every queued file
- **AND** every projected file's bytes equal the store's bytes for that file, apart from one managed relations block
- **AND** no temporary projection file remains

#### Scenario: Exomem's own projection is not an outside edit
- **WHEN** the watcher or reconcile sees a file whose hash equals the manifest hash
- **THEN** Exomem treats the file as unchanged and imports nothing

#### Scenario: Projection lag is visible
- **WHEN** projection intents are queued
- **THEN** status reports the number of queued files and the age of the oldest one

#### Scenario: A clean stop projects the queue, bounded
- **WHEN** the service stops cleanly with projection intents queued
- **THEN** the projector works through the queue for a bounded time before the service releases the vault
- **AND** an intent that it did not reach in that time stays queued, and the next start projects it
- **AND** a service on an HTTP transport that its supervisor stops with SIGTERM does the same and hands back the writer lease, also when the SIGTERM arrives while its startup runs, once that startup has finished; the stdio transport is unchanged

#### Scenario: Only a process that can record a projection writes it
- **WHEN** a replica without the vault's writer lease runs a projector and projection intents are queued
- **THEN** its projector writes no projection file and records nothing, and its pass ends as at a busy boundary, with no failure logged
- **AND** once that replica takes the writer lease, its next pass projects the queued intents

#### Scenario: A lifecycle write leaves the folders that file mode leaves
- **WHEN** a store-mode trash, move or recovery has committed and the projector has drained
- **THEN** a single file moved or trashed out of a folder has removed no folder, not even its emptied parent
- **AND** a folder trash or recovery has removed the moved folder's own tree at its source, and nothing above it
- **AND** a folder that holds something no queued intent moved stays

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
- **THEN** the stored page equals the person's current file bytes with one valid relations block removed, and no write to the file is queued
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
In store mode, once stage 2 ships, each semantic unit of a page SHALL be one store row with its identity, its page, its ordinal and exact byte span in the page bytes, its category, kind and text, its provenance, its verdict and its lifecycle. The identity SHALL follow the existing semantic unit identity rule: the authored anchor when the page carries one, otherwise the fingerprint-bound reference. The store SHALL derive the identity from the page bytes. The store itself SHALL write an anchor only into a unit that carries no anchor and becomes either end of its first stored relationship, in the governed write that adds that relationship. For the same bytes, the unit rows SHALL reproduce file mode's lexical semantic-unit rows and graph unit nodes. Typed edges SHALL be able to start or end at a unit as well as at a page. A governed write that changes a unit SHALL change the page bytes and the unit row in the same transaction.

#### Scenario: A link reaches the exact unit
- **WHEN** a page links to another page's unit by the anchor that unit carries
- **THEN** the graph edge ends at that unit, not only at its page

#### Scenario: Refuting one claim changes one unit
- **WHEN** a governed write marks one claim on a page as refuted
- **THEN** the page bytes and that unit's verdict commit in one transaction
- **AND** the other units on the page keep their identities and verdicts

#### Scenario: A unit without an anchor keeps today's identity
- **WHEN** a governed write adds a unit that carries no authored anchor and no stored relationship
- **THEN** the unit's identity is its fingerprint-bound reference
- **AND** the committed page bytes are exactly the bytes the write supplied

### Requirement: Typed relationships are stored rows
In store mode, once stage 2 ships, a governed operation SHALL be able to add, retire or supersede a typed relationship from a unit to another unit or to a page as a store row, with no line of page text. The relationship type SHALL come from the relation vocabulary registry. A unit SHALL be able to carry any number of stored relationships. A store-mode answer SHALL read them from the stored rows only, in bounded pages with a keyset cursor and an exact total.

#### Scenario: A first stored relationship pins the unit's identity
- **WHEN** a governed operation adds the first stored relationship to a unit that has no anchor
- **THEN** the same transaction writes an anchor in that unit's own anchor form and stores the relationship against that unit's store-assigned id
- **AND** a later edit of the unit's text keeps the relationship attached

#### Scenario: The far end of a first relationship is pinned too
- **WHEN** a governed operation adds the first stored relationship whose far end is a unit without an anchor on another page
- **THEN** the same transaction writes an anchor in that far-end unit's own anchor form on its own page
- **AND** when the caller may see but not write that page, the operation refuses with `RELATIONSHIP_END_NOT_WRITABLE` and changes nothing
- **AND** when the caller may not see that page, the answer is the same as for an absent far end

### Requirement: A stored relationship keeps its history and two-ended visibility
Each stored relationship SHALL record who added it, when and why, and what retired or superseded it. A caller SHALL see a stored relationship only when it may see both of its ends, and an operation SHALL answer a far end the caller may not see exactly as an absent one.

#### Scenario: A restricted caller sees only relationships between visible ends
- **WHEN** a restricted caller reads a unit whose relationship points at a page withheld from it
- **THEN** that relationship is absent from the answer, as the withheld page is
- **AND** the page read it receives holds no relations block

### Requirement: Stored relationships project beside their page
A page's projected file SHALL carry one bounded managed relations block that the page bytes never hold, and a relations file beside it SHALL list every stored relationship whose source unit is on that page. The importer SHALL remove the block before it imports and SHALL read relationship edits from the relations file. A rebuild from Markdown SHALL read the relations files, and a rollback to file mode SHALL project them first.

#### Scenario: A unit carries more relationships than its page shows
- **WHEN** a unit carries 500 stored relationships
- **THEN** the page's relations block shows counts per type and a bounded number of relationships
- **AND** the relations file beside the page lists all 500
- **AND** a reader that pages through the unit's relationships receives all 500 and an exact total of 500

#### Scenario: Adding a relationship leaves the page bytes unchanged
- **WHEN** a governed operation adds a relationship between two units that already carry anchors
- **THEN** the stored page bytes of both pages are unchanged
- **AND** the same transaction queues both pages' projection intents
- **AND** the projector updates the relations block and the relations file after the commit

#### Scenario: Removing a unit keeps its stored relationships
- **WHEN** an outside edit, a governed edit or a trash removes a unit that carries stored relationships
- **THEN** the store keeps the relationships
- **AND** opens a review item that names the lost unit

### Requirement: The relations block never enters the stored bytes
From stage 2, a page's projection MAY add one managed relations block, the one exception to a byte-exact projection. Import, keep-mine, the held listing and migration SHALL remove one valid block before they compare or adopt a page. An edit that leaves the block invalid SHALL be held, and the listing SHALL show that block only to the unrestricted owner. A rollback to file mode SHALL project pages without the block, and a migration SHALL load the relations files beside the pages.

#### Scenario: An edit inside the relations block is held
- **WHEN** a person edits a line inside a page's relations block outside Exomem
- **THEN** the edit becomes a held correction and the stored page bytes are unchanged

#### Scenario: Stored relationships survive a rollback and a re-migration
- **WHEN** a store-mode vault with stored relationships reverts to file mode and is migrated back
- **THEN** the reverted pages carry no relations block and the relationship history is exported
- **AND** after the re-migration every current stored relationship is present again

### Requirement: Held relations items resolve without leaking or storing the block
Keep-mine on a held page whose relations block is not valid SHALL remove the block by its markers, and SHALL refuse and name keep-store when the markers are not intact. A held relations file SHALL be listed to, and resolved by, the owner with unrestricted access only. Migration verification SHALL remove one relations block before it compares a projection with its source.

#### Scenario: Keep mine on an edited relations block keeps the block out
- **WHEN** an agent resolves with keep-mine a held page whose person's file has an edited relations block with intact markers
- **THEN** the stored page equals the person's file with the block removed by its markers
- **AND** the next projection carries exactly one relations block

#### Scenario: A restricted caller cannot see a held relations file
- **WHEN** a caller without unrestricted access lists held items while a relations file is held
- **THEN** the listing leaves that item out without a count

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
Exomem SHALL migrate a file-mode vault into store mode offline. The migration SHALL load every page's exact bytes, copy the lexical and reference rows, and stage the graph rows from one fresh derivation of the loaded bytes, in the derivation's row order and with its row ids. It SHALL verify before activation that every staged row equals that derivation and that a projection of every page reproduces the original bytes. It SHALL report how many rows the graph sidecar differed from the derivation by, with the first differing keys, and SHALL NOT refuse for that difference. Reverting to file mode SHALL first project every queued intent, including a full projection when projection was off. It SHALL refuse while held corrections or held deletions remain, unless they are exported for review. Exporting a held item SHALL move the person's held bytes into the export and project the store's version of that file, so the rebuild does not apply the held edit. It SHALL export the audit trail, the canonicity records and the stored relationships' history, which file mode has no tables for. After those steps, stopping the store and rebuilding the file-mode sidecars from the projected Markdown SHALL restore file mode with the same pages.

#### Scenario: Migration preserves every page byte
- **WHEN** a vault is migrated and then fully projected to an empty directory
- **THEN** every projected file is byte-identical to the file it was migrated from, apart from one managed relations block

#### Scenario: A stale graph sidecar does not refuse the migration
- **WHEN** an operator migrates a file-mode vault whose graph sidecar keeps a page's edges to a page that has left the catalogue
- **THEN** the migration installs, and the store's graph rows equal a fresh derivation from the files
- **AND** the result reports how many graph rows the sidecar differed by, with the first differing keys
- **AND** a dry run says that it did not check the sidecar against the files, and the apply will

#### Scenario: Reverting rebuilds file mode from the projection
- **WHEN** an operator reverts a store-mode vault with no queued intents and no held items to file mode
- **THEN** the audit trail, the canonicity records and the relationship history are exported as files
- **AND** file mode rebuilds its lexical catalogue and graph from the projected Markdown
- **AND** search and graph answers match the store's answers for the same pages

#### Scenario: Reverting refuses while held edits are unexported
- **WHEN** an operator reverts a store-mode vault that holds an unreviewed outside edit
- **THEN** the revert refuses and names the held items
- **AND** no file-mode rebuild applies the held bytes

#### Scenario: Reverting projects what a stopped service left queued
- **WHEN** an operator reverts a store-mode vault whose projection queue holds intents that no service projected
- **THEN** the revert projects them offline, with no service, before it exports or drops anything
- **AND** a dry run lists them, counts no file of Exomem's earlier bytes as a person's edit, and changes no file
- **AND** a file that a person changed at a queued path is held, so the revert refuses until it is exported
- **AND** a revert killed while it projects is finished by the next run

#### Scenario: Reverting keeps a changed file whose page the store removed
- **WHEN** an operator reverts with `--export-held` a vault where a person changed a file whose page the store has since removed or moved
- **THEN** the revert moves that file into the vault trash by one rename, never a copy and a removal, and the export names where it went
- **AND** a move that is refused stops the revert before the vault leaves store mode, and names the file

### Requirement: Migration catches up queued file-mode index work
Before it stages, the migration SHALL drain the graph repair that a stopped file-mode service left queued, so the graph index that a rollback returns to is current. It SHALL refuse before staging, and name the reconcile command, when the graph index is not current after a bounded catch-up; receipts left while the graph is current do not refuse. A dry run SHALL report the queue's depth and the oldest receipt's age, and SHALL NOT refuse for a queue that the apply will drain.

#### Scenario: Migrate catches up queued file-mode index work before it stages
- **WHEN** an operator migrates a file-mode vault whose stopped service left graph repair queued
- **THEN** the migration drains that queue into the graph index before it stages, and the staged rows then equal the files
- **AND** a graph index that is still not current after the catch-up refuses before staging and names the reconcile command
- **AND** a dry run reports the queue's depth and age and drains nothing

### Requirement: Knowledge writes share the store's availability
In store mode, knowledge writes SHALL take the collection store's writer lease and transaction owner. When the store cannot accept writes, because its engine is unsupported, its sync is pending, it has diverged or its custody is unknown, a store-mode knowledge write SHALL refuse with the store's reason. Store mode SHALL refuse to start or activate while multi-host writer-lease coordination is configured, because file replication does not carry the store. The collection store's in-vault replica SHALL NOT include page, lexical, graph or derivation-intent tables; the Markdown projection is the in-vault copy of pages.

#### Scenario: An unavailable store refuses knowledge writes
- **WHEN** the store reports that it cannot accept writes
- **THEN** a store-mode knowledge write refuses with that reason
- **AND** no Markdown file changes

#### Scenario: The in-vault replica carries collections only
- **WHEN** the collection store publishes its in-vault replica in a store-mode vault
- **THEN** the replica holds the collection tables and no page, lexical, graph or derivation-intent table

#### Scenario: A store-mode service starts on a store that its writer has not yet migrated
- **WHEN** a release whose collection-store schema is newer starts the service of a store-mode vault whose store is at an earlier schema that this release can still read pages from
- **THEN** the service starts and serves page reads from the store before its writer migrates the store
- **AND** a knowledge write runs only after the writer holds the lease and has migrated the store

### Requirement: A change to the schema overlay settles the registry state in the background
In store mode, a commit that changes a source of the schema overlay (a page under `_Schema/` or the access policy file) SHALL record its revision and return without deriving every page again. A background job SHALL then derive the stored pages again, one bounded transaction per batch, and SHALL record the overlay revision that the derived rows reflect. Status SHALL report `rebuilding` with the target revision while the rows reflect an older overlay, `current` when they reflect the latest, and `unknown` when no record says or the record of the last overlay change cannot be read, and SHALL count the overlay paths that wait for the projector or a hold. No batch of the job SHALL take the vault's mutation boundary, and a batch SHALL run only while no derivation intent is due. A batch and its cursor SHALL commit together, so a kill leaves the rows behind the store and never ahead of it, and the next start SHALL resume at the cursor. The job SHALL run on the store thread's scheduler and SHALL add no thread. The same job SHALL give a store that an older build set up the derived tables that this build adds: until it has created them and derived every page, status SHALL report `unknown` with `owner_schema` as `behind`, and a reader of rows that the store does not hold yet SHALL refuse with `VAULT_STORE_DERIVED_BEHIND` and SHALL NOT answer as if no row matched. The job SHALL record the tables it creates, so a rollback drops them.

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
- **THEN** status reports `unknown` with `owner_schema` as `behind`, and `list_inbound_links` and the inbound check of a move or delete refuse with `VAULT_STORE_DERIVED_BEHIND` until the job finishes, between its batches as well, instead of an empty or partial list
- **AND** after the job finishes, status reports `current`, the tables equal a fresh derivation, and a rollback drops them

### Requirement: Store-mode graph and link reads answer at the request's snapshot
In store mode, every reader of graph rows SHALL read the store's rows through the request's one snapshot, and SHALL NOT open the file-built graph sidecar, prove its readiness or schedule a rebuild. The snapshot revision and the registry state SHALL be the whole readiness model. Each derivation SHALL write the inbound-link rows of the pages its commit gave bytes to or removed by the scan rule of file mode's inbound-link index, so `list_inbound_links`, the inbound check of a move or delete, and the commit's guard on the linking pages answer by one rule. A page that only the store holds SHALL count as a linking page and as a link target, whether or not its file exists. While the registry job runs or its state is unknown, a graph or link reader SHALL serve its snapshot's rows with the registry notice, except that a link reader SHALL refuse with `VAULT_STORE_DERIVED_BEHIND` while the store's owner-object version is behind this build's. For the same page bytes, a store-mode graph or link reader SHALL give the answer that file mode gives, for the owner and for a restricted caller; an inbound list SHALL hold the same links, which store mode lists in path order and file mode in the order of its walk. A typed link to a page path that the store holds no page at SHALL derive as unresolved, even while a removed page's file waits for the projector.

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

A store-mode root SHALL refuse to serve when its claim is displaced or the decision order is unknown. The refusal SHALL name the offline rollback with `--export-held`. An unknown decision SHALL direct the owner to a compatible build first.

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
- **WHEN** the released holder starts or reports its status
- **THEN** the service refuses with `VAULT_STORE_CLAIM_DISPLACED` and status shows it displaced
- **AND** its offline rollback decides nothing over the release

#### Scenario: The active store cannot read the standing decision
- **WHEN** a store-mode root starts and either decision cannot be ordered
- **THEN** the service refuses with `VAULT_STORE_CLAIM_UNKNOWN` and names a compatible build

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
