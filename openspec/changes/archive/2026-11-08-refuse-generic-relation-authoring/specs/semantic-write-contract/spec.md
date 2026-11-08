## MODIFIED Requirements

### Requirement: Authored all-generic relations receive accurate non-blocking feedback

A write SHALL NOT add a generic relation; the relation-vocabulary requirement refuses it before commit.
When admitted authored explicit relations are present and all are registry-marked generic, because the page authored them earlier or an upgrade left them, semantic evaluation SHALL emit one bounded non-blocking advisory.
It SHALL ask the active agent to review specific registered meaning, and SHALL name a proposed relation, a plain link or no edge as the honest alternatives.
It SHALL describe only that authored pattern, even when a qualifying incoming specific relation satisfies disposition.
It SHALL suppress the duplicate typed-edge-absent warning for that pattern without changing incoming-specific qualification.
It SHALL NOT infer meaning, force a predicate, impose an output count or refuse a write that adds no generic relation.
Default compact mutation feedback SHALL retain the advisory and an actionable relation-resolution route.

#### Scenario: Incoming specificity does not conceal generic authorship
- **WHEN** a page authors only admitted registered generic relations and also has a qualifying incoming specific relation
- **THEN** disposition remains satisfied by that incoming relation and the authored-all-generic advisory is visible
- **AND** the advisory does not claim that no typed relation exists

#### Scenario: Specific authorship does not receive the all-generic advisory
- **WHEN** an admitted authored explicit relation is specific rather than registry-marked generic
- **THEN** the all-generic advisory is absent

#### Scenario: A newly added generic edge is refused
- **WHEN** an otherwise valid write adds a generic relation to a page
- **THEN** the write is refused with the relation menu and no advisory replaces the refusal

#### Scenario: A generic relation inside a semantic unit is refused
- **WHEN** an otherwise valid write adds a generic relation in a unit's `relations:` metadata, and the unit has no anchor
- **THEN** the write is refused like a generic Relations bullet, unless the page it replaces authored the same relation, target, anchor and unit kind

#### Scenario: Honest generic edges remain writable
- **WHEN** a page already holds only admitted generic relations that it authored earlier, and an otherwise valid edit adds none
- **THEN** the edit may commit with the advisory and no specificity refusal
