# agent-bootstrap-contract

## MODIFIED Requirements

### Requirement: Bootstrap Presents Simple Front-Door Actions
The bootstrap contract SHALL present the primary user/agent actions as save,
adopt/import, ask, prove, review, update, and connect. For each action it SHALL
name the preferred tool or composition of tools, the internal typed operation(s)
that enforce governance, and any selected-pack routing guidance. It SHALL keep
advanced tools visible but secondary.

#### Scenario: Bootstrap exposes available and selected packs
- **WHEN** an agent reads the bootstrap response
- **THEN** it can list available built-in packs with beginner descriptions
- **AND** it can list selected packs and their agent instructions
- **AND** a missing selection falls back to a default personal-records pack

#### Scenario: Agent can route a proof request
- **WHEN** an agent reads the bootstrap response and the user asks "prove this"
  or "save this for my warranty case"
- **THEN** the agent can identify the evidence/proof workflow
- **AND** it can distinguish that workflow from ordinary source capture
- **AND** selected pack guidance can refine the route without exposing internal
  ontology to the user

#### Scenario: Agent can route an existing-vault request
- **WHEN** an agent reads the bootstrap response and the user asks to import or
  adopt an old vault
- **THEN** the agent can identify the scan-first adoption workflow
- **AND** it knows existing files are read-only by default