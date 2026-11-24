## ADDED Requirements

### Requirement: A unit record keeps a unit's identity
In store mode, each unit of a page SHALL have a unit record with a store-assigned unit id, its placement, its state and its lineage. The unit id SHALL NOT depend on the unit's text or page. A history SHALL map every unit reference that a unit had to its unit id. Stored relationships, embeds and generated pages SHALL name unit ids.

#### Scenario: An edited unit keeps its unit id
- **WHEN** an agent rewrites the text of a unit that has no anchor through a unit operation
- **THEN** the unit keeps its unit id
- **AND** a unit reference saved before the edit resolves to the unit

#### Scenario: A moved unit keeps its unit id
- **WHEN** an agent moves a unit to another page
- **THEN** its unit id and its stored relationships stay the same
- **AND** its lineage names the page it moved from

### Requirement: A write commits the records of the units it changes
In store mode, the transaction of a write SHALL also write the records of the units that the write adds, changes, moves or retires, and those statements SHALL grow only with those units. The writer SHALL split the new bytes before it takes the vault mutation boundary. The commit SHALL split them again under the boundary when the parser or the registry changed since.

#### Scenario: A one-unit edit writes one record
- **WHEN** a write changes one unit of a page that holds 100 units
- **THEN** the transaction writes that unit's record and one statement that moves the later placements

#### Scenario: The registry changed between split and commit
- **WHEN** a registry change commits after a writer split a page and before its commit
- **THEN** the commit splits the page again under the boundary before it matches units

### Requirement: Unit identity follows the light rule
When a write supplies a page's bytes, the commit SHALL give each new unit the id of an unmatched record by, in order: equal bytes; the same anchor on exactly one record; the same slot between the same matched neighbours with only whitespace or one word changed. Any other new unit SHALL get a new unit id, and an unmatched record SHALL retire. No similarity score SHALL decide identity.

#### Scenario: A typo keeps the unit
- **WHEN** a person fixes one letter in a unit that has no anchor
- **THEN** the unit keeps its unit id

#### Scenario: A rewritten unit without an anchor is new
- **WHEN** a person rewrites the whole text of a unit that has no anchor
- **THEN** the new text gets a new unit id
- **AND** the old record retires, and the new unit's lineage names it

#### Scenario: A declaration takes the place of the rule
- **WHEN** a writer declares which unit id each changed unit keeps
- **THEN** the commit applies the declaration when it fits the split
- **AND** refuses with `UNIT_DECLARATION_INVALID` when it does not

### Requirement: A retired referenced unit keeps its references
When a commit would retire a unit that a stored relationship, an embed, a unit-level link or a generated page names, an outside-editor commit SHALL keep those references and open one review item. A governed commit without a declaration SHALL refuse with `UNIT_SUCCESSOR_REQUIRED`. A review item SHALL offer to attach the references to a named unit or to retire them.

#### Scenario: A hand edit deletes a referenced unit
- **WHEN** a person deletes a unit that carries a stored relationship
- **THEN** the relationship stays stored
- **AND** one review item names the unit and the relationship

#### Scenario: An agent rewrite would drop a referenced unit
- **WHEN** a governed page write would retire a referenced unit and declares nothing for it
- **THEN** the write refuses with `UNIT_SUCCESSOR_REQUIRED` and commits nothing
- **AND** the refusal names the unit and the two choices

### Requirement: Agents change a unit by its unit id
In store mode, a store change SHALL be able to update, insert, retire and move a unit by its unit id and version, and SHALL bind the page revision. A move SHALL keep the unit id, SHALL rewrite the unit's embeds in other pages in the same transaction, and SHALL refuse when they exceed its bound. The unit id that an agent sees SHALL stay the same across edits and moves.

#### Scenario: A stale version refuses
- **WHEN** an agent updates a unit with a version that a later write replaced
- **THEN** the update refuses and names the current version

### Requirement: An anchor is written only for a referenced unit
The store SHALL write an anchor into a unit only when the unit has no anchor and becomes either end of its first stored relationship, or gains its first embed or generated-page pin, in the write that adds it. The store SHALL NOT write an anchor into any other unit.

#### Scenario: A first embed pins the unit
- **WHEN** a write adds the first embed of a unit that has no anchor
- **THEN** the same transaction writes an anchor at the end of that unit in its home page

#### Scenario: An unreferenced unit keeps its bytes
- **WHEN** the upgrade pass assigns records to a page whose units nothing references
- **THEN** the page bytes do not change

### Requirement: A page embeds a unit of another page
A unit SHALL live in one home page. Another page SHALL show it with a block embed of its anchor and SHALL NOT hold a copy of its bytes. A page read SHALL be able to expand embeds in one batched read, up to 64 embeds and 256 KiB, and SHALL say when it stopped at the bound.

#### Scenario: An edit at home shows everywhere
- **WHEN** a person edits a unit in its home page and another page embeds it
- **THEN** the embedding page's bytes do not change
- **AND** an expanded read of the embedding page returns the edited text

#### Scenario: An embed line is a segment
- **WHEN** a person deletes an embed line from an embedding page
- **THEN** the import changes only the embedding page
- **AND** the unit stays live in its home page

### Requirement: A generated page renders a stored definition
A page SHALL be able to be generated from a stored definition whose selector names only registry values and page scopes. The store SHALL render it as headings and embeds, up to 500 members, and SHALL render it again when its member set changes. The importer SHALL hold a hand edit of it, and a governed page write to it SHALL refuse with `PAGE_IS_GENERATED`.

#### Scenario: A new member renders the page again
- **WHEN** an agent adds a unit that a generated page's selector matches
- **THEN** the page renders again with an embed of that unit

#### Scenario: A member's text change renders nothing
- **WHEN** a member unit's text changes
- **THEN** the generated page's bytes do not change

#### Scenario: A hand edit of a generated page is held
- **WHEN** a person edits a generated page's file
- **THEN** the edit is held
- **AND** keep-mine makes it an ordinary page with the person's bytes

### Requirement: A registry or parser change splits pages again
A commit that changes a registry source, and a release that changes the parser version, SHALL queue a split pass. The pass SHALL split the affected pages again in bounded batches under the vault mutation boundary by the light rule. Status SHALL report `splitting` with counts. A unit operation on a page that the pass has not reached SHALL refuse with `UNITS_BEHIND`.

#### Scenario: A new category turns bullets into units
- **WHEN** a registry change adds a category that existing bullets use
- **THEN** the split pass gives those bullets unit records
- **AND** every other unit keeps its unit id

### Requirement: Hand edits import after they settle
The importer SHALL import a changed file of a page that holds units only after the file's bytes have stayed the same for 2 seconds.

#### Scenario: Typing saves do not replace a unit
- **WHEN** an editor saves every 300 ms while a person types a sentence into a unit
- **THEN** the importer commits once, after the saves stop

### Requirement: Unit records stay in parity with the page bytes
The store SHALL keep four invariants: live placements lie in the body in order, without overlap, and hash to their records; a parse of the body at the page's split version gives the live placements; every derived unit reference maps to one live record; derived rows equal a cold derivation of the bodies. A failed check SHALL show in status and refuse unit operations on that page.

#### Scenario: The upgrade leaves derived rows unchanged
- **WHEN** the upgrade pass completes on a store
- **THEN** every derived row is unchanged apart from its unit id
- **AND** all four invariants hold

### Requirement: Unit records survive rollback
Activation SHALL record `vault-store-v2`, and a build without this change SHALL refuse to serve it. Rollback to stage 2 and to file mode SHALL export the unit records, the reference history, the lineage, the generated-page definitions and the open review items beside the vault. It SHALL refuse while review items are open unless they are exported. A later migration SHALL read an identity export whose span hashes match the bytes.

#### Scenario: A round trip keeps unit ids
- **WHEN** a vault rolls back to file mode and migrates again with its identity export
- **THEN** every unit whose bytes did not change keeps its unit id

#### Scenario: An older build refuses the store
- **WHEN** a build without this change starts on a store that records `vault-store-v2`
- **THEN** it refuses to serve and names the rollback

## MODIFIED Requirements

### Requirement: Semantic units are first-class store rows
In store mode, each semantic unit of a page SHALL be one derived row with its reference, page, ordinal, byte span, category, kind, text, provenance, verdict, lifecycle and unit id. The reference SHALL follow the existing identity rule and derive from the page bytes. For the same bytes, the rows SHALL reproduce file mode's lexical unit rows and graph unit nodes. A typed edge MAY start or end at a unit. A write that changes a unit SHALL change the page bytes and the unit in one transaction.

#### Scenario: A link reaches the exact unit
- **WHEN** a page links to another page's unit by the anchor that unit carries
- **THEN** the graph edge ends at that unit, not only at its page

#### Scenario: Refuting one claim changes one unit
- **WHEN** a governed write marks one claim on a page as refuted
- **THEN** the page bytes and that unit's verdict commit in one transaction
- **AND** the other units on the page keep their identities and verdicts

#### Scenario: A unit without an anchor keeps today's reference
- **WHEN** a governed write adds a unit that carries no anchor and that nothing references
- **THEN** the unit's reference is its fingerprint-bound reference
- **AND** the committed page bytes are exactly the bytes the write supplied
