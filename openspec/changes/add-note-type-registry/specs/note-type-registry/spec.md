## Purpose

Let a vault define its note types through governed vocabulary. Code then selects pages by closed note-type roles and attributes instead of fixed type lists.

## ADDED Requirements

### Requirement: Note types use the governed registry contract

The note-type registry SHALL hold each type and its attributes in a shipped pack and the `_Schema/note-types.yaml` overlay.
It SHALL expose inspect, propose, save, history and restore through `schema_memory(subject="note-types")`.
Its mutation authority, meaning continuity and history SHALL follow the shared vocabulary contract.
Registry operations SHALL NOT change page bytes.

#### Scenario: A vault-defined compiled type takes effect and can be restored away
- **WHEN** an owner saves a type with note-type role `compiled` and its own folder, and a page of that type exists in that folder
- **THEN** `find` boosts the page, the write gate requires a semantic unit on it, and activation treats it as an eligible compiled page
- **AND** after a restore of the previous registry version, the page keeps its bytes and surfaces as unregistered note-type debt

### Requirement: Note-type roles and attributes are closed

An entry MAY declare note-type role `compiled`, `entity`, `source` or `evidence`; an entry without a role SHALL match no role predicate.
`time_bounded` SHALL be a boolean, and `sources` SHALL be `required` or `optional`.
A compiled type SHALL declare one `Notes/<Name>` folder that no other compiled type holds, and that the write gate's compiled router does not exempt.
A save SHALL refuse any other role or attribute value, and SHALL refuse to change `role`, `folder` or `time_bounded` on an existing entry.

#### Scenario: An unknown role is refused
- **WHEN** a save gives an entry a note-type role outside the closed set
- **THEN** the save is refused and the overlay keeps its bytes

#### Scenario: A role cannot change in place
- **WHEN** a save changes the role, folder or `time_bounded` value of an existing entry
- **THEN** the save is refused and the entry keeps its meaning

#### Scenario: Two compiled types cannot share a folder
- **WHEN** a save gives a new compiled type the folder of another compiled type
- **THEN** the save is refused

#### Scenario: A folder the router exempts is refused
- **WHEN** a save gives a compiled type a folder whose name the write gate exempts from compiled routing, such as `Notes/Data`
- **THEN** the save is refused with `NOTE_TYPE_FOLDER_RESERVED`

### Requirement: Shipped note types keep their meanings

The shipped pack SHALL define every note type that the product writes or reads.
An overlay SHALL NOT shadow, remove, redirect, deprecate or change a shipped type, and an extension key SHALL NOT collide with a shipped key.
Classification of a shipped type SHALL depend only on the pack, including when the overlay is absent, denied or invalid.

#### Scenario: Shipped types resolve the same under any overlay
- **WHEN** an admitted page of a shipped type is classified with an absent overlay, a denied overlay and an invalid overlay
- **THEN** its role and attributes are the same each time
- **AND** no private hash, key or debt appears

#### Scenario: A shipped type cannot be redefined
- **WHEN** a save or a hand edit of the overlay gives `insight` another role or folder
- **THEN** the save is refused and the hand edit surfaces as an invalid-overlay finding
- **AND** `insight` keeps its shipped role and folder

#### Scenario: Product types are never debt
- **WHEN** an audit runs over pages of every type that the product writes or reads, with no overlay
- **THEN** no page surfaces as unregistered note-type debt

### Requirement: Consumers select note types through closed predicates

Code SHALL select note types through predicates over note-type role and declared attributes, never through a type key.
Each predicate SHALL keep its consumer's purpose, so that the shipped pack selects exactly the types that each consumer selected before the registry existed.

#### Scenario: An unchanged vault keeps its results
- **WHEN** an unchanged vault whose type values spell shipped keys exactly is served before and after the registry ships
- **THEN** find ranking, claim scope, contradiction candidates, activation eligibility, relation debt, stale review, missing sources, source closure and minimum-unit applicability select the same pages

#### Scenario: Entity pages rank as compiled but carry no unit obligation
- **WHEN** an active `entity` page has no semantic unit
- **THEN** `find` applies the compiled boost to it
- **AND** the minimum-unit predicate is false for it

#### Scenario: Time-bounded compiled types stay out of stale review
- **WHEN** a compiled type declares `time_bounded: true`
- **THEN** stale review never selects its pages, as it never selects `experiment` pages

#### Scenario: Required sources follow the attribute
- **WHEN** a compiled type declares `sources: required` and its active page cites no sources
- **THEN** the missing-sources check reports that page

### Requirement: Unregistered note types are visible debt

After admitted resolution, a page whose `type` value no entry defines SHALL match no role predicate and SHALL surface as unregistered note-type debt.
A page without a `type` value SHALL NOT be debt.
Debt reporting SHALL NOT change page bytes.

#### Scenario: An unregistered type is reported, not rewritten
- **WHEN** an admitted page uses a `type` value that no entry defines
- **THEN** the audit reports it as unregistered note-type debt
- **AND** the page keeps its bytes and matches no role predicate

#### Scenario: An untyped page is not debt
- **WHEN** an admitted Markdown page has no `type` value
- **THEN** no unregistered note-type debt names it

### Requirement: Private note-type definitions require admission

The system SHALL admit the overlay before it reads extension entries, attributes or identity.
A caller who cannot admit it, and a library call that no surface bound, SHALL classify every page against the shipped pack only, for reads and writes alike.
For such a caller, a type or `Notes` folder outside the pack SHALL have no definition: it SHALL match no role predicate, rank neutral and never surface as unregistered debt.
The write gate SHALL judge such a page as a page of no registered type, never refuse it for its type or folder.
The owner's audit and activation review SHALL classify the page under the owner's registry, so they report an obligation that such a write skipped.
When an admitted caller's overlay is invalid, a write whose page needs a definition outside the pack SHALL refuse with `NOTE_TYPE_DEFINITION_UNAVAILABLE`, and its remediation SHALL name the findings of `schema_memory(subject="note-types", operation="inspect")`.

#### Scenario: A restricted write applies the shipped meaning
- **WHEN** a caller who cannot admit the overlay writes a page whose type or `Notes` folder is not in the shipped pack
- **THEN** the write gate judges the page against the shipped pack and does not refuse the write for its type or folder
- **AND** the page carries no semantic-unit obligation from a vault-defined type

#### Scenario: An invalid overlay refuses a write that needs a vault type
- **WHEN** the owner's overlay is invalid and the owner writes a page whose type or `Notes` folder is not in the shipped pack
- **THEN** the write gate refuses with `NOTE_TYPE_DEFINITION_UNAVAILABLE`
- **AND** the remediation names the findings of `schema_memory(subject="note-types", operation="inspect")`

#### Scenario: A link rewrite needs no vault definition under an invalid overlay
- **WHEN** the owner's overlay is invalid and a move rewrites links in a page whose type or `Notes` folder is not in the shipped pack
- **THEN** the gate judges that page against the shipped pack and does not refuse the move

#### Scenario: Ranking stays neutral without the definition
- **WHEN** a restricted caller's `find` returns a page whose type needs an unadmitted definition
- **THEN** that page ranks with a neutral type multiplier
- **AND** shipped types keep their ranking knobs

#### Scenario: Denied absent and present overlays are indistinguishable
- **WHEN** a restricted caller sees identical pages with no overlay, or with an overlay that defines their type privately
- **THEN** results and refusals are the same
- **AND** no private key, digest or debt appears

### Requirement: Ranking maps note-type roles to configured knobs

The ranking configuration SHALL own one map from note-type role to ranking knob.
Roles `compiled` and `entity` SHALL take the compiled boost, `source` SHALL take the source penalty, and `evidence` SHALL stay neutral.
An entry SHALL NOT carry its own multiplier.
An adopted ranking configuration SHALL reach every type with the mapped role.

#### Scenario: An adopted boost reaches a vault-defined type
- **WHEN** an owner adopts a ranking configuration with a new compiled boost
- **THEN** pages of a vault-defined compiled type take that boost, as `insight` pages do

#### Scenario: Evidence stays distinct from sources
- **WHEN** `find` ranks a `source` page and an `evidence` page with equal base scores
- **THEN** the `source` page takes the source penalty
- **AND** the `evidence` page stays neutral

#### Scenario: An entry cannot carry a multiplier
- **WHEN** a save gives an entry a ranking multiplier attribute
- **THEN** the save is refused

### Requirement: Note-type changes reach the next operation

A committed note-type save or restore SHALL change the next dependent operation without a restart, an index rebuild or a page edit.
Reusable derived results that depend on note-type meaning SHALL bind to the registry's effective digest.
The claim store is the one known exception until S4b binds it: its rows follow a save or restore only when their page is next written or the store is rebuilt.

#### Scenario: Save and restore reach a running service
- **WHEN** an owner saves a compiled type, then searches and writes in the same running service
- **THEN** `find` boosts its pages and the write gate checks them
- **AND** after a restore, the next operation stops doing so

#### Scenario: Parsed catalogue rows follow the registry
- **WHEN** the registry's effective digest changes
- **THEN** the lexical catalogue serves no semantic rows parsed under the previous digest

### Requirement: Published guidance teaches the note-type role rule

The current semantic authoring contract SHALL state that compiled intent is a canonical destination or a type with note-type role `compiled`.
It SHALL list the shipped compiled types and folders from the pack.
The workflow skills SHALL say that a vault may register more and name `schema_memory(subject="note-types", operation="inspect")` as the source of the live set.
The bootstrap vocabulary summary SHALL NOT list note-type keys, because the authoring contract and search guidance already list the shipped types. It SHALL still report the registry's findings, new keys and a refusal.
No published guidance SHALL claim that the shipped list is complete.
Released hosted profiles SHALL keep their frozen contracts and descriptors.

#### Scenario: Current guidance points to the live set
- **WHEN** a client reads the current authoring contract and a workflow skill
- **THEN** the contract gives the role rule and the shipped list, and the skill also names the registry route
- **AND** no sentence says that the compiled type list is exact

#### Scenario: Released profiles keep their bytes
- **WHEN** a v1 to v4 hosted profile serves bootstrap after a vault registers a compiled type
- **THEN** it serves its frozen authoring contract unchanged
- **AND** the vault-defined type does not appear in it

#### Scenario: Search guidance matches ranking
- **WHEN** bootstrap serves search guidance
- **THEN** its compiled types are the shipped types that `find` boosts

### Requirement: Promoted note types inherit their parent's role

A promotion SHALL name a registered parent.
The new type SHALL take its parent's note-type role, `time_bounded` and `sources`, and SHALL declare its own folder when its role is `compiled`.
A restore SHALL remove a promoted type without changing page bytes.

#### Scenario: A promoted compiled type gets compiled behaviour
- **WHEN** an owner promotes `meeting-note` under `insight` with folder `Notes/Meetings`, then creates one through `remember`
- **THEN** the page lands in `Notes/Meetings`, carries the compiled obligations of `insight`, and the notes index counts it
- **AND** after a restore, the page keeps its bytes and surfaces as unregistered note-type debt

### Requirement: Typed creation follows the registry

Typed creation SHALL place a page of a registered compiled type in its declared folder.
It SHALL apply the type's required fields, statuses, sections and typed fields from the registry.
The notes index SHALL count each compiled folder, including a new one.
Type-specific values SHALL reach `remember` and `replace_memory` through one generic `fields` parameter on the current tool surfaces, including hosted v5.
Released hosted profiles v1 to v4 SHALL keep their parameters.

#### Scenario: A new compiled folder enters the notes index
- **WHEN** typed creation writes the first page of a vault-defined compiled type
- **THEN** the notes index gains an entry and a count for its folder

#### Scenario: Released profiles keep their parameters
- **WHEN** a v1 to v4 hosted profile lists `remember` and `replace_memory`
- **THEN** their parameters and descriptors are unchanged
- **AND** only the current surfaces, including hosted v5, accept `fields`
