## ADDED Requirements

### Requirement: Native Memory Boundary
Agent-facing documentation SHALL distinguish native assistant memory from Exomem
without presenting either as a replacement for the other.

#### Scenario: Boundary is visible to users and agents
- **WHEN** a user reads the built-in memory comparison or assistant guide
- **THEN** the documentation identifies native memory/custom instructions as the
  place for preferences, tone, identity facts, and routing reminders
- **AND** identifies Exomem as the place for durable governed knowledge with
  provenance, review, and supersession

#### Scenario: Non-memory uses are excluded
- **WHEN** a user needs to store secrets, transient scratch, reminders, or task
  state
- **THEN** the documentation points away from both Exomem and native assistant
  memory where those systems are not appropriate

### Requirement: Intent To Action Guidance
Agent-facing documentation SHALL map common user intents to simple Exomem
actions before exposing internal page-type or folder terminology.

#### Scenario: Agent sees a remember request
- **WHEN** the user says "remember this" or equivalent phrasing
- **THEN** the guidance tells the agent to preserve raw material when needed and
  save durable conclusions as concise compiled notes

#### Scenario: Agent sees a source or proof request
- **WHEN** the user asks to preserve a source, receipt, document, case artifact,
  or proof-bearing record
- **THEN** the guidance distinguishes source capture from evidence preservation
  in user-facing language

#### Scenario: Agent sees a stale or superseded knowledge request
- **WHEN** the user asks whether old knowledge is still valid or says a new
  conclusion replaces an old one
- **THEN** the guidance routes the agent to review/audit behavior or
  supersession behavior instead of duplicating notes

### Requirement: First Run Guidance For Non CLI Users
Quickstart documentation SHALL include a path for users who are not comfortable
driving CLI setup unaided.

#### Scenario: User asks an assistant to help set up Exomem
- **WHEN** a non-CLI-comfortable user reads the quickstart
- **THEN** the quickstart gives them a concise prompt they can hand to an AI
  assistant
- **AND** states the expected verification steps after setup: doctor, bootstrap,
  one known lookup, and one safe test save

### Requirement: Concrete Examples
Agent-facing documentation SHALL include concrete examples for the memory
workflows Exomem wants agents to perform.

#### Scenario: Required examples are present
- **WHEN** the assistant guide and scaffold guidance are reviewed
- **THEN** examples cover remembering a conclusion, finding a previous
  conclusion, preserving a source or proof artifact, compiling evidence,
  reviewing stale knowledge, and superseding an old conclusion

### Requirement: Scaffold Remains Generic
The shipped scaffold guidance SHALL stay generic and free of private paths,
private names, and project-specific tenant tokens.

#### Scenario: Scaffold leak guard runs
- **WHEN** the scaffold no-leak tests scan `src/exomem/_scaffold`
- **THEN** they pass without private-token or machine-path findings
