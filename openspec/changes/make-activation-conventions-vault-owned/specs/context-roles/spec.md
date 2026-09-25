## ADDED Requirements

### Requirement: Role cues are the single cue vocabulary, with explicit evidence
The cue patterns declared on roles in the effective context-role registry SHALL be the
only turn-cue vocabulary the context compiler evaluates. A role MAY declare
`evidence_cues` and `evidence_categories`, default none, which a vault override MAY
extend by union on any role. An evidence cue SHALL be a cue in the role's effective
`evidence_cues` that is at least three characters long, tokenises to at least one term, and occurs in the
turn as whole terms in order; a cue failing any of these SHALL never be evidence and
SHALL be reported as a finding. An `evidence_cues` entry SHALL also select its role.
Role selection SHALL keep its existing matching over every cue. For a turn, the categories
eligible for the `category_match` evidence kind SHALL be the union of
`evidence_categories` over the roles with an evidence cue in the turn. Every
`evidence_categories` entry SHALL be one the semantic-language registry resolves with a
status other than `unregistered`, stored as its resolved key; any other entry SHALL be
dropped with a finding. `category_match` SHALL remain a qualifier
that never establishes contact. The server SHALL hold no cue pattern or cue-to-category
mapping outside the registry. On the shipped registry no category SHALL become eligible
that was not eligible before this requirement existed, and no turn SHALL make a category
eligible that made none eligible before; the one intended difference is that a question
mark alone no longer makes `question` and `problem` eligible.

#### Scenario: An owner's cue reaches anchor evidence
- **WHEN** a vault override adds `ich plane` to the `evidence_cues` of `active_plans`,
  whose `evidence_categories` include `action`, and a turn containing `ich plane` reaches an
  anchor by `lexical_overlap` whose page carries an `action` unit
- **THEN** the anchor's evidence kinds include `category_match` and the selected roles
  include `active_plans` with source `turn_cue`

#### Scenario: A short or embedded cue selects a role without counting as evidence
- **WHEN** a role's cue is `?`, or is `an` and the turn contains only the word `plan`
- **THEN** the role may be selected and no anchor earns `category_match` from that cue

#### Scenario: A cue made only of punctuation is never evidence
- **WHEN** an override adds `???` to the `evidence_cues` of a role with
  `evidence_categories`
- **THEN** the cue is reported as a finding and makes no category eligible on any turn

#### Scenario: An owner promotes a shipped selection cue
- **WHEN** an override adds `budget`, already a shipped cue of `constraints`, to that
  role's `evidence_cues`, and a turn containing `budget` reaches an anchor by
  `lexical_overlap` whose page carries a `constraint` unit
- **THEN** the anchor's evidence kinds include `category_match`

#### Scenario: A shipped selection cue is not an evidence cue
- **WHEN** a turn contains `budget`, a shipped cue of a role whose `evidence_cues` do not
  list it, and no evidence cue
- **THEN** the role may be selected and no category becomes eligible

#### Scenario: A role without evidence categories never qualifies an anchor
- **WHEN** a turn contains `where`, a cue of the shipped `location` role, which declares
  no `evidence_categories`
- **THEN** no anchor earns `category_match` from that cue

#### Scenario: A cue alone never resolves an anchor
- **WHEN** a turn has an evidence cue and reaches no anchor by any contact kind
- **THEN** activation abstains

#### Scenario: Shipped evidence never widens the previous behaviour
- **WHEN** a vault has no override
- **THEN** for every turn the eligible categories are a subset of those the compiler
  computed before the cue table was removed, equal to them unless the turn's only
  matching pattern was a question mark, and no anchor becomes `resolved` on a turn for
  which no category was eligible before

### Requirement: The roles registry is bounded and has a governed write path
The roles loader SHALL refuse a file larger than 256 KiB before parsing it and SHALL
cap an override at 32 roles, 48 cues per role, 64 characters per cue and 8 evidence
categories per role, ignoring entries past a cap with a finding. `schema_memory` SHALL
accept the subject `context-roles` to validate a proposed override, diff it against the
effective registry, and save a reviewed proposal under an expected-hash guard with a
stated reason through the dedicated operation `save-roles`, which SHALL refuse a
proposal that has any finding; `infer` SHALL be refused for this subject. No other tool
SHALL write the registry.

#### Scenario: An agent adds a cue through the governed tool
- **WHEN** an agent saves a reviewed override through `schema_memory` with the current
  content hash
- **THEN** the override file holds the proposal, the next packet's `generation` reports
  the vault registry and its new hash, and the generic file tools remain refused for
  the schema folder on a hosted tier

#### Scenario: A stale hash refuses the save
- **WHEN** the registry changed after the agent read it
- **THEN** the save is refused and nothing is written
