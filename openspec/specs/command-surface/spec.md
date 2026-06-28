# command-surface Specification

## Purpose
TBD - created by archiving change unify-command-surface. Update Purpose after archive.
## Requirements
### Requirement: Single Command Registry Generates Every Surface

The system SHALL define a single declarative command registry (`commands.py`) that enumerates each
operation with its name, leaf function, description, parameter specs, and exposed surfaces, and the
MCP tools, the REST facade, the OpenAPI document, and the CLI SHALL all be generated from it. No
surface may maintain its own separate list of operations.

#### Scenario: One entry exposes an op everywhere

- **WHEN** a new operation is added as a single registry entry with surfaces `{mcp, rest, cli}`
- **THEN** its MCP tool, its `/api/<name>` REST route, its OpenAPI path, and its `kb <name>` CLI
  subcommand all exist with no further per-surface edits
- **AND** removing the entry removes it from all surfaces

### Requirement: MCP Tools Are Generated With Byte-Identical Fidelity

The MCP tools SHALL be generated from the registry via a `bind_vault` helper that presents each leaf's
signature (minus the injected `vault_root`) and the registry description to the MCP framework. A
snapshot test SHALL assert each generated tool's input-schema and description are byte-identical to a
committed baseline of the current tools, so the migration cannot change what Claude sees. Any tool
that cannot match SHALL remain hand-registered and be named in an explicit exceptions list.

#### Scenario: Generated tool matches the baseline exactly

- **WHEN** the schema-fidelity snapshot test runs over a registry-generated tool
- **THEN** its input-schema and description equal the committed baseline byte-for-byte
- **AND** the test fails if any generated tool's schema or description differs

#### Scenario: Non-matching tool is an explicit exception

- **WHEN** a tool (e.g. the wide `note`) cannot be generated with a matching schema
- **THEN** it stays hand-registered and appears in the exceptions list
- **AND** the snapshot test asserts the exceptions list is explicit, with no silently-skipped tool

### Requirement: REST Facade And OpenAPI Derive From The Registry

The REST facade SHALL register an `/api/<name>` POST route for every registry op exposed on `rest`,
via one generic handler (auth gate → JSON body → coerced leaf kwargs → threadpool call → envelope),
and the OpenAPI document SHALL be generated from the registry's parameter specs, replacing the
hand-maintained tool list. The previously hand-wired routes SHALL be preserved.

#### Scenario: Existing routes preserved, missing ones added

- **WHEN** the registry-driven facade is built
- **THEN** the previously hand-wired routes (find, get, note, add, edit, audit, reconcile,
  list_directory, suggest_links) still exist at the same paths calling the same leaves
- **AND** operations that previously lacked a route (e.g. replace, link, provenance_report) now have
  one because they are in the registry with `rest`

#### Scenario: OpenAPI documents real parameters

- **WHEN** `GET /api/openapi.json` is requested
- **THEN** each path's request schema lists the operation's actual parameters from the registry
- **AND** no separate hand-maintained operation list exists to drift

### Requirement: A First-Class CLI Over All Operations

The system SHALL ship console-script entry points `kb` and `kb-mcp` that expose every registry op on
the `cli` surface (reads AND writes) as a verb-first subcommand, with positional args for params
marked positional and `--flags` for the rest. It SHALL support a global `--json` structured envelope,
emit structured error codes with remediation, and return exit code 0 on success, 1 on operation
error, and 2 on usage/argument error. The existing admin subcommands SHALL keep working unchanged.

#### Scenario: Query the KB from the terminal

- **WHEN** `kb find "carbonation rig" --json` is run
- **THEN** the search runs against the local vault and prints a single-line envelope
  `{success: true, data: [...]}`, exit code 0

#### Scenario: Write from the CLI and usage errors

- **WHEN** `kb note --note-type insight --title "..." --content "..."` is run against a temp vault
- **THEN** the note is created and reported
- **AND** running any op with a missing required argument prints `Error [..]: …` and exits 2

### Requirement: Shared Result And Error Envelope

The CLI (`--json` mode) and the REST facade SHALL use one shared envelope shape:
`{success, data, error: {code, message, remediation}}`. A success carries `data` with `success:true`
and no `error`; a failure carries `success:false` and an `error` block with a stable, machine-readable
`code`. The REST binary-blob guard for text fields SHALL be preserved.

#### Scenario: Same logical failure, same code on both surfaces

- **WHEN** an operation fails validation in REST and in CLI `--json` mode
- **THEN** both return `{success: false, error: {code, message, remediation}}` with the same `code`

#### Scenario: Binary-blob guard preserved

- **WHEN** a REST request passes an oversized base64 blob in a text field
- **THEN** it is rejected with the existing `BINARY_BLOB_REJECTED`-class error, as before

