# Spec Delta

## MODIFIED Requirements

### Requirement: A Dated Observation With No Kind Keeps Its Dates On A Default Kind

When `add_observation`, or `edit_note(action="edit_observation")` on a compact unit, carries `as_of`
or `until` and no `kind`, the write SHALL commit a rich unit of the kind that carries the
`dated_default` trait, and the reply SHALL say so plainly. `verdict` or `check_by`, and an explicit
compact `kind`, SHALL keep the refusal. No refusal SHALL offer to omit `as_of` or `until`.

#### Scenario: Dates alone land in a dated block and the reply says so
- **WHEN** `add_observation` sends `as_of` and `until`, a category and a text, and no `kind`
- **THEN** the page gains one rich `record` unit with the category, the text and both dates within `## Observations`
- **AND** the server creates that section when the page has none
- **AND** the reply says "Saved with its dates."

#### Scenario: Dating a compact observation moves it to a dated block
- **WHEN** `edit_note(action="edit_observation")` sends `as_of` and no `kind` for a compact unit
- **THEN** the compact line leaves the list and a `record` block with the same anchor appears in `## Observations`
- **AND** the reply returns the unit id, whose version suffix differs from the id sent

#### Scenario: Only the shipped pack sets the trait
- **WHEN** a vault kind declares `dated_default`
- **THEN** a dated observation still lands on `record`
- **AND** the vault's schema diagnostics carry a warning that the trait was ignored

#### Scenario: Verdict and check_by keep the refusal and keep the date in view
- **WHEN** `add_observation` sends `verdict` or `check_by` with `as_of` and no `kind`
- **THEN** it refuses with `COMPACT_METADATA_REQUIRES_RICH_KIND` and the page is unchanged
- **AND** the remediation offers to pass a kind that takes the fields, or to omit `verdict` and `check_by` so the date lands in a dated block
- **AND** it does not offer to omit `as_of`

#### Scenario: The busy-write intake leaves a dated call to the page
- **WHEN** the service is busy and `add_observation` sends `as_of` and no `kind`
- **THEN** the intake queues the save and the replay writes it in a dated block
- **AND** the applied save reads "Saved with its date."
- **AND** a call that sends `verdict` or `check_by` is refused at once and never queued

#### Scenario: Later edits preserve observation boundaries
- **WHEN** a page has compact lines and a dated block in its observation section followed by a separate section
- **AND** an owner adds another compact observation and edits, assesses or links the dated block without changing its section boundaries
- **THEN** the compact lines appear above the dated block and remain independent units
- **AND** the dated block keeps its heading depth and the next section remains separate

#### Scenario: Nested content needs the shallower placement
- **WHEN** an add or edit can store the full dated body at the note's end but nesting would cut it short or hide a following section
- **THEN** the writer uses the end of the note to keep the full body in one dated block
- **AND** the reply names the placement and an existing anchor survives the move

#### Scenario: An uncertain destination uses the end of the note
- **WHEN** a date would land inside `## Observations` and a whole dated block fits at the note's end, but the note has multiple observation sections or an uncertain Markdown boundary
- **THEN** the writer keeps the full dated block at the end of the note and names its placement

#### Scenario: Neither placement can store the whole block
- **WHEN** neither placement shows the dated block's heading or lets the writer read back its full body
- **THEN** the operation refuses and the page is unchanged

#### Scenario: A rich kind owns the observation heading
- **WHEN** the vault resolves `Observations` as a rich kind and the full record fits at the note's end
- **THEN** the writer places the dated record at the end and names its placement
- **AND** the custom kind's existing units retain their bodies

#### Scenario: The existing record overlaps a separate section
- **WHEN** a replacement, move or removal of a nested record would touch a peer or parent heading that CommonMark reads as a separate section inside the record span
- **THEN** the operation refuses and the page is unchanged

#### Scenario: The existing record heading is hidden
- **WHEN** CommonMark hides the heading of a nested record that the writer would replace, move or remove
- **THEN** the operation refuses and the page is unchanged

### Requirement: Entity Pages Accept Unit Writes

`observe_memory` SHALL accept every unit operation, including `supersede` and the assess or link operation, on a page whose
note-type role is `entity`, with the drift guards of a compiled page. A compact `add` SHALL add one line to
`## Observations` on the server and SHALL NOT require `expected_hash`. Role `compiled` SHALL NOT
widen. `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE` SHALL still refuse other roles and any tier that
is not read-write.

#### Scenario: A compact observation appends to an entity page
- **WHEN** `add_observation` targets an entity page with a category and text only
- **THEN** the page gains one compact line at the end of the compact lines in `## Observations`, above canonical child headings
- **AND** every other line of the page is unchanged

#### Scenario: A rich observation with links appends to an entity page
- **WHEN** `add_observation` targets an entity page with `kind="correction"`, links and `as_of`
- **THEN** the write commits and the new unit carries the links and `as_of`

#### Scenario: A read-only entity page still refuses
- **WHEN** `add_observation` targets an entity page whose access tier is `readonly`
- **THEN** it refuses with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE` and names the tier

### Requirement: One Claim Inside A Note Is Superseded In Place

`observe_memory` SHALL offer a `supersede` operation, exposed as `edit_note` action
`supersede_observation`. In one governed write it SHALL append the replacement as a rich unit with
`supersedes` to the target's same-page fragment, plus any links and `as_of`, and record `why` in
revision history. It SHALL NOT change any other unit's text or metadata, and SHALL NOT require
`expected_hash`.

#### Scenario: A compact claim on an entity page is superseded
- **WHEN** `edit_note(action="supersede_observation")` names a compact unit on an entity page, with
  replacement text, `as_of`, `why` and a link `owns` to another page
- **THEN** the page gains one rich unit with relations `supersedes` to the target's anchor and
  `owns`, and with `as_of`
- **AND** the target unit's text is unchanged
- **AND** `read_page(part="history")` shows the entry with `why` as its reason and both units among
  the affected units

#### Scenario: The replacement kind follows the target
- **WHEN** the caller passes no `kind` and the target is rich
- **THEN** the replacement uses the target's kind

#### Scenario: A compact correction uses the shipped default
- **WHEN** the caller passes no `kind` for a compact target
- **THEN** the replacement uses the sole shipped kind with the `replacement` trait, even when an
  admitted custom kind has the trait
- **AND** an explicit request for the admitted custom kind still uses it

#### Scenario: An unanchored target gains only an anchor
- **WHEN** the target has no authored anchor
- **THEN** the write adds an anchor to the target and changes none of its other bytes

#### Scenario: A stale fingerprint refuses
- **WHEN** the target is named by a fingerprint that no longer matches
- **THEN** the operation refuses with `STALE_UNIT_REFERENCE` and the page is unchanged

#### Scenario: Recall treats the target as superseded
- **WHEN** a supersede write commits
- **THEN** recall marks the target unit `superseded` and the replacement `active`
