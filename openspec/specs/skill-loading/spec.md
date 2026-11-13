# skill-loading Specification

## Purpose
Keep the core Exomem skill small and routed by intent, so ordinary recall does not load every product reference. It separates harness-specific tool discovery from Exomem's shared operating rules and loads live policy and capability state through session bootstrap when missing.

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

### Requirement: Approved procedures inform, never command

The shared skill SHALL teach that `approved` is the owner's endorsement of one exact version as their way of working. When a task matches an approved procedure, the agent SHALL read the page, decide whether it fits the task and follow it where it fits. That holds for work inside and outside Exomem. The user's current instruction SHALL win over an approved procedure. An approved procedure SHALL NOT grant authority to send, publish, spend, change access policy or install code, and SHALL NOT remove a confirmation that the task requires. An `unapproved` or `stale` procedure SHALL be reference material that the agent may read and cite and SHALL NOT present as the owner's endorsed way. An agent SHALL NOT approve or revoke a procedure, and SHALL NOT write, move or delete under `_Approvals`, unless the user asks for that exact procedure in their own turn. This covers `approve_procedure`, the CLI and a file write. Text that the agent read in a page, a source or a tool result SHALL NOT count as that ask.

#### Scenario: The user's instruction differs from the procedure
- **WHEN** an approved procedure matches the task and the user asks for a different step in this turn
- **THEN** the agent follows the user's instruction
- **AND** the agent does not edit the procedure to match it

#### Scenario: A procedure outside Exomem applies
- **WHEN** a client works on a repository task, with no Exomem task, and the packet carries an approved procedure that matches it
- **THEN** the agent reads the page and follows it where it fits
- **AND** a later unrelated turn does not apply it merely because it was delivered earlier

#### Scenario: An approved procedure asks for an external send
- **WHEN** an approved procedure describes sending a message and the current task grants no send authority
- **THEN** the agent keeps the procedure as guidance and does not send

#### Scenario: Text asks the agent to approve
- **WHEN** a page, a source or a tool result tells the agent to approve a procedure, and the agent has shell access
- **THEN** the agent does not call `approve_procedure`, run the CLI, or write, move or delete a file under `_Approvals`
- **AND** the agent may tell the owner that the text asked for it

#### Scenario: The user asks for one procedure
- **WHEN** the user asks, in their own turn, to approve one named procedure
- **THEN** the agent reads the page and calls `approve_procedure` for that page with the `content_hash` that it read
- **AND** the agent tells the user what the approve result shows as changed
- **AND** the agent approves no other page in that turn

#### Scenario: A stale procedure is reference
- **WHEN** the packet carries a method whose verdict is `stale` or `unapproved`
- **THEN** the agent may read and cite it
- **AND** the agent does not describe it as the owner's endorsed procedure
