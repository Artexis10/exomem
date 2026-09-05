# skill-loading Specification

## Purpose
TBD - created by archiving change progressive-skill-loading. Update Purpose after archive.

## Requirements

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

### Requirement: Skill-aware live-state loading

The core and standalone Exomem skills SHALL use session bootstrap with their metadata `skill_contract` when live policy or capability state is missing and their local operating rules have been loaded. They SHALL reuse that state until policy, connection, adapter, or returned vault configuration/registry state changes. If the exposed bootstrap schema lacks `skill_contract`, they SHALL request compact directly. A server that rejects the session profile SHALL receive one compact fallback. Generic clients and clients missing a required local procedure SHALL continue to request the portable compact contract.

#### Scenario: Older server
- **WHEN** a skill-aware client requests session bootstrap and the server rejects the unsupported profile
- **THEN** the skill directs one compact fallback and retains the returned live policy
- **AND** it does not assume default permissions or loop on the unsupported profile

#### Scenario: Existing discovery schema lacks the new argument
- **WHEN** a client has an exposed bootstrap schema without `skill_contract`
- **THEN** it requests compact directly
- **AND** it does not send an unsupported argument

#### Scenario: Missing procedure
- **WHEN** a client cannot load the required local procedure
- **THEN** it obtains the compact portable operating contract
- **AND** a session-only state response is insufficient authority to improvise the operation
