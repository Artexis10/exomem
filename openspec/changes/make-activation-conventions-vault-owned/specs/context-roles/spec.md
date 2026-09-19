## ADDED Requirements

### Requirement: Role cues are the single cue vocabulary, with explicit evidence
The cue patterns declared on roles in the effective context-role registry SHALL be the
only turn-cue vocabulary the context compiler evaluates. A role MAY declare
`evidence_categories`, default none. A role's cue SHALL count as an evidence cue only
when it is at least three characters long and occurs in the turn as whole terms in
order; role selection SHALL keep its existing matching. For a turn, the categories
eligible for the `category_match` evidence kind SHALL be the union of
`evidence_categories` over the roles with an evidence cue in the turn. Every
`evidence_categories` entry SHALL be a category the semantic-language registry knows; an
unknown entry SHALL be dropped with a finding. `category_match` SHALL remain a qualifier
that never establishes contact. The server SHALL hold no cue pattern or cue-to-category
mapping outside the registry, and the shipped registry SHALL make eligible exactly the
categories the compiler made eligible before this requirement existed.

#### Scenario: An owner's cue reaches anchor evidence
- **WHEN** a vault override adds the cue `ich plane` to `active_plans`, whose
  `evidence_categories` include `action`, and a turn containing `ich plane` reaches an
  anchor by `lexical_overlap` whose page carries an `action` unit
- **THEN** the anchor's evidence kinds include `category_match` and the selected roles
  include `active_plans` with source `turn_cue`

#### Scenario: A short or embedded cue selects a role without counting as evidence
- **WHEN** a role's cue is `?`, or is `an` and the turn contains only the word `plan`
- **THEN** the role may be selected and no anchor earns `category_match` from that cue

#### Scenario: A role without evidence categories never qualifies an anchor
- **WHEN** a turn contains `where`, a cue of the shipped `location` role, which declares
  no `evidence_categories`
- **THEN** no anchor earns `category_match` from that cue

#### Scenario: A cue alone never resolves an anchor
- **WHEN** a turn has an evidence cue and reaches no anchor by any contact kind
- **THEN** activation abstains

#### Scenario: Shipped evidence equals the previous behaviour
- **WHEN** a vault has no override
- **THEN** for every turn, the eligible categories equal those the compiler computed
  before the cue table was removed, and no anchor in `partial` contact becomes
  `resolved` through a category that was not eligible before

### Requirement: The roles registry is bounded and has a governed write path
The roles loader SHALL refuse a file larger than 256 KiB before parsing it and SHALL
cap an override at 32 roles, 48 cues per role, 64 characters per cue and 8 evidence
categories per role, ignoring entries past a cap with a finding. `schema_memory` SHALL
accept the subject `context-roles` to validate a proposed override, diff it against the
effective registry, and save a reviewed proposal under an expected-hash guard with a
stated reason. No other tool SHALL write the registry.

#### Scenario: An agent adds a cue through the governed tool
- **WHEN** an agent saves a reviewed override through `schema_memory` with the current
  content hash
- **THEN** the override file holds the proposal, the next packet's `generation` reports
  the vault registry and its new hash, and the generic file tools remain refused for
  the schema folder on a hosted tier

#### Scenario: A stale hash refuses the save
- **WHEN** the registry changed after the agent read it
- **THEN** the save is refused and nothing is written
