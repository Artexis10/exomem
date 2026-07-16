# command-surface Specification

## Purpose
Keep every operation defined once instead of once per surface: a single
declarative command registry generates the MCP tools, the REST facade, the
OpenAPI document, and the CLI, so adding or removing an operation requires no
per-surface code and MCP tool schemas stay byte-identical to their committed
baseline. The CLI and REST facade share one result/error envelope so a given
failure carries the same machine-readable code on both surfaces.
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

The system SHALL ship console-script entry points `kb` and `exomem` that expose every registry op on
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

### Requirement: Bootstrap Is Exposed On Every Generated Surface
The system SHALL expose `bootstrap` through the single command registry on MCP,
REST, CLI, and OpenAPI. The tool SHALL be marked read-only and non-destructive in
MCP annotations.

#### Scenario: Bootstrap appears in generated surfaces
- **WHEN** the server is built
- **THEN** `bootstrap` appears in the MCP tool list
- **AND** `/api/bootstrap` appears in the REST facade and OpenAPI document
- **AND** the CLI exposes a `bootstrap` subcommand

#### Scenario: Bootstrap is accounted for by schema fidelity tests
- **WHEN** the MCP schema fidelity test runs
- **THEN** the live tool set includes `bootstrap`
- **AND** `bootstrap` is registry-generated rather than a hand-registered exception

### Requirement: Adoption Studio Is A Single Registered Product Command

The system SHALL expose Adoption Studio as one product command, `adoption_studio`, added as a single `_PRODUCT_SPEC` registry entry so its MCP tool, its `/api/adoption_studio` REST route, its OpenAPI path, and its CLI subcommand are all generated with no per-surface code. The command SHALL multiplex ten actions on a required `action` selector — `start`, `status`, `select`, `plan`, `apply`, `cancel`, `finish`, `work-item`, `propose`, `apply-proposal` — dispatching to the run engine and the proposal engine, and SHALL re-raise engine errors as the shared `{code}: {reason}` envelope. Its registry `routes` metadata SHALL reference the existing canonical `adopt` leaf so `validate_product_registry()` passes, and it SHALL be marked `first_run_safe` because its default read is safe and `start` is explicitly guarded.

#### Scenario: One entry exposes adoption on every surface

- **WHEN** the server is built from the registry
- **THEN** `adoption_studio` appears in the MCP tool list, `/api/adoption_studio` exists in the REST facade and OpenAPI document, and the CLI exposes an `adoption-studio` subcommand
- **AND** `validate_product_registry()` passes because the entry's route references the canonical `adopt` leaf

#### Scenario: An unknown action is rejected

- **WHEN** `adoption_studio` is invoked with an `action` outside the ten defined actions
- **THEN** it is refused with an `INVALID_MODE`-class error naming the valid actions and writes nothing

### Requirement: Adoption Read-Only Actions Are Classified For Lease And Hosted Admission

`invocation_is_read_only` SHALL classify `adoption_studio` invocations by resolving the `action` selector, returning read-only ONLY for `status` and `work-item` and treating every other action as a mutation. Read-only actions SHALL therefore run lease-free and without an idempotency key, while mutating actions SHALL route through `writer_lease.invoke_command` with implicit MCP retry replay. This one classification SHALL serve as both the local lease decision and the hosted read/write admission decision, requiring no bespoke hosted routing for the command.

#### Scenario: Only status and work-item are read-only

- **WHEN** `invocation_is_read_only` is evaluated for `adoption_studio` with each action, including the omitted-versus-explicit `action` selector
- **THEN** it returns true only for `status` and `work-item`
- **AND** all other actions are treated as mutations and acquire the writer lease

### Requirement: Existing Review Verbs Dispatch Adoption Refs

`review_memory`, `triage_memory`, and `review_item_context` SHALL each dispatch adoption-namespaced work rather than growing parallel review machinery: `review_memory(mode="adoption")` SHALL return the per-run grouped adoption proposal queue, `triage_memory` SHALL route an `exomem://review/adoption/<id>` ref to the adoption triage path before the relation dispatch, and `review_item_context` SHALL route an adoption ref to the adoption context assembler before the default review-context path. These docstring and behavior changes SHALL be reflected as an intentional, explicitly-noted regeneration of the golden MCP schema fixture for exactly `adoption_studio`, `review_memory`, `triage_memory`, and `review_item_context`.

#### Scenario: Review verbs route adoption refs correctly

- **WHEN** `review_memory(mode="adoption")` is called and when an adoption ref is passed to `triage_memory` and `review_item_context`
- **THEN** each returns the adoption-specific result, and non-adoption modes and refs behave exactly as before

#### Scenario: Golden schema regeneration is intentional and bounded

- **WHEN** the MCP schema-fidelity test runs after the change
- **THEN** the only tools whose committed baseline changed are `adoption_studio` (new), `review_memory`, `triage_memory`, and `review_item_context`
- **AND** the regenerated fixture is committed with an explicit intentional-change note and the gate passes

