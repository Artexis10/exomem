## ADDED Requirements

### Requirement: A new generic edge touching an entity needs an attributable choice

A governed live write that adds a generic edge SHALL refuse before commit when an endpoint is an entity and a specific predicate fits both visible endpoints.
The refusal SHALL carry at most four active specific options, each with its label, description and parent, and a ready retry hash.
The agent SHALL commit in one retry by choosing a specific predicate or supplying `relation_disposition="generic"`, `relation_review_reason` and the hash.
A generic `create-entity` relation item MAY supply its reason directly.
The committed log SHALL retain the attributable edge choice; the reason SHALL NOT prove semantic correctness.

Added edges SHALL be compared by stable resolved target identity against the pre-write or predecessor state.
Drafts, pending pages, the declared bulk operation classes, frozen surfaces and inherited predecessor edges SHALL NOT trigger the barrier.
An unresolved or withheld target SHALL NOT trigger it, and both states SHALL produce the same observable refusal shape.
A predicate without fitting endpoint declarations SHALL NOT be offered.

#### Scenario: One reasoned retry keeps generic

- **WHEN** a live write adds a generic edge touching an entity with fitting specific options
- **THEN** the first call refuses before commit with the options and retry hash
- **AND** a retry with that hash and a recorded generic reason commits the edge once

#### Scenario: A draft stays writable

- **WHEN** a draft or imported pending page adds the same generic edge
- **THEN** the barrier does not refuse the write

#### Scenario: A carried edge is not newly authored

- **WHEN** a superseding page retains its predecessor's generic edge
- **THEN** the barrier does not treat it as an addition

### Requirement: Note-to-note generic edges offer non-blocking specific options

A note-to-note generic edge SHALL remain writable and count as connectivity rather than a typed edge.
The existing non-blocking typed-edge warning SHALL apply.
When fitting predicates exist, the post-commit `relation_advisory` SHALL offer at most four `specific_options` with in-place replacement routes.
A predicate without endpoint declarations SHALL NOT be offered.
Each route SHALL replace the generic bullet with one hash-guarded edit, leaving one edge rather than two.
Write-advisory fingerprints SHALL support dismissal.

#### Scenario: Accepting an option replaces the generic edge

- **WHEN** the agent accepts a post-commit specific option for a note-to-note generic edge
- **THEN** the page holds one specific bullet to that target and no generic bullet to it

#### Scenario: Honest generic stays available

- **WHEN** no predicate's declared endpoints fit
- **THEN** the response offers no specific predicate and does not demand a new relation

### Requirement: Generators leave relation choice to the agent

Queue acceptance SHALL require an explicit requested predicate when fitting specific options exist.
Compile proposals and question-pair suggestions SHALL carry options rather than default to the generic relation.

#### Scenario: A generated proposal does not choose generic implicitly

- **WHEN** a generator finds fitting specific options
- **THEN** its output carries those options and leaves the predicate choice to the agent
