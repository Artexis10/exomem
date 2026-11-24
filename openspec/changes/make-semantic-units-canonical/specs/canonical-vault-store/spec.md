## ADDED Requirements

### Requirement: A unit record keeps a unit's identity
In store mode, each unit of a page that its derivation gives unit rows SHALL have a unit record with a store-assigned unit id, its placement, its state and its lineage. The unit id SHALL NOT depend on the unit's text or page. A history SHALL map every unit reference that a unit had to its unit id. Stored relationships SHALL name unit ids, and the store SHALL resolve an embed to the unit that carries its anchor. A page that the derivation does not index SHALL have no unit records.

#### Scenario: An edited unit keeps its unit id
- **WHEN** an agent rewrites the text of a unit that has no anchor through a unit operation
- **THEN** the unit keeps its unit id
- **AND** a unit reference saved before the edit resolves to the unit

#### Scenario: A moved unit keeps its unit id
- **WHEN** an agent moves a unit to another page
- **THEN** its unit id and its stored relationships stay the same
- **AND** its lineage names the page it moved from

#### Scenario: A retained before-image mints no unit
- **WHEN** a semantic write retains a compiled-history image of a page that holds units
- **THEN** the image has no unit records

### Requirement: A page's key is its exomem_id
The canonical commit SHALL settle a page key for each page of its change in an owner key table: a move's destination takes its source's key; a path's open entry keeps its key; another page takes its valid `exomem_id` unless an open entry holds it, else a UUID hashed from the store id, path and revision. Removals and moves SHALL close their entries first. A rename, trash or recovery SHALL keep every unit id.

#### Scenario: A rename keeps the page's unit ids
- **WHEN** a page with units is renamed and its unit derivation runs in a later batch than the rename's other pages
- **THEN** every unit keeps its unit id and its stored relationships

#### Scenario: A trash and a recovery keep the page's unit ids
- **WHEN** a page with a referenced unit is trashed and later recovered, with or without a drain in between
- **THEN** the unit's record is `trashed` after the trash and `live` after the recovery, with the same unit id

#### Scenario: Two moves through one path keep both pages' keys
- **WHEN** one request moves page A to X, then X to Y, then page B to X, before any unit derivation runs
- **THEN** the page from A keeps its key at Y and the page from B keeps its key at X
- **AND** every unit of both pages keeps its unit id

#### Scenario: A copy gets its own key
- **WHEN** a person copies a page file, so two pages carry one `exomem_id`
- **THEN** the page that held the key keeps it and the copy gets a minted key

#### Scenario: A page without an id gets one that backfill stamps
- **WHEN** a person creates a page with units and no `exomem_id`, and the operator later runs `backfill-ids`
- **THEN** the store mints its key and writes no id into the page itself
- **AND** backfill stamps that same value as the page's `exomem_id`

#### Scenario: A new page at a moved page's old path gets its own key
- **WHEN** a page without an `exomem_id` moves away and a new page is created at its old path before any unit derivation runs
- **THEN** the moved page keeps its key and the new page gets another

#### Scenario: A duplicated id resolves as ambiguous
- **WHEN** two pages carry one `exomem_id` and a reader resolves `exomem://memory/<id>#…`
- **THEN** the reference index reports the reference as ambiguous
- **AND** the key table still gives the records to one page

### Requirement: A removed page hands its key to the page that carries it
When a removal closes a key that exactly one open key entry records as its page's `exomem_id`, the same commit SHALL give that page the key. After every older version of both keys, the unit derivation SHALL match its units against the removed key's records by the light rule: matched units take the old unit ids, their replaced records retire with lineage `replaces`, and their stored relationships move. Otherwise the key's records SHALL retire.

#### Scenario: A held rename keeps its unit ids
- **WHEN** a person renames and edits a page, the importer holds the deletion and imports the new file, and keep-mine removes the old page
- **THEN** the new page takes the old page's key
- **AND** each unit that the light rule matches keeps its old unit id
- **AND** a stored relationship that an agent added to the new page's unit before keep-mine moves to that old unit id

### Requirement: A commit logs every page version that units need
The canonical transaction SHALL write no unit record. It SHALL log one pending version of each page of its change, with its key, revision, path and declaration, in an owner version log. Before it changes a path, it SHALL copy the path's current bytes into the pending version there that holds none, and reserve room for them. Its statements SHALL NOT grow with a page's units.

#### Scenario: A large page runs no more statements at commit
- **WHEN** one write stores a page with one unit and another stores a page with 400 units
- **THEN** both canonical transactions run the same number of statements
- **AND** each page has its unit records once its unit derivation ends

#### Scenario: Two commits in one request keep both declarations
- **WHEN** one request updates a unit through a unit operation and then commits the same page again before its derivation runs
- **THEN** the unit derivation applies the first commit's declaration to its own bytes and then matches the second commit's bytes
- **AND** the unit keeps its unit id

### Requirement: Only a unit operation refuses for pending versions
A unit operation on a page that keeps 16 or more pending versions SHALL refuse with `UNITS_BUSY` and a retry hint. No other commit SHALL refuse, merge or drop a version because of pending versions. The unit derivation SHALL apply at most 16 versions of a page per transaction.

#### Scenario: Page writes never refuse for pending versions
- **WHEN** page writes commit one page 20 times before any unit derivation runs
- **THEN** every commit succeeds
- **AND** the unit derivation applies all 20 versions in order

#### Scenario: A unit operation waits for a long backlog
- **WHEN** an agent updates a unit on a page that keeps 16 pending versions
- **THEN** the update refuses with `UNITS_BUSY` and a retry hint

### Requirement: Units derive from every page version in order
The store thread SHALL select pages for the unit derivation from the version log only, never from stored pages, the derivation queue or the audit trail, up to 32 pages per transaction in revision order. It SHALL apply each page's versions in revision order before the rows, delete them in the same transaction, and not advance the store revision. A new unit id SHALL hash the store's id, page key, revision and ordinal.

#### Scenario: A failed row derivation leaves units current
- **WHEN** a page's row derivation fails with `no_room` and waits for its retry while no other write touches the page
- **THEN** the page's records reflect its latest bytes before that retry
- **AND** a unit operation on the page proceeds

#### Scenario: A kill between the two transactions repeats nothing
- **WHEN** the process is killed after the unit transaction commits and before the rows transaction commits
- **THEN** after restart the unit derivation selects none of that batch's pages
- **AND** every unit keeps the unit id that it had

#### Scenario: Batch size does not change identity
- **WHEN** the same commits are derived once in batches of 32 pages and once one page at a time
- **THEN** every page's records are the same in both runs

#### Scenario: A hub rename derives in bounded batches
- **WHEN** a rename rewrites 10,000 linking pages in one commit
- **THEN** the unit derivation takes them in transactions of at most 32 pages

### Requirement: Unit identity follows the light rule
For each page version, the unit derivation SHALL give each unit the id of an unmatched live record of the same page by, in order: equal bytes after link normalisation; the same anchor on exactly one record; the same slot, with only whitespace changed or one run of at most 12 characters without whitespace changed. It SHALL compare a wikilink by its target's page key as the vault stood at the version's revision, keeping fragment and alias. It SHALL NOT match across pages or by similarity.

#### Scenario: A typo keeps the unit
- **WHEN** a person fixes one letter in a unit that has no anchor
- **THEN** the unit keeps its unit id

#### Scenario: A rewritten unit without an anchor is new
- **WHEN** a person rewrites the whole text of a unit that has no anchor
- **THEN** the new text gets a new unit id
- **AND** the old record retires, and the new unit's lineage names it

#### Scenario: A rewrite in unspaced text is not a one-word change
- **WHEN** a person replaces a 30-character run of a unit written without spaces
- **THEN** the unit gets a new unit id

#### Scenario: A rename keeps the units that cite the page
- **WHEN** a page moves to another folder under a new title, and link rewrites change two links inside an unanchored unit that cites it
- **THEN** the citing unit keeps its unit id, even when the citing page derives in a batch before the moved page
- **AND** a link's fragment and alias take part in the comparison

### Requirement: A declaration takes the place of the light rule
A writer SHALL be able to send, with a page's bytes, a declaration of the unit id that each unit it wrote keeps, adds, retires, moves or folds. A move or fold SHALL declare its whole effect on each page's version. A declaration that fits its version's split SHALL replace the light rule. One that does not SHALL fall back to the light rule and be recorded as `UNIT_DECLARATION_INVALID` in an owner mismatch table that status reads, not in the audit trail.

#### Scenario: An added unit's id is in the writer's reply
- **WHEN** an agent adds a unit through a unit operation
- **THEN** the reply names the unit id that the commit's revision and the unit's ordinal give
- **AND** the derived record holds that unit id

#### Scenario: An invalid declaration does not freeze the page
- **WHEN** a writer's declaration names a unit that its bytes do not hold
- **THEN** the page's identity and rows still derive, by the light rule
- **AND** status counts one declaration mismatch and names the page

### Requirement: A retired referenced unit keeps its references
When a derivation retires a unit that a stored relationship, an embed or a unit-level link names, or keeps such a unit without its anchor, the store SHALL keep those references and open one review item. The repair tool SHALL resolve it by restoring the anchor, attaching the references to a named unit, or retiring them. Retiring SHALL leave embeds and links as text.

#### Scenario: A hand edit deletes a referenced unit
- **WHEN** a person deletes a unit that carries a stored relationship
- **THEN** the relationship stays stored
- **AND** one review item names the unit and the relationship

#### Scenario: Typing after an anchor is caught
- **WHEN** a person types one word after the anchor of a unit that another page embeds
- **THEN** the unit keeps its unit id
- **AND** a review item offers to restore its anchor

#### Scenario: Attaching on the same page needs one write
- **WHEN** a review item attaches the references of a retired unit to a unit on the same page
- **THEN** that unit takes the old anchor in one page write
- **AND** every embed of the old anchor resolves to it

### Requirement: A semantic write refuses to drop a referenced unit
A semantic write SHALL check, in its preflight and across the pages of its change, whether it would retire a referenced unit or remove the anchor of one it keeps. It SHALL read inbound unit links by the rule that link reads do not trust rows that lag their page. Unless the write names the unit in `retire_units`, it SHALL refuse with `REFERENCED_UNIT_CHANGED` and name the unit and the ways forward.

#### Scenario: An agent rewrite would drop a referenced unit
- **WHEN** a governed page edit would retire a referenced unit and does not name it in `retire_units`
- **THEN** the edit refuses with `REFERENCED_UNIT_CHANGED` and commits nothing
- **AND** the refusal names the unit, `observe_memory` update and `retire_units`

#### Scenario: Registry drift does not blame the writer
- **WHEN** a registry change removed a category and an agent edits an unrelated unit of a page that the registry job has not reached
- **THEN** the edit does not refuse
- **AND** a review item names the registry change for each referenced unit of that category

### Requirement: Agents change a unit by its unit id
A writer SHALL be able to update, insert, retire, move and embed a unit by its unit id and version, as a page write with a declaration that binds the page revision and the page's records revision. Pending units SHALL be computed in memory from the durable inputs, at most 16 parses of that page. A move SHALL rewrite the unit's embeds in up to 256 other pages, else refuse with `UNIT_MOVE_TOO_WIDE`. A page whose units are `unknown` SHALL refuse with `UNITS_UNKNOWN`.

#### Scenario: A stale version refuses
- **WHEN** an agent updates a unit with a version that a later write replaced
- **THEN** the update refuses and names the current version

#### Scenario: A move past the bound refuses
- **WHEN** an agent moves a unit that 300 other pages embed
- **THEN** the move refuses with `UNIT_MOVE_TOO_WIDE` and names the count

#### Scenario: An unknown identity names its remedy
- **WHEN** an agent updates a unit on a page whose current bytes do not parse, so its units are `unknown`
- **THEN** the update refuses with `UNITS_UNKNOWN`
- **AND** the refusal names `exomem store-mode resplit --page`

### Requirement: A page read serves its units during lag
A page read SHALL never refuse because a page's units lag its bytes. It SHALL serve the page and its units, and SHALL mark each unit of a page whose units are pending as `pending`, with no unit id. When the page's units are `unknown`, it SHALL mark them `unknown`.

#### Scenario: A read right after a write
- **WHEN** a page read with units runs while the page's unit derivation is pending
- **THEN** the read returns the page bytes and every unit
- **AND** each unit is marked `pending` and carries no unit id

### Requirement: An anchor is written only for a referenced unit
The store SHALL write an anchor into a unit only when the unit has no anchor and becomes either end of its first stored relationship, or gains its first embed, in the write that adds it. The store SHALL NOT write an anchor into any other unit, and a soft reference SHALL NOT write one.

#### Scenario: A first embed pins the unit
- **WHEN** a write adds the first embed of a unit that has no anchor
- **THEN** the same commit writes an anchor at the end of that unit in its home page

#### Scenario: An unreferenced unit keeps its bytes
- **WHEN** the upgrade pass assigns records to a page whose units nothing references
- **THEN** the page bytes do not change

### Requirement: A page embeds a unit of another page
A unit SHALL live in one home page. Another page SHALL show it with a block embed of its anchor and SHALL NOT hold a copy of its bytes. A page read SHALL be able to expand embeds in one batched read, up to 64 embeds and 256 KiB, and SHALL say when it stopped at the bound. A review item SHALL list embedding pages 50 at a time with their total count.

#### Scenario: An edit at home shows everywhere
- **WHEN** a person edits a unit in its home page and another page embeds it
- **THEN** the embedding page's bytes do not change
- **AND** an expanded read of the embedding page returns the edited text

#### Scenario: An embed line is a segment
- **WHEN** a person deletes an embed line from an embedding page
- **THEN** the import changes only the embedding page
- **AND** the unit stays live in its home page

### Requirement: Trash keeps unit records
A trash SHALL keep its page's page key, set its unit records to `trashed` and keep their unit ids and stored relationships, which SHALL NOT be served while an end is trashed. Its result SHALL name the referenced units it hides. A recovery SHALL make them `live` with the same unit ids. A removal SHALL follow the hand-over rule, and a retired referenced unit SHALL open a review item.

#### Scenario: A recovered page keeps its relationships
- **WHEN** a page whose unit carries a stored relationship is trashed and then recovered
- **THEN** the unit has the same unit id and the relationship is served again

#### Scenario: A trash says what it hides
- **WHEN** an agent trashes a page with a unit that carries a stored relationship
- **THEN** the trash's result names that unit and its relationship count

### Requirement: The registry job settles units after a registry or parser change
After a change to a registry source or the parser version, the registry job SHALL split each page again by the light rule in its unit step, in its bounded batches, and blame the change in its review items. A record that retires while its page's split version is behind SHALL name the change, and also the write unless the record's normalised form still stands in the new body.

#### Scenario: A new category turns bullets into units
- **WHEN** a registry change adds a category that existing bullets use
- **THEN** the registry job gives those bullets unit records
- **AND** every other unit keeps its unit id

### Requirement: Hand edits import after they settle
The importer SHALL import a changed file of a page that holds units only after the file's bytes have stayed the same for the collection views' edit-back settle window.

#### Scenario: Typing saves do not replace a unit
- **WHEN** an editor saves every 300 ms while a person types a sentence into a unit
- **THEN** the importer commits once, after the saves stop

### Requirement: Unit records stay in parity with the page bytes
The store SHALL keep four invariants: live placements lie in the body in order, without overlap, and hash to their records' hashes; a parse of the body at its split version gives the live placements; each derived unit reference maps to one live record; derived rows equal a cold derivation of the bodies. A failed check SHALL show in status and refuse unit operations on the page until `store-mode resplit` fixes it.

#### Scenario: The upgrade leaves derived rows unchanged
- **WHEN** the upgrade pass completes on a store
- **THEN** every derived row is unchanged
- **AND** all four invariants hold

#### Scenario: A failed check has a remedy
- **WHEN** the audit finds a page whose placements do not match its body
- **THEN** unit operations on that page refuse
- **AND** `exomem store-mode resplit --page` on it restores the invariants and opens review items for retired referenced units

### Requirement: The upgrade pass is safe to interrupt and downgrade
Before its first batch, the upgrade pass SHALL record `vault-store-v2`, and a build without this change SHALL then refuse to serve the store. It SHALL settle every page's key in one transaction, then adopt stage 2's unit id for each anchored unit and give every other unit its deterministic unit id. A page that a commit reaches first SHALL take its first records from its first pending version. The store SHALL keep stage 2's anchor-to-id table in step with the anchored records.

#### Scenario: An older build cannot write during the pass
- **WHEN** a build without this change starts while the upgrade pass is half done
- **THEN** it refuses to serve and names the rollback

#### Scenario: Stored relationships stay attached
- **WHEN** the upgrade pass assigns records to a page with an anchored unit that carries stored relationships
- **THEN** the unit's record holds the unit id that stage 2 assigned

### Requirement: Unit records survive rollback
Rollback to stage 2 and to file mode SHALL export the unit records, key table, history, lineage and open review items beside the vault, and SHALL refuse while review items are open unless `--export-review` exports them. Rollback to stage 2 SHALL drop this change's owner objects, restore the `store_meta` keys it wrote, then remove `vault-store-v2`. A later migration or upgrade SHALL match an export by page key, else by path, then by the light rule.

#### Scenario: A round trip keeps unit ids
- **WHEN** a vault rolls back to file mode, a line is added at the top of a page, the page is renamed, and the vault migrates again with its identity export
- **THEN** every unit of that page keeps its unit id

#### Scenario: A rollback to stage 2 leaves stage 2's writes intact
- **WHEN** a store rolls back to stage 2 and a stage-2 build then creates and moves pages
- **THEN** those writes succeed, and a later upgrade that reads the export keeps every earlier unit id

### Requirement: This change's data stays in owner tables
Every table and index that this change adds SHALL be an owner object under `vault_store_owner_schema` that `store_mode_objects` names. This change SHALL NOT add a column or an index to a collection-schema table, and its rules SHALL read no stage-1 table other than `store_pages`.

#### Scenario: The upgrade leaves the collection schema as stage 2 wrote it
- **WHEN** the upgrade pass completes on a stage-2 store
- **THEN** the store's collection schema version, and every collection-schema table's columns and indexes, equal stage 2's

## MODIFIED Requirements

### Requirement: Semantic units are first-class store rows
In store mode, each semantic unit of a page SHALL be one derived row with its reference, page, ordinal, byte span, category, kind, text, provenance, verdict and lifecycle. The reference SHALL follow the existing identity rule and derive from the page bytes. For the same bytes, the rows SHALL reproduce file mode's lexical unit rows and graph unit nodes. Typed edges SHALL be able to start or end at a unit. A unit's row SHALL follow its page's derivation.

#### Scenario: A link reaches the exact unit
- **WHEN** a page links to another page's unit by the anchor that unit carries
- **THEN** the graph edge ends at that unit, not only at its page

#### Scenario: Refuting one claim changes one unit
- **WHEN** a governed write marks one claim on a page as refuted
- **THEN** the page bytes commit in one transaction
- **AND** once the page is derived, that unit's row shows the verdict and the other units keep their unit ids and verdicts

#### Scenario: A unit without an anchor keeps today's reference
- **WHEN** a governed write adds a unit that carries no anchor and that nothing references
- **THEN** the unit's reference is its fingerprint-bound reference
- **AND** the committed page bytes are exactly the bytes the write supplied
