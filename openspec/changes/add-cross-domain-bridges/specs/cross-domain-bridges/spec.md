# cross-domain-bridges

## ADDED Requirements

### Requirement: Bridge notes release where their sources are withheld

A bridge SHALL be an ordinary compiled note carrying `bridge_of` (one or more
restricted source refs), `bridge_scope`, and `bridge_review` frontmatter plus an
approval grant. The release gate SHALL release a bridge note according to the
note's own scope even when its `bridge_of` sources are restricted, and SHALL strip
provenance pointing into a restricted scope at egress — frontmatter sources,
history, authored relation edges, and hit-level seed/relation/supersession
annotations — so the bridge never discloses a restricted title, path, or
neighbour.

#### Scenario: Abstraction crosses, provenance does not

- **WHEN** a bridge note whose sources are restricted is released to an audience
- **THEN** the bridge's own content is returned and no restricted source title,
  path, or relation edge appears in the response

### Requirement: Approval binds content

A bridge approval SHALL be a release grant carrying the note's content hash at
approval time. At release the gate SHALL compare the note's current content hash
to the approved hash and SHALL withhold with a stale-release outcome on mismatch,
requiring re-approval. An edited bridge SHALL NOT be released under a prior
approval.

#### Scenario: Edited bridge re-reviews

- **WHEN** a bridge note is edited after approval and then requested
- **THEN** the release is withheld as stale and re-approval is required

#### Scenario: Unedited bridge releases

- **WHEN** an approved bridge is unchanged since approval
- **THEN** it releases at its approved level

### Requirement: Constraint strings as micro-bridges

A scope MAY register a constraint string released at the constraint level (L2)
wherever a rule permits, without authoring a bridge note. The constraint string
SHALL convey a purpose-limited instruction and SHALL contain no raw facts from the
restricted source.

#### Scenario: Workload constraint informs planning

- **WHEN** a query in a planning context matches a scope with a registered
  constraint string
- **THEN** the constraint string is released and the raw source is not

### Requirement: Bridge re-review lifecycle

Due `bridge_review` dates SHALL surface in the existing review queue, and
restricting or deleting a `bridge_of` source SHALL flag its dependent bridges for
re-review without altering or deleting the owner's bridge note. An optional local
model MAY draft a bridge abstraction but SHALL NOT approve its own output;
approval SHALL be the owner's confirmed write plus the approval event.

#### Scenario: Source change flags dependent bridges

- **WHEN** a source referenced by a bridge is restricted or deleted
- **THEN** the dependent bridge is flagged for re-review and is not silently
  changed

#### Scenario: A model cannot self-approve

- **WHEN** a local model drafts a bridge abstraction
- **THEN** it is not released until the owner approves it with a confirmed write
