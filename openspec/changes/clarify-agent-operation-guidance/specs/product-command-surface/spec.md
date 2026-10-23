## ADDED Requirements

### Requirement: Public titles describe product actions

Every product command SHALL publish a plain, purpose-specific public title from the shared command metadata. The title SHALL describe the complete tool capability. A mixed tool SHALL NOT claim to be read-only. Titles SHALL NOT change callable names, execution identities, authorization, replay, or operation coverage.

#### Scenario: A host receives a product title
- **WHEN** MCP discovery publishes a product command
- **THEN** its annotations contain the registry-owned public title alongside its existing effect hints
- **AND** preferred and compatible calls retain the same execution identity and safeguards

#### Scenario: A mixed Records tool is displayed
- **WHEN** a host receives the Records tool definition
- **THEN** its public title describes reading and changing records
- **AND** the definition retains conservative write-capable annotations
- **AND** a query still follows the existing read-only invocation path

### Requirement: Tool guidance explains branch effects and required inputs

Product tool guidance SHALL distinguish the tool's job from neighboring tools and explain material read, write, and conditional effects. It SHALL identify required branch inputs and retained guards using the existing operation contracts. It SHALL preserve supported capabilities and actionable recovery while avoiding redundant procedural prose.

#### Scenario: A caller reads structured information
- **WHEN** a caller needs an observed event or planned item
- **THEN** guidance identifies the existing collection inspect/query route
- **AND** it explains that an ordinary recall miss does not establish absence of a structured item
- **AND** it permits multiple matching items for recall while requiring an exact item for a mutation

#### Scenario: A caller inspects or saves a schema
- **WHEN** a tool includes read-only inspection and guarded schema writes
- **THEN** its guidance distinguishes their required fields, effects, and current-state guards
- **AND** conditional saving retains its existing behavior

### Requirement: Client teaching stays scoped and consistent

Server instructions, bootstrap guidance, tools, skills, and hooks SHALL teach consistent Exomem workflows at their respective levels of detail. They SHALL reuse current policy and context, request only purpose-specific context, and preserve activation continuity, maps, and provenance. Generated clients SHALL derive from the owning instruction sources.

#### Scenario: A client already holds current context
- **WHEN** a client has current policy and an adequate working set
- **THEN** guidance teaches targeted reads for remaining evidence gaps
- **AND** it does not prescribe another initial activation or redundant bootstrap

#### Scenario: A client needs continuity or a map page
- **WHEN** a supported client resumes a task or receives map references
- **THEN** guidance preserves session, workspace, bounded recent excerpts, task refresh, view references, and pagination
- **AND** a selected page or view remains available through the existing read route

#### Scenario: A host controls permission presentation
- **WHEN** Exomem describes a tool's effects
- **THEN** its guidance distinguishes static discovery hints from invocation authorization
- **AND** it makes no guarantee about prompts or activity that the host has not demonstrated
