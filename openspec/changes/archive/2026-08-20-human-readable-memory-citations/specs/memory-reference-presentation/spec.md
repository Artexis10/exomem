## ADDED Requirements

### Requirement: Agent guidance keeps stable memory identity machine-facing

Agent guidance SHALL identify immutable `exomem_id` frontmatter and canonical
`exomem://memory/<uuid>` references as move-safe machine identity, consistent with the
stable-memory-references contract. It SHALL reserve canonical refs for tool arguments,
durable machine state, machine-readable automation, and cases where the identifier
itself is explicitly requested or inspected.

#### Scenario: A workflow continues after a note move

- **WHEN** an agent needs to retain a note identity across later tool calls or a note
  move
- **THEN** the guidance directs it to retain and use the canonical ref internally
- **AND** it does not substitute a mutable title or path for that machine identity

#### Scenario: A user explicitly asks for the canonical identifier

- **WHEN** a user asks to see a note's canonical reference or is debugging reference
  resolution
- **THEN** the agent may show the raw canonical ref because the identifier itself is the
  requested information

### Requirement: User-facing citations are title-first

Agent guidance SHALL direct agents to cite a memory note by its human-readable title in
normal user-facing prose and SHALL direct them not to expose the raw
`exomem://memory/<uuid>` reference by default. The guidance SHALL allow a current
vault-relative path or another short human-readable qualifier when needed for clarity.

#### Scenario: A search hit has a title, path, and canonical ref

- **WHEN** an agent cites the hit in a normal answer
- **THEN** the visible citation names the note title
- **AND** the answer omits the raw canonical ref

#### Scenario: Two relevant notes have the same title

- **WHEN** title-only citations would be ambiguous
- **THEN** the agent adds a human-readable path or short qualifier to distinguish them
- **AND** it does not use the UUID as the default disambiguator

#### Scenario: A legacy result has no usable title

- **WHEN** an agent must cite a result whose title is missing or unusable
- **THEN** the agent uses the current path or file name as the visible fallback
- **AND** it keeps the raw canonical ref out of normal prose

#### Scenario: A client renders Markdown source visibly

- **WHEN** a custom-scheme Markdown link would expose its raw UUID target in the client
- **THEN** the agent uses a plain title-first citation instead of embedding the canonical
  ref as the link target

### Requirement: Bootstrap and installed skill teach the same presentation rule

The generic MCP bootstrap and the installed skill scaffold SHALL both teach the
title-first citation rule, the optional path disambiguator, the machine-use role of the
canonical ref, and the explicit-request diagnostic exception. The bootstrap contract
version SHALL change when this guidance ships.

#### Scenario: A generic client initializes from bootstrap

- **WHEN** a client without the installed Exomem skill reads the bootstrap contract
- **THEN** it receives the title-first human-presentation rule and the machine-use ref
  rule

#### Scenario: An installed-skill client handles a memory result

- **WHEN** an agent follows the shipped scaffold guidance
- **THEN** it applies the same presentation and exception rules as a bootstrap client

#### Scenario: Contract guidance is regression-tested

- **WHEN** the bootstrap or scaffold guidance is changed
- **THEN** model-free tests detect removal of the title-first rule, path fallback,
  machine-use ref rule, or explicit-request exception
