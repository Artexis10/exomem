## ADDED Requirements

### Requirement: A saved soft reference anchors its unit
When a reader or writer saves a soft reference to an unanchored unit on a store-mode page, the store SHALL write that unit's anchor later, in a governed write on the store thread. The save SHALL NOT wait or fail for it. The write SHALL need the saver's write admission, bind the page revision and a projected file, and wait while the file changed within the settle window. A request whose unit changed first SHALL be dropped.

#### Scenario: A read saves a reference to an unanchored unit
- **WHEN** a recall response saves a working-set entry that names a unit without an anchor
- **THEN** the response returns without writing the page
- **AND** once the page's file is quiet, one governed write adds only the unit's anchor

#### Scenario: The reference survives a later edit
- **WHEN** the anchor has landed and a person then edits the unit's text and keeps the anchor
- **THEN** the saved reference still resolves to the unit

#### Scenario: A busy file waits
- **WHEN** the page's file changed within the settle window, or an outside change to it is held
- **THEN** the anchor write waits for a later step
- **AND** no held item opens because of it

#### Scenario: A changed unit gets no anchor
- **WHEN** the unit's text changes before its anchor lands
- **THEN** the store drops the request and writes nothing
- **AND** the saved reference is stale, as it is today

#### Scenario: A reader that may not write gets no anchor
- **WHEN** the saving principal may read but not write the page
- **THEN** the store writes no anchor into it

### Requirement: A replaced reference resolves through its alias
When the store replaces a unit's reference, by writing its anchor or by moving the unit, it SHALL record an alias from the old reference to the new one in an owner table, in the same transaction. Reference resolution SHALL follow an alias, with one indexed lookup, when the exact match is stale or missing. A later move SHALL update every alias that points at the reference it replaces.

#### Scenario: A moved unit resolves at its old reference
- **WHEN** an agent moves a unit to another page and a reader later resolves the unit's old reference
- **THEN** the reference resolves to the unit on its new page

#### Scenario: Two moves still take one lookup
- **WHEN** a unit that a soft reference named is anchored and then moved twice
- **THEN** the saved reference resolves to the unit with one alias lookup

### Requirement: An anchor is written only for a named unit
The store SHALL write an anchor into a unit, in the unit's own anchor form, only when the unit has none and becomes either end of its first stored relationship, gains its first embed, is named by a unit operation, or is named by a saved soft reference. Writing it into another page SHALL need write admission on that page. The store SHALL NOT write an anchor into any other unit.

#### Scenario: A first embed pins the unit
- **WHEN** a write adds the first embed of a unit that has no anchor
- **THEN** the same commit writes the unit's anchor in its home page

#### Scenario: An unnamed unit keeps its bytes
- **WHEN** a page with unanchored units is read, searched and recalled, and no soft reference to its units is saved
- **THEN** the page bytes do not change

### Requirement: Agents change a unit by its reference
A writer SHALL be able to update, insert, retire, move and embed a unit by its reference, as a page write that binds the page revision. An operation on an unanchored unit SHALL write its anchor in the same commit and reply with the anchored reference. A move SHALL keep the unit id and stored relationships and rewrite embeds in up to 256 pages, else refuse with `UNIT_MOVE_TOO_WIDE`. An anchor needed in a visible page the caller may not write SHALL refuse with `UNIT_PAGE_NOT_WRITABLE`.

#### Scenario: An edited unit keeps its reference
- **WHEN** an agent updates the text of a unit without an anchor three times through `observe_memory`
- **THEN** the first update anchors the unit and every reply names the same anchored reference
- **AND** a reference saved before the first update resolves to the unit

#### Scenario: A moved unit keeps its relationships
- **WHEN** an agent moves a unit that carries a stored relationship to another page
- **THEN** its unit id and the relationship stay the same

#### Scenario: A stale revision refuses
- **WHEN** an agent updates a unit with a page revision that a later write replaced
- **THEN** the update refuses and names the current revision

#### Scenario: A move past the bound refuses
- **WHEN** an agent moves a unit that 300 other pages embed
- **THEN** the move refuses with `UNIT_MOVE_TOO_WIDE` and names the count

### Requirement: A page embeds a unit of another page
A unit SHALL live in one home page. Another page SHALL show it with a block embed of its anchor and SHALL NOT hold a copy of its bytes. A page read SHALL be able to expand embeds in one batched read, up to 64 embeds and 256 KiB, and SHALL say when it stopped at the bound.

#### Scenario: An edit at home shows everywhere
- **WHEN** a person edits a unit in its home page and another page embeds it
- **THEN** the embedding page's bytes do not change
- **AND** an expanded read of the embedding page returns the edited text

#### Scenario: An embed line is part of its page
- **WHEN** a person deletes an embed line from an embedding page
- **THEN** the import changes only the embedding page
- **AND** the unit stays in its home page

### Requirement: A semantic write refuses to drop a referenced unit
A semantic write SHALL check, in its preflight and across the pages of its change, whether a referenced unit loses its anchor, by removal of the unit or of the anchor. It SHALL read inbound unit links by the rule that link reads do not trust rows that lag their page. Unless the write names the unit in `retire_units`, it SHALL refuse with `REFERENCED_UNIT_CHANGED` and name the unit and the ways forward.

#### Scenario: An agent rewrite would drop a referenced unit
- **WHEN** a governed page edit would remove an embedded unit and does not name it in `retire_units`
- **THEN** the edit refuses with `REFERENCED_UNIT_CHANGED` and commits nothing
- **AND** the refusal names the unit, `observe_memory` update and `retire_units`

#### Scenario: A soft reference does not block a write
- **WHEN** a semantic write removes a unit that only a saved soft reference names
- **THEN** the write commits

#### Scenario: Registry drift does not blame the writer
- **WHEN** a registry change removed a category and an agent edits an unrelated unit of the page
- **THEN** the edit does not refuse

### Requirement: A removed referenced unit keeps its references
When an edit removes a unit that a stored relationship, an embed or a unit-level link names, or keeps it without its anchor, the store SHALL keep those references and open one review item. The item SHALL list embedding pages 50 at a time with their total. The repair tool SHALL resolve it by restoring the anchor, attaching the references to a unit on the same page, or retiring them; retiring SHALL leave embeds and links as text.

#### Scenario: A hand edit deletes an embedded unit
- **WHEN** a person deletes a unit that another page embeds
- **THEN** one review item names the unit and the embedding page

#### Scenario: Typing after an anchor is caught
- **WHEN** a person types one word after the anchor of a unit that another page embeds
- **THEN** a review item offers to restore its anchor

#### Scenario: Attaching on the same page needs one write
- **WHEN** a review item attaches the references of a removed unit to a unit on the same page
- **THEN** that unit takes the old anchor in one page write
- **AND** every embed of the old anchor resolves to it

### Requirement: Hand edits import after they settle
The importer SHALL import a changed file of a page that holds referenced units only after the file's bytes have stayed the same for the collection views' edit-back settle window.

#### Scenario: Typing saves do not open review items
- **WHEN** an editor saves every 300 ms while a person types a sentence into a referenced unit
- **THEN** the importer commits once, after the saves stop

### Requirement: This change's data stays in owner tables
Every table and index that this change adds SHALL be an owner object under `vault_store_owner_schema` that `store_mode_objects` names. This change SHALL NOT add a column or an index to a collection-schema table. A stage-2 build SHALL serve a store that this change ran on.

#### Scenario: The collection schema stays as stage 2 wrote it
- **WHEN** this change's build has run on a stage-2 store
- **THEN** the store's collection schema version, and every collection-schema table's columns and indexes, equal stage 2's

#### Scenario: A stage-2 build serves the store
- **WHEN** a stage-2 build starts on a store that holds aliases and pending anchor requests
- **THEN** it serves the store
- **AND** when this change's build returns, the aliases resolve and the requests run

### Requirement: Aliases survive a rollback to file mode
Rollback to file mode SHALL export the aliases beside the vault with its other exports, and a later migration SHALL load them.

#### Scenario: A round trip keeps a moved unit's old reference
- **WHEN** an agent moves a unit, the vault rolls back to file mode and later migrates again with its export
- **THEN** in store mode the unit's old reference resolves to the unit again

## MODIFIED Requirements

### Requirement: Semantic units are first-class store rows
In store mode, each semantic unit of a page SHALL be one derived row with its identity, page, ordinal, byte span, category, kind, text, provenance, verdict and lifecycle. Its identity SHALL follow the existing rule, the anchor else the fingerprint-bound reference, derived from the page bytes. The store SHALL write an anchor only for a named unit. For the same bytes, the rows SHALL reproduce file mode's lexical unit rows and graph unit nodes. Typed edges SHALL be able to start or end at a unit.

#### Scenario: A link reaches the exact unit
- **WHEN** a page links to another page's unit by the anchor that unit carries
- **THEN** the graph edge ends at that unit, not only at its page

#### Scenario: Refuting one claim changes one unit
- **WHEN** a governed write marks one claim on a page as refuted
- **THEN** the page bytes commit in one transaction
- **AND** once the page is derived, that unit's row shows the verdict and the other units keep their identities and verdicts

#### Scenario: A unit that nothing names keeps today's identity
- **WHEN** a governed write adds a unit that carries no anchor and that nothing names
- **THEN** the unit's identity is its fingerprint-bound reference
- **AND** the committed page bytes are exactly the bytes the write supplied
