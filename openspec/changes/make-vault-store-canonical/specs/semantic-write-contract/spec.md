## MODIFIED Requirements

### Requirement: One Claim Inside A Note Is Superseded In Place

`supersede_observation` SHALL append one rich replacement with `as_of` when supplied and add
`supersedes` to the target plus any supplied links. It SHALL record `why` in revision history and
preserve other units' text and metadata. `expected_hash` is optional. File mode SHALL author the
links. Store mode SHALL commit them as relationship rows with the new unit. Readers SHALL return
each authored or stored supersession once.

#### Scenario: A compact claim on an entity page is superseded
- **WHEN** `edit_note(action="supersede_observation")` names a compact unit on an entity page, with
  replacement text, `as_of`, `why` and a link `owns` to another page
- **THEN** the page gains one rich unit with `as_of`
- **AND** the unit gains `supersedes` to the target's anchor and `owns` to the other page, in the
  active mode's canonical relationship form
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

#### Scenario: A stored supersession is read with an authored supersession
- **WHEN** a store-mode page has an authored supersession and a governed write adds another
- **THEN** current readers return each supersession once
- **AND** the governed successor's relationship and history commit with its page bytes
- **AND** the authored relationship remains in the page

### Requirement: One Claim Is Assessed Or Linked In Place

`assess_observation` and `link_observation` SHALL resolve one unit as for supersede. An assessment
may set a registry verdict and `check_by`. Both actions add links and preserve the unit's text and
existing relationships. File mode SHALL author the links in the unit. Store mode SHALL commit them
as relationship rows. The write SHALL record `why` in relationship history for stored links and
revision history for page changes.

#### Scenario: A claim is refuted with evidence
- **WHEN** `edit_note(action="assess_observation")` names a rich claim with `verdict="refuted"`, an
  `evidenced_by` link and `why`
- **THEN** the target carries verdict `refuted` and the added relation, and its text is unchanged
- **AND** history records `why`

#### Scenario: A claim is linked without losing its relations
- **WHEN** `edit_note(action="link_observation")` adds a link to a rich unit that already has one
- **THEN** the unit carries both relations, and its text is unchanged

#### Scenario: A compact target is promoted only with a kind
- **WHEN** assess or link names a compact unit and passes `kind`
- **THEN** the unit becomes a rich block of that kind with the same id and text, appended at the
  end of the body, and every other unit parses exactly as before
- **AND** without `kind` the operation refuses with `COMPACT_METADATA_REQUIRES_RICH_KIND` and lists
  the admitted kinds

#### Scenario: An unknown verdict refuses
- **WHEN** `verdict` is not in the admitted verdict set
- **THEN** the operation refuses, lists the admitted verdicts, and the page is unchanged
