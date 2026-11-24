## ADDED Requirements

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
- **WHEN** the caller passes no `kind`
- **THEN** the replacement uses the target's kind when the target is rich, else the one admitted
  kind that carries the `replacement` trait (`correction` in the core pack)
- **AND** when no admitted kind or more than one carries that trait, the operation asks for `kind`
  and lists the admitted kinds

#### Scenario: An unanchored target gains only an anchor
- **WHEN** the target has no authored anchor
- **THEN** the write adds an anchor to the target and changes none of its other bytes

#### Scenario: A stale fingerprint refuses
- **WHEN** the target is named by a fingerprint that no longer matches
- **THEN** the operation refuses with `STALE_UNIT_REFERENCE` and the page is unchanged

#### Scenario: Recall treats the target as superseded
- **WHEN** a supersede write commits
- **THEN** recall marks the target unit `superseded` and the replacement `active`

### Requirement: Supersede Targets Resolve Exactly

A span target SHALL lie inside exactly one top-level list item. A non-unit item SHALL first become
a compact unit in place, with its text unchanged and the canonical category of its nearest `##`
heading. An unresolvable target SHALL refuse without writing, with
`SUPERSEDE_TARGET_NOT_FOUND`, `SUPERSEDE_TARGET_AMBIGUOUS` or
`SUPERSEDE_TARGET_ALREADY_SUPERSEDED`, and the remediation SHALL name the next step.

#### Scenario: A facet prose item is superseded by span
- **WHEN** the target is a span inside a facet list item that is not a unit
- **THEN** that item becomes a compact unit under its own heading, with its text unchanged and a new
  anchor
- **AND** the replacement supersedes that anchor
- **AND** no other list item changes

#### Scenario: An ambiguous span refuses
- **WHEN** the span occurs in two list items, or crosses two items
- **THEN** the operation refuses with `SUPERSEDE_TARGET_AMBIGUOUS`, names the match count and asks
  for a longer span or a unit id, and the page is unchanged

#### Scenario: A missing target refuses
- **WHEN** the unit id or span matches nothing on the page
- **THEN** the operation refuses with `SUPERSEDE_TARGET_NOT_FOUND`, says how to read unit ids, and
  the page is unchanged

#### Scenario: An already superseded target points to its successor
- **WHEN** a unit on the page already supersedes the target
- **THEN** the operation refuses with `SUPERSEDE_TARGET_ALREADY_SUPERSEDED` and names that
  successor's id as the unit to supersede instead

### Requirement: One Claim Is Assessed Or Linked In Place

`observe_memory` SHALL offer an operation that changes one target unit's verdict, `check_by` and
relations without changing its text, exposed as `edit_note` actions `assess_observation` (a
verdict from the registry's verdict set, plus optional evidence links) and `link_observation`
(typed links). Links SHALL be added to the target's existing relations. The target SHALL resolve
as for a supersede, and `why` SHALL be the revision-history reason.

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

### Requirement: Entity Pages Accept Unit Writes

`observe_memory` SHALL accept every unit operation, including `supersede` and the assess or link operation, on a page whose
note-type role is `entity`, with the drift guards of a compiled page. A compact `add` SHALL append one line to
`## Observations` on the server and SHALL NOT require `expected_hash`. Role `compiled` SHALL NOT
widen. `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE` SHALL still refuse other roles and any tier that
is not read-write.

#### Scenario: A compact observation appends to an entity page
- **WHEN** `add_observation` targets an entity page with a category and text only
- **THEN** the page gains one compact line at the end of `## Observations`
- **AND** every other line of the page is unchanged

#### Scenario: A rich observation with links appends to an entity page
- **WHEN** `add_observation` targets an entity page with `kind="correction"`, links and `as_of`
- **THEN** the write commits and the new unit carries the links and `as_of`

#### Scenario: A read-only entity page still refuses
- **WHEN** `add_observation` targets an entity page whose access tier is `readonly`
- **THEN** it refuses with `OBSERVE_TARGET_NOT_WRITABLE_COMPILED_PAGE` and names the tier

### Requirement: Rich-Kind Refusals Name The Governed Kinds

The core kinds pack SHALL include a `correction` kind, a unit that states the current view and
replaces an earlier unit. `COMPACT_RELATIONS_REQUIRE_RICH_KIND` and
`COMPACT_METADATA_REQUIRES_RICH_KIND` SHALL list the kind keys that the page's selected registry
admits, in their remediation. When the caller's `category` is itself an admitted kind key, the
remediation SHALL say to pass it as `kind`. The list SHALL come from the registry, so a vault kind
appears and no kind list lives in code.

#### Scenario: Links on a compact unit name the valid kinds
- **WHEN** `add_observation` sends links with no kind
- **THEN** it refuses with `COMPACT_RELATIONS_REQUIRE_RICH_KIND`, and the remediation lists the
  admitted kinds, `correction` among them

#### Scenario: as_of on a compact unit names the valid kinds
- **WHEN** `add_observation` sends `as_of` with no kind
- **THEN** it refuses with `COMPACT_METADATA_REQUIRES_RICH_KIND`, and the remediation lists the
  admitted kinds

#### Scenario: A kind passed as a category is named
- **WHEN** `add_observation` sends `category="correction"`, links and no kind
- **THEN** the remediation says to pass `kind="correction"`

#### Scenario: A vault kind is listed
- **WHEN** the vault overlay adds a kind
- **THEN** the remediation lists that kind too

### Requirement: Units Show Their Supersession

`read_page(part="units")` SHALL return, for each unit, its `as_of` when present, the targets of its
`supersedes` relations, and `superseded_by`: the ids of the same-page units whose relations
supersede it. `superseded_by` SHALL be derived from those relations at read time. The `currency`
dimension of `search_memory` SHALL group same-page unit supersession, with the replaced unit
marked as replaced by its successor.

#### Scenario: The units part shows both ends of a supersession
- **WHEN** a page holds unit B with `supersedes: [[#a]]` and unit A with anchor `a`
- **THEN** `read_page(part="units")` shows A with `superseded_by` holding B's id, and B with
  `supersedes` holding A's id

#### Scenario: Search finds the superseded claim
- **WHEN** `search_memory` runs with the filter `unit.dimension` equal to `currency` and a query
  that names the page's subject
- **THEN** the result holds the replaced unit, marked replaced by its successor
