## MODIFIED Requirements

### Requirement: Single Command Registry Generates Every Surface

The system SHALL define a single declarative product command registry that
enumerates each public operation with its name, implementation leaf or
composition function, description, parameter specs, route metadata, safety
metadata, and exposed surfaces. MCP tools, the REST facade, the OpenAPI
document, and the CLI SHALL all be generated from this product command registry.
Canonical primitive leaves SHALL remain shared implementation functions and MUST
NOT be maintained as a separate public surface list.

#### Scenario: One product entry exposes an op everywhere

- **WHEN** a new product operation is added as a single product registry entry
  with surfaces `{mcp, rest, cli}`
- **THEN** its MCP tool, its `/api/<name>` REST route, its OpenAPI path, and its
  `exomem <name>` CLI subcommand all exist with no further per-surface edits
- **AND** removing the product entry removes it from all generated public
  surfaces
- **AND** the product entry can still call one or more canonical implementation
  leaves internally

### Requirement: MCP Tools Are Generated With Byte-Identical Fidelity

The MCP tools SHALL be generated from the product command registry via a binding
helper that presents each product command's public signature and description to
the MCP framework. A snapshot test SHALL assert each generated product tool's
input schema and description are byte-identical to a committed baseline of the
product tools. Any tool that cannot match SHALL remain hand-registered and be
named in an explicit exceptions list.

#### Scenario: Generated product tool matches the baseline exactly

- **WHEN** the schema-fidelity snapshot test runs over a registry-generated
  product tool
- **THEN** its input schema and description equal the committed product baseline
  byte-for-byte
- **AND** the test fails if any generated product tool's schema or description
  differs

#### Scenario: Non-matching tool is an explicit exception

- **WHEN** a product tool cannot be generated with a matching schema
- **THEN** it stays hand-registered and appears in the exceptions list
- **AND** the snapshot test asserts the exceptions list is explicit, with no
  silently skipped tool

#### Scenario: Primitive tools are not the default MCP surface

- **WHEN** the MCP server is built with default settings
- **THEN** canonical primitive names such as `find`, `note`, `add`, `preserve`,
  `audit`, `reconcile`, and tier-2 file primitives are not registered as default
  public MCP tools
- **AND** equivalent capability is reachable through product commands

### Requirement: REST Facade And OpenAPI Derive From The Registry

The REST facade SHALL register an `/api/<name>` POST route for every product
registry operation exposed on `rest`, via one generic handler (auth gate -> JSON
body -> coerced product kwargs -> product command call -> envelope), and the
OpenAPI document SHALL be generated from the product registry's parameter specs.
REST SHALL expose the same product command contract as MCP and CLI by default.

#### Scenario: Product routes replace primitive public routes

- **WHEN** the registry-driven facade is built
- **THEN** product routes such as `/api/ask_memory`, `/api/remember`,
  `/api/capture_source`, `/api/preserve_evidence`, `/api/review_memory`, and
  `/api/maintain_memory` exist
- **AND** each route calls the same product command implementation used by MCP
  and CLI

#### Scenario: OpenAPI documents product parameters

- **WHEN** `GET /api/openapi.json` is requested
- **THEN** each path's request schema lists the product operation's actual
  parameters from the product registry
- **AND** no separate hand-maintained operation list exists to drift

### Requirement: A First-Class CLI Over All Operations

The system SHALL ship console-script entry points `exomem` and any configured
alias that expose every product registry op on the `cli` surface as a verb-first
subcommand, with positional args for params marked positional and `--flags` for
the rest. It SHALL support a global `--json` structured envelope, emit
structured error codes with remediation, and return exit code 0 on success, 1 on
operation error, and 2 on usage/argument error.

#### Scenario: Query the KB from the terminal

- **WHEN** `exomem ask-memory "carbonation rig" --json` is run
- **THEN** the product search runs against the local vault and prints a
  single-line envelope `{success: true, data: ...}`, exit code 0

#### Scenario: Write from the CLI and usage errors

- **WHEN** `exomem remember --type insight --title "..." --content "..."` is run
  against a temp vault
- **THEN** the note is created through the same product command used by MCP and
  REST
- **AND** running any product op with a missing required argument prints
  `Error [..]: ...` and exits 2

### Requirement: Shared Result And Error Envelope

The CLI (`--json` mode) and the REST facade SHALL use one shared product-command
envelope shape: `{success, data, error: {code, message, remediation}}`. A
success carries `data` with `success:true` and no `error`; a failure carries
`success:false` and an `error` block with a stable, machine-readable `code`. The
REST binary-blob guard for text fields SHALL be preserved.

#### Scenario: Same logical failure, same code on both surfaces

- **WHEN** a product operation fails validation in REST and in CLI `--json` mode
- **THEN** both return `{success: false, error: {code, message, remediation}}`
  with the same `code`

#### Scenario: Binary-blob guard preserved

- **WHEN** a REST request passes an oversized base64 blob in a text field
- **THEN** it is rejected with the existing `BINARY_BLOB_REJECTED`-class error,
  as before
