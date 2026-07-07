# command-surface

## MODIFIED Requirements

### Requirement: Single Command Registry Generates Every Surface

The system SHALL define a single declarative command registry (`commands.py`)
that enumerates each operation with its name, leaf function, description,
parameter specs, exposed surfaces, and product-surface metadata. Product-surface
metadata SHALL identify whether an operation is `primary` or `advanced`, which
simple action(s) it supports (`save`, `adopt`, `ask`, `prove`, `review`,
`update`, `connect`), and whether it is safe for first-run/onboarding use. The
MCP tools, the REST facade, the OpenAPI document, the CLI, and agent bootstrap
guidance SHALL all derive from this registry metadata. No surface may maintain
its own separate list of operations.

#### Scenario: One entry exposes an op everywhere

- **WHEN** a new operation is added as a single registry entry with surfaces
  `{mcp, rest, cli}`
- **THEN** its MCP tool, its `/api/<name>` REST route, its OpenAPI path, and its
  `kb <name>` CLI subcommand all exist with no further per-surface edits
- **AND** removing the entry removes it from all surfaces

#### Scenario: Primary tools are discoverable without hiding advanced tools

- **WHEN** an agent reads the bootstrap contract or generated tool metadata
- **THEN** it can identify the primary front-door operations for save, adopt,
  ask, prove, review, update, and connect
- **AND** advanced typed/file operations remain available for agents that need
  precise control

### Requirement: Simple Front-Door Actions Route To Typed Operations

The command surface SHALL provide a simple front-door vocabulary for agents.
The first implementation MAY expose these as registry aliases, metadata, or
thin orchestration leaves, but the behavior SHALL be backed by the existing
typed operations rather than duplicating write logic. The front-door vocabulary
SHALL include save, adopt/import, ask, prove, review, update, and connect.

#### Scenario: Save routes without duplicate write logic
- **WHEN** the simple save action creates raw input, compiled knowledge, an
  entity, or proof
- **THEN** it delegates to the existing typed leaf (`add`, `note`, `link`, or
  `preserve`/evidence workflow) and preserves the same validation, logging,
  frontmatter, and write-scope rules

#### Scenario: Review fronts existing queues
- **WHEN** the simple review action is invoked
- **THEN** it can surface attention/audit/unprocessed-source findings through a
  product-level response
- **AND** the underlying audit/attention operations remain independently
  callable

### Requirement: Tool Descriptions Teach Intent

Generated MCP tool descriptions SHALL state when an agent should use the tool,
what kind of durable memory it creates or retrieves, and how it preserves
sources/provenance. Primary tool descriptions SHALL use simple product language.
Advanced tool descriptions MAY include internal page-type details.

#### Scenario: Agent sees proof intent
- **WHEN** the MCP schema/tool-description snapshot is inspected
- **THEN** the proof/evidence path tells the agent to use it for cases, claims,
  disputes, warranties, records, or other proof-bearing contexts
- **AND** it does not describe Evidence as the default destination for all raw
  input
