## ADDED Requirements

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
