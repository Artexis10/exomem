## ADDED Requirements

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
