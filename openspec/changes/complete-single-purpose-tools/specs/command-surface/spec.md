## ADDED Requirements

### Requirement: Conditional operation effects remain explicit on MCP

Each converted MCP operation SHALL publish its own typed inputs and effect conditions. A read tool SHALL accept only calls classified as reads. Omitted defaults, explicit nulls and boolean values SHALL retain their grouped-command meanings.

#### Scenario: A maintenance preview cannot become a repair

- **WHEN** a caller sends a maintenance write selection through a read tool
- **THEN** the server refuses it before writer authority and names the write tool when one exists
- **AND** an accepted preview retains the same normalized arguments as the grouped call

#### Scenario: Omission differs from an explicit selection

- **WHEN** an operation treats an omitted flag differently from an explicit boolean or null
- **THEN** its schema and normalization preserve that distinction
- **AND** an integer does not satisfy a boolean discriminator

### Requirement: Public operation fields preserve leaf data

Typed operations SHALL preserve every accepted data field when a leaf field conflicts with the public discriminator. The public schema SHALL use an unambiguous field name. Both public and compatibility calls SHALL reach the same leaf arguments.

#### Scenario: A semantic kind is distinct from an operation kind

- **WHEN** a caller selects an observation operation and supplies its semantic kind
- **THEN** the server preserves both values under their respective leaf arguments
- **AND** conflicting public and compatibility field spellings refuse before execution

### Requirement: Shared operation fields preserve complete variant contracts

Generated operation schemas MAY share identical complete field definitions at their common operation-object scope. The effective schema SHALL preserve allowed fields, required fields, defaults, nullability, literal constraints and descriptions. Sharing SHALL distinguish JSON scalar types and leave different field definitions separate. Runtime validation, normalization and authorization SHALL remain unchanged.

#### Scenario: A common field remains unavailable to a sibling operation

- **WHEN** two operations share a field definition and another operation does not accept that field
- **THEN** the effective schema rejects that field for the sibling operation
- **AND** the accepted operations retain their original required and optional behavior

#### Scenario: A nested alternative has a different field contract

- **WHEN** a field has a different completed schema in a nested alternative at the same operation-object scope
- **THEN** the renderer keeps those definitions separate
- **AND** defaults, explicit nulls and boolean or numeric values retain their original meanings

### Requirement: Governance tools preserve authorization families and variant credentials

Public governance tools SHALL separate authorization families and coherent jobs. Existing operation and transport registries SHALL determine each variant's authorization family and credential rule. Public normalization SHALL preserve session identity, token checks, standing grants, purpose declarations and authorization-affecting receipts.

#### Scenario: A standing grant cannot use the session-grant tool

- **WHEN** a caller sends a standing grant through the session-grant tool
- **THEN** the server refuses it before writer authority
- **AND** the owner-authorized public tool retains the standing-grant operation

#### Scenario: Session lifecycle operations retain different credential presence rules

- **WHEN** a caller opens or inspects an authorization session through the lifecycle tool
- **THEN** opening forbids a credential and status requires the existing session credential
- **AND** the tool does not replace either rule with a tool-wide credential requirement

### Requirement: Shared public tools preserve their complete jobs

A public tool MAY combine related operations from existing execution commands. Each typed variant SHALL select exactly one execution command before authorization. The combination SHALL preserve response capabilities, guards, retry identity and error envelopes. Existing grouped calls SHALL remain callable.

#### Scenario: A graph view retains its coverage contract

- **WHEN** a shared read tool exposes the global graph view
- **THEN** it retains drawing limits, complete or partial coverage state, counts and the original graph response
- **AND** it does not substitute a context catalogue map

#### Scenario: File preservation retains its transport identity

- **WHEN** a client preserves evidence files
- **THEN** the dedicated file-preservation tool retains its operation identity, top-level file handles and transport metadata
- **AND** exact retries preserve the original command's mutation identity

#### Scenario: Shared reads retain dataset admission

- **WHEN** tier 2 is disabled and the page-reading tool remains listed
- **THEN** its public schema and invocation resolver exclude the dataset variant
- **AND** enabling tier 2 retains the dataset reader's permissions, provenance and query result contract

### Requirement: Converted capabilities remain available on their admitted surfaces

Every existing operation SHALL remain reachable through a listed public variant or a recorded surface exclusion. A surface SHALL omit tools with no admitted variants. MCP guidance SHALL name listed tools; REST and CLI SHALL retain grouped command contracts.

#### Scenario: A Cloud exclusion does not remove local capability

- **WHEN** a maintenance operation is unavailable remotely
- **THEN** Cloud discovery excludes that variant and explains the supported host route
- **AND** the existing local command remains available with its original arguments

### Requirement: Structured collection schemas explain exact query inputs

Records and Planning schemas SHALL identify accepted collection selectors and describe filter items with their column, operator and value fields. Operator choices and the omitted-operator default SHALL derive from the existing query contract. Schema clarification SHALL preserve runtime query behavior.

#### Scenario: An agent queries a collection it inspected

- **WHEN** an agent uses the selector returned by collection inspection and omits a filter operator
- **THEN** the query applies the existing default operator
- **AND** every surface describes the same accepted selector and filter structure
