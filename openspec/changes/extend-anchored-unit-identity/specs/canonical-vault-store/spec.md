## ADDED Requirements

### Requirement: A saved soft reference anchors its unit
When a reader or writer saves a soft reference to an unanchored unit on a store-mode page, it SHALL hand a request to the store thread without waiting or failing. The store thread SHALL later write the unit's anchor in one governed write that needs a requester's write admission and binds the page revision. It SHALL wait, with backoff, while the file is not projected, differs from the manifest or changed within the settle window. A request whose unit changed SHALL end.

#### Scenario: A read saves a reference to an unanchored unit
- **WHEN** a recall response saves a working-set entry that names a unit without an anchor
- **THEN** the response returns without writing the page
- **AND** once the page's file is quiet, one governed write adds only the unit's anchor

#### Scenario: The reference survives a later edit
- **WHEN** the anchor has landed and a person then edits the unit's text and keeps the anchor
- **THEN** the saved reference still resolves to the unit

#### Scenario: A busy file waits
- **WHEN** the page's file changed within the settle window, holds a save that the importer has not imported, or has an outside change held
- **THEN** the anchor write waits for a later attempt
- **AND** no held item opens because of it

#### Scenario: A changed unit gets no anchor
- **WHEN** the unit's text changes before its anchor lands
- **THEN** the store ends the request and writes nothing
- **AND** the saved reference is stale, as it is today

#### Scenario: A reader that may not write gets no anchor
- **WHEN** no principal that saved a reference to the unit may write its page
- **THEN** the store writes no anchor into it

### Requirement: A replaced reference resolves through its alias
When the store replaces a unit's reference, by writing its anchor or moving the unit, it SHALL record an alias from the old reference to the unit's stage-2 unit id in an owner table, in the same transaction. A reference with an alias SHALL resolve through it alone, with one indexed lookup, and SHALL NOT fall back to an exact match. A write SHALL follow an alias as a read does.

#### Scenario: An identical unit does not take a saved reference
- **WHEN** a page holds two identical unanchored units, a soft reference names the first, and the store anchors it
- **THEN** the saved reference resolves to the first unit, not to the second unit that now has its old reference

#### Scenario: An alias whose unit lost its anchor is stale
- **WHEN** an aliased unit later loses its anchor and an identical unit then carries the old reference
- **THEN** the saved reference answers stale and does not resolve to the identical unit

#### Scenario: A moved unit resolves at its old reference
- **WHEN** an agent moves a unit to another page and a reader later resolves the unit's old reference
- **THEN** the reference resolves to the unit on its new page

#### Scenario: Two moves still take one lookup
- **WHEN** a unit that a soft reference named is anchored and then moved twice
- **THEN** the saved reference resolves to the unit with one alias lookup

### Requirement: The store itself writes an anchor only for a named unit
The store itself SHALL write an anchor, in the unit's own anchor form, only into a unit that carries no anchor and that becomes either end of its first stored relationship or the target of its first embed, in the governed write that adds it, or that a saved soft reference names, in a deferred write. A caller's governed write may still author anchors.

#### Scenario: A first embed pins the unit
- **WHEN** a write adds the first embed of a unit that has no anchor
- **THEN** the same commit writes the unit's anchor in its home page

#### Scenario: An unnamed unit keeps its bytes
- **WHEN** a page with unanchored units is read, searched and recalled, and no soft reference to its units is saved
- **THEN** the page bytes do not change

### Requirement: Unit rows reproduce file mode and change with their page
For the same bytes, the unit rows SHALL reproduce file mode's lexical semantic-unit rows and graph unit nodes. Typed edges SHALL be able to start or end at a unit as well as at a page. A governed write that changes a unit SHALL change the page bytes and the unit row in the same transaction.

#### Scenario: A link reaches the exact unit
- **WHEN** a page links to another page's unit by the anchor that unit carries
- **THEN** the graph edge ends at that unit, not only at its page

#### Scenario: Refuting one claim changes one unit
- **WHEN** a governed write marks one claim on a page as refuted
- **THEN** the page bytes and that unit's verdict commit in one transaction
- **AND** the other units on the page keep their identities and verdicts

### Requirement: Agents change a unit by its reference
A writer SHALL be able to update, insert, retire, move and embed a unit by its reference, as a page write that binds the revision it read, a caller's revision being optional. An operation on an unanchored unit SHALL write its anchor in the same commit and reply with the anchored reference. A stale reference SHALL refuse with `STALE_UNIT_REFERENCE`. A move SHALL keep the unit id and rewrite inbound embeds and unit links up to a configured bound, else refuse with `UNIT_MOVE_TOO_WIDE`.

#### Scenario: An edited unit keeps its reference
- **WHEN** an agent updates the text of a unit without an anchor three times through `observe_memory`
- **THEN** the first update anchors the unit and every reply names the same anchored reference
- **AND** a reference saved before the first update resolves to the unit

#### Scenario: A move keeps relationships and links
- **WHEN** an agent moves a unit that carries a stored relationship and that another page links to as `[[Old#^a]]`
- **THEN** its unit id and the relationship stay the same
- **AND** the link names the unit's new page
- **AND** the move does not refuse with `REFERENCED_UNIT_CHANGED`

#### Scenario: A stale reference refuses
- **WHEN** an agent updates a unit by a fingerprint-bound reference that matches nothing and has no alias
- **THEN** the update refuses with `STALE_UNIT_REFERENCE` and the page is unchanged

#### Scenario: A move past the bound refuses
- **WHEN** an agent moves a unit whose referring pages in its scope exceed the move bound
- **THEN** the move refuses with `UNIT_MOVE_TOO_WIDE`, names the count and suggests embedding the unit from the destination instead

### Requirement: A page embeds a unit of another page
A unit SHALL live in one home page. Another page SHALL show it with a block embed and SHALL NOT hold a copy of its bytes. A rich unit with an `id` SHALL have a block id only at the end of its body's first paragraph: one already there, else one its first embed writes. A body with no paragraph SHALL refuse with `UNIT_EMBED_NO_PARAGRAPH`. A promotion SHALL keep a compact unit's block id. A page read SHALL expand embeds one level deep, up to 64 embeds and 256 KiB.

#### Scenario: An edit at home shows everywhere
- **WHEN** a person edits a unit in its home page and another page embeds it
- **THEN** the embedding page's bytes do not change
- **AND** an expanded read of the embedding page returns the edited text

#### Scenario: An embed line is part of its page
- **WHEN** a person deletes an embed line from an embedding page
- **THEN** the import changes only the embedding page
- **AND** the unit stays in its home page

#### Scenario: A rich unit's embed renders
- **WHEN** a write adds the first embed of a rich unit whose `id` is a valid block id
- **THEN** the first paragraph of the unit's body ends with a block id equal to its `id`
- **AND** the unit's identity, content and fingerprint stay the same

#### Scenario: A person's block id is reused
- **WHEN** the first paragraph of a rich unit's body already ends with `^xyz` and a write adds its first embed
- **THEN** the embed names `^xyz` and the store writes no second block id

#### Scenario: An id that is not a block id gets a generated one
- **WHEN** a write adds the first embed of a rich unit whose `id` is `claim_1`
- **THEN** the store writes a generated block id of letters, digits and hyphens, unique on the page

#### Scenario: A promotion keeps an embed rendering
- **WHEN** a write promotes an embedded compact unit to a rich block
- **THEN** the rich block's first content paragraph keeps the compact line's block id

### Requirement: A semantic write refuses to drop a referenced unit
A semantic write SHALL check, in its preflight and with one batched query, whether a referenced unit loses its anchor, a unit that a same-page link names included and a unit that it moves counted as kept. It SHALL read inbound unit links by the rule that link reads do not trust rows that lag their page. Unless the write names the unit in `retire_units`, it SHALL refuse with `REFERENCED_UNIT_CHANGED` and name the unit and the ways forward.

#### Scenario: An agent rewrite would drop a referenced unit
- **WHEN** a governed page edit would remove an embedded unit and does not name it in `retire_units`
- **THEN** the edit refuses with `REFERENCED_UNIT_CHANGED` and commits nothing
- **AND** the refusal names the unit, `observe_memory` update and `retire_units`

#### Scenario: A same-page supersession target is protected
- **WHEN** a write removes unit A while unit B on the same page carries `supersedes: [[#a]]`
- **THEN** the write refuses with `REFERENCED_UNIT_CHANGED` unless it names A in `retire_units`

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

### Requirement: Cross-page unit answers apply two-ended visibility
Embed expansion, the referenced-unit refusal, the move count and review items SHALL answer a page that the caller may not see as absent and SHALL NOT count it. An alias whose unit the caller may not see SHALL answer as a stale reference. A unit move SHALL rewrite referring pages only within `MoverView.scope` and only those it may write; any other page SHALL keep its link, resolved through the alias, and SHALL appear in an owner review item.

#### Scenario: An invisible home expands to nothing
- **WHEN** a reader expands an embed whose home page the reader may not see
- **THEN** the embed is answered as a missing unit and is not counted against the bound

#### Scenario: A restricted mover leaves a hidden embed
- **WHEN** a restricted agent, connector-limited or not, moves a unit that a page it cannot see embeds
- **THEN** the move neither counts nor rewrites that page
- **AND** Exomem resolves that page's embed through the alias
- **AND** an owner review item lists that page

#### Scenario: An alias to a hidden unit answers stale
- **WHEN** a reader resolves a reference whose alias names a unit on a page that the reader may not see
- **THEN** the answer is stale, as for a reference that matches nothing
- **AND** a write by that reference refuses with `STALE_UNIT_REFERENCE`

#### Scenario: A hidden reference does not refuse a write
- **WHEN** a write drops a unit that only a page the writer cannot see embeds
- **THEN** the write commits and its reply names no review item
- **AND** the owner's review item names that page, and a reader who sees none of its referrers does not see the item

#### Scenario: An unwritable visible page keeps its embed
- **WHEN** an agent moves a unit that a page it may see but not write embeds
- **THEN** the move commits and that page keeps its embed, which resolves through the alias
- **AND** the reply names that page

### Requirement: Semantic-unit identity data stays in owner tables
Every table and index that this change adds SHALL be an owner object under `vault_store_owner_schema`, named in `store_mode_objects` and created by the registry pass. It SHALL NOT add a column or an index to a collection-schema table. A build without these tables SHALL serve the store.

#### Scenario: The collection schema stays as stage 2 wrote it
- **WHEN** this change's build has run on a stage-2 store
- **THEN** the store's collection schema version, and every collection-schema table's columns and indexes, equal stage 2's

#### Scenario: An earlier build serves the store
- **WHEN** a build without these tables starts on a store that holds aliases and pending anchor requests
- **THEN** it serves the store
- **AND** when a build with them returns, the aliases resolve and the requests run

### Requirement: Aliases survive a rollback to file mode
Before it projects, rollback to file mode SHALL rewrite every embed and unit-level link that resolves only through an alias to the unit's current reference. It SHALL export the aliases beside the vault with its other exports, and a later migration SHALL load them.

#### Scenario: A page left on an alias matches after rollback
- **WHEN** a restricted move leaves a hidden page's embed on the alias and the vault rolls back to file mode
- **THEN** that page's embed names the unit's current page, and file mode's graph answer for it matches the store's

#### Scenario: A round trip keeps a moved unit's old reference
- **WHEN** an agent moves a unit, the vault rolls back to file mode and later migrates again with its export
- **THEN** in store mode the unit's old reference resolves to the unit again

## MODIFIED Requirements

### Requirement: Semantic units are first-class store rows
In store mode, once stage 2 ships, each semantic unit of a page SHALL be one store row with its identity, its page, its ordinal and exact byte span in the page bytes, its category, kind and text, its provenance, its verdict and its lifecycle. The identity SHALL follow the existing semantic unit identity rule: the authored anchor when the page carries one, otherwise the fingerprint-bound reference. The store SHALL derive the identity from the page bytes.

#### Scenario: A unit without an anchor keeps today's identity
- **WHEN** a governed write adds a unit that carries no authored anchor and no stored relationship
- **THEN** the unit's identity is its fingerprint-bound reference
- **AND** the committed page bytes are exactly the bytes the write supplied
