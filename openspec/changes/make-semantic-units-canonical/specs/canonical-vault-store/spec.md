## ADDED Requirements

### Requirement: A unit record keeps a unit's identity
In store mode, each unit of a page that its derivation gives unit rows SHALL have a unit record with a store-assigned unit id, its placement, its state and its lineage. The unit id SHALL NOT depend on the unit's text or page. A history SHALL map every unit reference that a unit had to its unit id. Stored relationships and embeds SHALL name unit ids. A page that the derivation does not index SHALL have no unit records.

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

### Requirement: Unit records follow their page's derivation
The canonical transaction of a write SHALL write no unit record. The store thread SHALL write a page's unit records when it derives the page, from its stored bytes, its live records, the writer's declaration on the intent and, when the page was behind its split, its prior bytes. A unit operation, or a read of unit ids, on a page whose intent is pending or failed SHALL refuse with `UNITS_BEHIND` and a retry hint.

#### Scenario: A large page runs no more statements at commit
- **WHEN** one write stores a page with one unit and another stores a page with 400 units
- **THEN** both canonical transactions run the same number of statements
- **AND** each page has its unit records once its derivation ends

#### Scenario: A kill before the derivation keeps identity
- **WHEN** the process is killed after a write commits and before its derivation runs
- **THEN** after restart the derivation gives every unit the same unit id that it would have given without the kill

### Requirement: Unit identity follows the light rule
A derivation SHALL give each new unit the id of an unmatched live record of its page by, in order: equal bytes; the same anchor on exactly one record; the same slot, with only whitespace changed or one inserted, deleted or substituted run of at most 12 characters without whitespace. Across the pages of one revision, equal bytes or a unique anchor SHALL move an id. No similarity score SHALL decide identity.

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

#### Scenario: A unit that moves between pages in one write keeps its id
- **WHEN** one write removes a unit from one page and adds the same bytes to another page
- **THEN** the unit keeps its unit id and its lineage names the first page

### Requirement: A declaration takes the place of the light rule
A writer SHALL be able to send, with a page's bytes, a declaration of which unit id each unit it wrote keeps, adds or retires, and MAY mint the unit id of a unit it adds. The derivation SHALL apply a declaration that fits the split, and SHALL keep the intent as failed with `UNIT_DECLARATION_INVALID` when it does not. A commit that coalesces with a pending intent SHALL drop that intent's declaration.

#### Scenario: An added unit's id is in the writer's reply
- **WHEN** an agent adds a unit through a unit operation
- **THEN** the reply names the unit id before the derivation runs
- **AND** the derived record holds that unit id

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
- **WHEN** a registry change removed a category and an agent edits an unrelated unit of a page that the split pass has not reached
- **THEN** the edit does not refuse
- **AND** a review item names the registry change for each referenced unit of that category

### Requirement: Agents change a unit by its unit id
A writer SHALL be able to update, insert, retire, move and embed a unit by its unit id and version, as a page write with a declaration that binds the page revision and the page's records revision. A move SHALL keep the unit id and rewrite its embeds in up to 256 other pages in the same commit, and SHALL refuse with `UNIT_MOVE_TOO_WIDE` beyond that. An embed of an unanchored unit SHALL write its anchor in the same commit.

#### Scenario: A stale version refuses
- **WHEN** an agent updates a unit with a version that a later write replaced
- **THEN** the update refuses and names the current version

#### Scenario: A move past the bound refuses
- **WHEN** an agent moves a unit that 300 other pages embed
- **THEN** the move refuses with `UNIT_MOVE_TOO_WIDE` and names the count

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
A move of a page into the trash SHALL set its unit records to `trashed` and keep their unit ids and stored relationships, which SHALL NOT be served while an end is trashed. A recovery SHALL make them `live` with the same unit ids. A purge or governed deletion SHALL retire them and their stored relationships, and its result SHALL list the relationships it retired.

#### Scenario: A recovered page keeps its relationships
- **WHEN** a page whose unit carries a stored relationship is trashed and then recovered
- **THEN** the unit has the same unit id and the relationship is served again

### Requirement: A registry or parser change splits pages again
A commit that changes a registry source, and a release that changes the parser version, SHALL queue a split pass that splits the affected pages again by the light rule, in bounded batches under the derived-row authority. While it is pending, a commit SHALL keep the prior bytes of each behind page in one set-based statement, and that page's derivation SHALL apply the pass's step to them first.

#### Scenario: A new category turns bullets into units
- **WHEN** a registry change adds a category that existing bullets use
- **THEN** the split pass gives those bullets unit records
- **AND** every other unit keeps its unit id
- **AND** status reports `splitting` with counts until the pass ends

### Requirement: Hand edits import after they settle
The importer SHALL import a changed file of a page that holds units only after the file's bytes have stayed the same for the collection views' edit-back settle window.

#### Scenario: Typing saves do not replace a unit
- **WHEN** an editor saves every 300 ms while a person types a sentence into a unit
- **THEN** the importer commits once, after the saves stop

### Requirement: Unit records stay in parity with the page bytes
The store SHALL keep four invariants: live placements lie in the body in order, without overlap, and hash to their records; a parse of the body at its split version gives the live placements; each derived unit reference maps to one live record; derived rows equal a cold derivation of the bodies. A failed check SHALL show in status and refuse unit operations on the page until `store-mode resplit` fixes it.

#### Scenario: The upgrade leaves derived rows unchanged
- **WHEN** the upgrade pass completes on a store
- **THEN** every derived row is unchanged apart from its unit id
- **AND** all four invariants hold

#### Scenario: A failed check has a remedy
- **WHEN** the audit finds a page whose placements do not match its body
- **THEN** unit operations on that page refuse
- **AND** `exomem store-mode resplit --page` on it restores the invariants and opens review items for retired referenced units

### Requirement: The upgrade pass is safe to interrupt and downgrade
Before its first batch, the upgrade pass SHALL record `vault-store-v2`, and a build without this change SHALL then refuse to serve the store. The pass SHALL adopt stage 2's unit id for each anchored unit. A derivation of a page beyond its cursor SHALL assign that page's records first. The store SHALL keep stage 2's anchor-to-id table in step with the anchored records.

#### Scenario: An older build cannot write during the pass
- **WHEN** a build without this change starts while the upgrade pass is half done
- **THEN** it refuses to serve and names the rollback

#### Scenario: Stored relationships stay attached
- **WHEN** the upgrade pass assigns records to a page with an anchored unit that carries stored relationships
- **THEN** the unit's record holds the unit id that stage 2 assigned

### Requirement: Unit records survive rollback
Rollback to stage 2 and to file mode SHALL export the unit records with their bytes, the reference history, the lineage and the open review items beside the vault, and SHALL refuse while review items are open unless `--export-review` exports them. Rollback to stage 2 SHALL remove `vault-store-v2` after its commit. A later migration SHALL match an identity export to the bytes by the light rule.

#### Scenario: A round trip keeps unit ids
- **WHEN** a vault rolls back to file mode, a line is added at the top of a page, and the vault migrates again with its identity export
- **THEN** every unit of that page keeps its unit id

## MODIFIED Requirements

### Requirement: Semantic units are first-class store rows
In store mode, each semantic unit of a page SHALL be one derived row with its reference, page, ordinal, byte span, category, kind, text, provenance, verdict, lifecycle and unit id. The reference SHALL follow the existing identity rule and derive from the page bytes. For the same bytes, the rows SHALL reproduce file mode's lexical unit rows and graph unit nodes, apart from `unit_id`. Typed edges SHALL be able to start or end at a unit. A unit's row SHALL follow its page's derivation.

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
