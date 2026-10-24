## ADDED Requirements

### Requirement: Preferred Public Names Preserve Stable Command Identity

The product registry SHALL define preferred public names independently of stable execution identities.
The preferred names SHALL be `recall_context`, `search_memory`, `create_note`, `edit_note`, `supersede_note`, and `prepare_note` for the existing activation, search, creation, edit, supersession, and preparation commands respectively.
Both previous and preferred spellings SHALL resolve directly to the same command on MCP, REST, and CLI.
The system SHALL preserve parameters, defaults, response shapes, authorization, disclosure, writer leases, mutation replay, and operation boundaries.
Compatibility spellings SHALL have no automatic expiry.
Existing requirements that name an execution identity SHALL retain their operation contract under both spellings.
Discovery and documentation for that operation SHALL use its preferred name.

#### Scenario: Discovery recommends one spelling per operation

- **WHEN** a client reads MCP discovery, bootstrap recommendations, OpenAPI, or CLI help
- **THEN** it sees each preferred name instead of the previous name
- **AND** previous spellings remain callable without duplicate operation listings

#### Scenario: Retry crosses spellings without another mutation

- **WHEN** a mutation submitted through one spelling is retried with the same request identity and arguments through the other spelling
- **THEN** the existing mutation replay returns its original outcome
- **AND** no second write occurs

#### Scenario: Compatibility cannot restore a withheld capability

- **WHEN** surface, tier, or Cloud policy excludes a command
- **THEN** neither spelling bypasses that exclusion

#### Scenario: Existing clients keep their argument and result contracts

- **WHEN** a client calls either spelling with previously valid arguments
- **THEN** the command applies the same validation, guards, and result contract
- **AND** activation inputs, task refresh, map/profile references, continuation, and note summary/topics retain their behavior

### Requirement: Public Guidance Describes Jobs Without Extra Workflow Steps

Public tool guidance SHALL distinguish turn context, targeted evidence search, selected-result reads, note preparation, note creation, edits, and supersession.
It SHALL retain minimum valid-call teaching and safety remediation while using current held policy and context.
It SHALL recommend existing combined operations where they avoid an unnecessary call without adding an effect.

#### Scenario: Raw capture can return preparation guidance

- **WHEN** an authorized source capture requests the existing compilation guidance
- **THEN** guidance does not require another preparation call for the same returned proposal
- **AND** a compiled-note write remains a separate governed operation
