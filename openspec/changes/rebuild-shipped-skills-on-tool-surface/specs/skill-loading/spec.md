## MODIFIED Requirements

### Requirement: Portable operating contract

The skill SHALL take live engagement and capabilities from the server, never from static
text. Each standalone authoring package SHALL ship the semantic authoring projection once,
as a reference file. A workflow SHALL load it, or `read_instructions(section="authoring")`,
before its first compiled-note write, and SHALL NOT copy it or the core rules.

#### Scenario: A harness has no select syntax
- **WHEN** Exomem tools are exposed through a different discovery mechanism
- **THEN** the agent uses that mechanism and the same intent-scoped route
- **AND** the skill does not require a Claude-specific discovery call

#### Scenario: Local procedure is unavailable
- **WHEN** the selected route requires a reference that the harness cannot read
- **THEN** the agent obtains the portable bootstrap contract
- **AND** it does not improvise a mutation whose required rules are still unavailable

#### Scenario: Standalone capture skill
- **WHEN** a capture workflow is loaded without the core skill
- **THEN** its package still carries the canonical semantic grammar and minimum-unit contract as a reference file
- **AND** the workflow reads that file before it writes a compiled note

## ADDED Requirements

### Requirement: Shipped calls match the published surface

Every call that the shipped skill text spells SHALL be one that the published tool
surface accepts: a published tool with fields that tool takes, or a door with an
operation that door serves and arguments that operation takes. The skill text SHALL
NOT name an internal command, an internal kind or a retired tool. A check SHALL parse
the shipped text and fail the build on any call that the surface refuses.

#### Scenario: A tool field is renamed
- **WHEN** a tool stops taking a field that a shipped skill spells
- **THEN** the check fails and names the file, the line and the field

#### Scenario: A door operation is spelled as an internal kind
- **WHEN** a shipped skill names `resolve-relation` instead of `connections.resolve_relation`
- **THEN** the check fails and names the operation spelling to use

### Requirement: Owner skills build on shipped workflows

An owner skill SHALL hold the owner's method: what to capture and where it lands. A
shipped workflow SHALL hold how Evermatter writes: calls, order and governance. When recall
lists an owner skill for the task, the workflow SHALL direct the agent to follow it, and
the workflow's rules still apply. The core skill SHALL say how to save a repeated method
as an owner skill.

#### Scenario: An owner skill covers an ingest
- **WHEN** the user shares a podcast episode and recall lists the owner's episode-notes skill
- **THEN** the agent reads that skill and follows its extraction method and destinations
- **AND** it saves through the ingest workflow's calls, with Sources immutable and the authoring contract applied

#### Scenario: The user repeats a method
- **WHEN** the user asks to keep a method they described for later use
- **THEN** the agent saves it as an owner skill with a description that says when it applies
- **AND** the skill names the shipped workflow it builds on instead of restating its calls
