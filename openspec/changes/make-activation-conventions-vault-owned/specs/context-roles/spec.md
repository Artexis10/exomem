## ADDED Requirements

### Requirement: Role cues are the single cue vocabulary
The cue patterns declared on roles in the effective context-role registry SHALL be the
only turn-cue vocabulary the context compiler evaluates. Each role SHALL carry a boolean
`cue_evidence` field, default `false`, which a vault override MAY set on any role. For a
turn, the semantic-unit categories eligible for the `category_match` evidence kind SHALL
be the union of the `categories` of every role whose cue matches the turn and whose
`cue_evidence` is true. `category_match` SHALL remain a qualifier that never establishes
contact between a turn and an anchor. The server SHALL hold no cue pattern or
cue-to-category mapping outside the registry.

#### Scenario: An owner's cue reaches anchor evidence
- **WHEN** a vault override adds the cue `ich plane` to `active_plans`, whose
  `cue_evidence` is true, and a turn containing `ich plane` reaches an anchor by
  `lexical_overlap` whose page carries an `action` unit
- **THEN** the anchor's evidence kinds include `category_match` and the selected roles
  include `active_plans` with source `turn_cue`

#### Scenario: A broad cue selects a role without counting as evidence
- **WHEN** a turn contains `where`, a cue of the shipped `location` role whose
  `cue_evidence` is false
- **THEN** `location` may be selected as a role, and no anchor earns `category_match`
  from that cue

#### Scenario: A cue alone never resolves an anchor
- **WHEN** a turn matches a cue with `cue_evidence` true and reaches no anchor by any
  contact kind
- **THEN** activation abstains
