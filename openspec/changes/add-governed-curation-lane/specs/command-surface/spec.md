## ADDED Requirements

### Requirement: Maintain memory exposes one generated curation action surface

The product registry SHALL extend `maintain_memory` with
`mode="curation"` and the finite `curation_action` set `work-item`, `propose`,
`preview`, `status`, `apply`, `resume`, `propose-compensation`, and
`apply-compensation`. The registry-derived MCP tool, REST route, OpenAPI schema,
CLI subcommand, generated capability documentation, and schema fixtures SHALL
expose the same arguments and shared leaf implementation with no per-surface
curation logic.

#### Scenario: Curation is discovered on every product surface

- **WHEN** the registry generates MCP, REST, OpenAPI, CLI, and capability output
- **THEN** every surface exposes the same curation actions and input constraints
  under `maintain_memory`

### Requirement: Curation selector classification is conservative and exact

`invocation_is_read_only` SHALL classify curation `work-item`, `preview`, and
`status` as read-only and every other known curation action as a mutation. An
omitted or unknown curation action SHALL take the conservative mutation path and
then fail validation before leaf dispatch. Arguments belonging to another
maintenance mode SHALL be refused rather than ignored or reinterpreted.

#### Scenario: Read-only status bypasses mutation acquisition

- **WHEN** `maintain_memory(mode="curation", curation_action="status")` is
  invoked with a valid run id
- **THEN** it follows the common read-only path and performs no canonical write

#### Scenario: Unknown action cannot become an unreceipted read

- **WHEN** a caller supplies a future or misspelled curation action
- **THEN** the invocation is treated as write-capable and returns
  `INVALID_CURATION_ACTION` before any curation or content leaf executes

### Requirement: Curation mutations use the shared terminal envelope

Successful curation plan creation, approval, step execution, resume, and
compensation mutations SHALL pass through the common writer authority,
vault-scoped mutation boundary, idempotency, canonical terminal, graph/due-state
settlement, and response-detail projection. Expected plan, binding, and outcome
failures SHALL retain stable application error codes and SHALL NOT be converted
to successful result dictionaries.

#### Scenario: Hosted or MCP acknowledgement is retried

- **WHEN** a curation mutation acknowledgement is lost and the same principal
  repeats the exact canonical request within its retry contract
- **THEN** the common terminal path replays the result or the curation witness
  recovers it without executing a second content effect
