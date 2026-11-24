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

### Requirement: A commit keeps the page versions that identity needs
The canonical transaction of a write SHALL write no unit record. In one set-based statement, it SHALL keep an identity step, the current bytes and pending declaration, for each page of its change that has a pending intent or whose records are behind the registry. A page SHALL keep at most 16 steps without a declaration, dropping the oldest, and SHALL keep every step with one.

#### Scenario: A large page runs no more statements at commit
- **WHEN** one write stores a page with one unit and another stores a page with 400 units
- **THEN** both canonical transactions run the same number of statements
- **AND** each page has its unit records once its identity derivation ends

#### Scenario: Two commits in one request keep both declarations
- **WHEN** one request updates a unit through a unit operation and then commits the same page again before its derivation runs
- **THEN** the identity derivation applies the first commit's declaration to its own bytes and then matches the second commit's bytes
- **AND** the unit keeps its unit id

### Requirement: Identity derives from every page version in order
The store thread SHALL derive a page's identity in its own transaction, before the rows of its batch, by walking the page's identity steps in revision order and then its current bytes. A row failure SHALL NOT hold identity back. A new unit id SHALL be a hash of the store's id, the page path, the revision and the unit's ordinal in the first version that holds the unit.

#### Scenario: A failed row derivation leaves identity current
- **WHEN** a page's row derivation fails with `no_room` and retries while two more edits commit
- **THEN** the page's records reflect all three versions in order
- **AND** a unit operation on the page proceeds

#### Scenario: A kill before the derivation keeps identity
- **WHEN** the process is killed after a write commits and before its identity derivation commits
- **THEN** after restart the derivation gives every unit the same unit id, new units included

#### Scenario: A split batch gives the same identity
- **WHEN** the drain re-runs a failed batch one path at a time
- **THEN** every page's records equal those that the whole batch would have given

### Requirement: Unit identity follows the light rule
For each page version, the identity derivation SHALL give each unit the id of an unmatched live record of the same page by, in order: equal bytes; the same anchor on exactly one record; the same slot, with only whitespace changed or one run of at most 12 characters without whitespace changed. It SHALL compare a wikilink by the page that the resolver gives it, through its prior-path step. It SHALL NOT match across pages or by similarity.

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
- **THEN** the citing unit keeps its unit id

### Requirement: A declaration takes the place of the light rule
A writer SHALL be able to send, with a page's bytes, a declaration of the unit id that each unit it wrote keeps, adds, retires, moves or folds. A move or fold SHALL declare its whole effect on each page's intent. A declaration that fits its version's split SHALL replace the light rule. One that does not SHALL fall back to the light rule and be recorded as `UNIT_DECLARATION_INVALID` in status and the audit trail.

#### Scenario: An added unit's id is in the writer's reply
- **WHEN** an agent adds a unit through a unit operation
- **THEN** the reply names the unit id that the commit's revision and the unit's ordinal give
- **AND** the derived record holds that unit id

#### Scenario: An invalid declaration does not freeze the page
- **WHEN** a writer's declaration names a unit that its bytes do not hold
- **THEN** the page's identity and rows still derive, by the light rule
- **AND** status counts one declaration mismatch and the audit trail names the page

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
A writer SHALL be able to update, insert, retire, move and embed a unit by its unit id and version, as a page write with a declaration that binds the page revision and the page's records revision. A pending identity SHALL be computed in memory from the durable inputs. A move SHALL rewrite the unit's embeds in up to 256 other pages, else refuse with `UNIT_MOVE_TOO_WIDE`. A page whose identity is `unknown` SHALL refuse with `UNITS_UNKNOWN`.

#### Scenario: A stale version refuses
- **WHEN** an agent updates a unit with a version that a later write replaced
- **THEN** the update refuses and names the current version

#### Scenario: A move past the bound refuses
- **WHEN** an agent moves a unit that 300 other pages embed
- **THEN** the move refuses with `UNIT_MOVE_TOO_WIDE` and names the count

#### Scenario: An unknown identity names its remedy
- **WHEN** an agent updates a unit on a page whose current bytes do not parse
- **THEN** the update refuses with `UNITS_UNKNOWN`
- **AND** the refusal names `exomem store-mode resplit --page`

### Requirement: A page read serves its units during lag
A page read SHALL never refuse because a page's identity lags its bytes. It SHALL serve the page and its units, and SHALL mark each unit of a page whose identity is pending as `pending`, with no unit id. When the page's identity is `unknown`, it SHALL mark its units `unknown`.

#### Scenario: A read right after a write
- **WHEN** a page read with units runs while the page's identity derivation is pending
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
A trash SHALL set its page's unit records to `trashed` and keep their unit ids and stored relationships, which SHALL NOT be served while an end is trashed. Its result SHALL name the referenced units it hides. A recovery SHALL make them `live` with the same unit ids. A removal of a page from the store SHALL retire its records, and a retired referenced unit SHALL open a review item.

#### Scenario: A recovered page keeps its relationships
- **WHEN** a page whose unit carries a stored relationship is trashed and then recovered
- **THEN** the unit has the same unit id and the relationship is served again

#### Scenario: A trash says what it hides
- **WHEN** an agent trashes a page with a unit that carries a stored relationship
- **THEN** the trash's result names that unit and its relationship count

### Requirement: The registry job settles units after a registry or parser change
After a change to a registry source or the parser version, the registry job SHALL split each page again by the light rule in its unit step, in its bounded batches, and blame the change in its review items. A page's rows and records SHALL come from one registry view. A derivation of a behind page SHALL apply that step to its oldest identity step before it matches newer bytes.

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
The store SHALL keep four invariants: live placements lie in the body in order, without overlap, and hold their records' bytes; a parse of the body at its split version gives the live placements; each derived unit reference maps to one live record; derived rows equal a cold derivation of the bodies. A failed check SHALL show in status and refuse unit operations on the page until `store-mode resplit` fixes it.

#### Scenario: The upgrade leaves derived rows unchanged
- **WHEN** the upgrade pass completes on a store
- **THEN** every derived row is unchanged apart from its unit id
- **AND** all four invariants hold

#### Scenario: A failed check has a remedy
- **WHEN** the audit finds a page whose placements do not match its body
- **THEN** unit operations on that page refuse
- **AND** `exomem store-mode resplit --page` on it restores the invariants and opens review items for retired referenced units

### Requirement: The upgrade pass is safe to interrupt and downgrade
Before its first batch, the upgrade pass SHALL record `vault-store-v2`, and a build without this change SHALL then refuse to serve the store. The pass SHALL adopt stage 2's unit id for each anchored unit and give every other unit its deterministic unit id. A derivation of a page beyond its cursor SHALL assign that page's records first. The store SHALL keep stage 2's anchor-to-id table in step with the anchored records.

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

### Requirement: Derived unit rows carry the unit id
In store mode, each derived unit row SHALL carry its unit's `unit_id`, and no other derived column SHALL change. Every comparison of store-mode derived rows with file mode's SHALL set `unit_id` aside, as it sets aside the columns of write time and write order.

#### Scenario: Store mode still matches file mode
- **WHEN** the same page bytes are derived in file mode and in store mode
- **THEN** the derived rows are equal apart from write time, write order and `unit_id`

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
