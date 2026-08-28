## ADDED Requirements

### Requirement: Every compiled writer shares one source-closure leaf

`remember_memory`, `replace_memory`, `edit_memory`, and governed Tier-2 compiled-note creation SHALL call one shared source-closure validator at the semantic precommit boundary. MCP, REST, CLI, OpenAPI, bootstrap guidance, and schema-fidelity fixtures SHALL derive the same behaviour and remediation from the canonical command registry; no facade SHALL implement its own resolver or warning-only exception.

#### Scenario: Equivalent unresolved write has surface parity

- **WHEN** the same compiled-note mutation with an unresolved explicit source is invoked through MCP, REST, CLI JSON, and the governed Tier-2 route
- **THEN** every surface refuses with the same stable application data and no surface commits the note

#### Scenario: Future writer cannot omit closure classification

- **WHEN** a new registry command is classified as a compiled semantic writer
- **THEN** registry or contract validation fails closed unless it enters the shared source-closure precommit path

### Requirement: Source-closure refusal uses one stable application envelope

The public error registry SHALL define `UNRESOLVED_SOURCE_CITATION` with a non-empty bounded message, capture-first remediation, unresolved total, deterministic capped caller-supplied values, and truncation state. MCP SHALL return the deliberate refusal as normal tool content; REST and CLI JSON SHALL return the identical shared envelope; human CLI SHALL render the same code, message, and remediation with the canonical operation-error exit status.

#### Scenario: Deliberate source refusal is not an internal error

- **WHEN** source closure rejects a non-empty unresolved citation
- **THEN** each generated facade presents the stable application refusal and does not relabel it as an unexpected execution failure

#### Scenario: Refusal guidance teaches capture then retry

- **WHEN** capability or bootstrap guidance describes a writer that accepts `sources`
- **THEN** it states that external material must first be captured and the derived write retried with the governed source reference

### Requirement: Capture remains independent from derived compilation

Source and Evidence capture commands SHALL remain valid without a pending derived note and SHALL preserve existing raw-material and provenance contracts. A compiled writer SHALL NOT call a connector, fetch a remote locator, or silently invoke capture while validating source closure.

#### Scenario: Compiled write does not fetch an external ID

- **WHEN** a source entry resembles a connector URL or object identifier
- **THEN** the writer refuses locally without network access or a hidden capture side effect
