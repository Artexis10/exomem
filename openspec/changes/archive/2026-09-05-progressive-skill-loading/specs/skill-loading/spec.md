## ADDED Requirements

### Requirement: Intent-scoped skill loading

The core skill SHALL expose common operating boundaries and explicit conditional
routes. An ordinary recall SHALL NOT instruct the agent to load every product
tool or every reference. Each required local reference SHALL ship inside the
core filesystem install, public plugin and upload archive.

#### Scenario: Ordinary recall with deferred tools
- **WHEN** a skill-aware agent is asked what the user previously concluded
- **THEN** it discovers recall tools as needed and reads selected hits
- **AND** media, mutation and maintenance procedures remain unloaded

#### Scenario: A write requires a conditional procedure
- **WHEN** an agent moves from recall to a governed write
- **THEN** the entrypoint directs it to the applicable writing/operation procedure and mutation result contract before the write
- **AND** it honors live engagement ceilings, provenance, and immutable-source boundaries

### Requirement: Portable operating contract

The skill SHALL distinguish harness-specific tool discovery from Exomem's shared
operating rules. It SHALL obtain live engagement and capability information when
missing and SHALL NOT infer permissions from a static skill or unavailable tool.
Every standalone authoring package SHALL retain the complete canonical semantic
authoring projection exactly once without requiring the core skill to be present.

#### Scenario: A harness has no select syntax
- **WHEN** Exomem tools are exposed through a different discovery mechanism
- **THEN** the agent uses that mechanism and the same intent-scoped route
- **AND** the skill does not require a Claude-specific discovery call

#### Scenario: Local procedure is unavailable
- **WHEN** the selected route requires a reference that the harness cannot read
- **THEN** the agent obtains the portable bootstrap contract
- **AND** it does not improvise a mutation whose required rules are still unavailable

#### Scenario: Standalone capture skill
- **WHEN** a capture workflow is installed without the core package
- **THEN** it still carries the canonical semantic grammar and minimum-unit contract
