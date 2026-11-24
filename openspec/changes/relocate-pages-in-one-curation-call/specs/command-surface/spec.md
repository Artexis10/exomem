## MODIFIED Requirements

### Requirement: Maintain memory exposes one generated curation action surface

The product registry SHALL extend `maintain_memory` with
`mode="curation"` and the finite `curation_action` set `work-item`, `propose`,
`preview`, `status`, `apply`, `resume`, `propose-compensation`,
`apply-compensation`, `revert`, and `relocate`. The registry-derived MCP tool, REST route, OpenAPI schema,
CLI subcommand, generated capability documentation, and schema fixtures SHALL
expose the same arguments and shared leaf implementation with no per-surface
curation logic.

#### Scenario: Curation is discovered on every product surface

- **WHEN** the registry generates MCP, REST, OpenAPI, CLI, and capability output
- **THEN** every surface exposes the same curation actions and input constraints
  under `maintain_memory`

#### Scenario: A promotion revert uses the common destructive surface

- **WHEN** the user requests a promotion's undo and supplies its current revert fingerprint
- **THEN** `maintain_memory(mode="curation", curation_action="revert")` applies the bound revert through the common mutation boundary

#### Scenario: A relocation takes only its own arguments

- **WHEN** a caller sends `curation_action="relocate"` with `pages` and `to_project`
- **THEN** the relocation derives and seals its plan through the common mutation boundary
- **AND** a relocation that also carries `plan` or `run_id` refuses with `INVALID_ARGUMENTS`

## ADDED Requirements

### Requirement: An unowned pointer field on any uncurated page is corrected through the pointer write

A curation `edit` step that patches one pointer field without a `writer` on a
page outside the append-only and curated trees SHALL take the governed pointer
write. It SHALL change only that field's value span, keep `updated` and every
other byte, and its compensation SHALL reverse the sealed line delta. A page in
a curated tree SHALL refuse with `CURATED_PROTECTED`.

#### Scenario: An entity's project is corrected and undone

- **WHEN** the user confirms a curation plan whose `edit` step changes an
  entity's `project` field, and later approves its compensation
- **THEN** the correction changes only the `project` value span
- **AND** the compensation returns the entity to its bytes before the correction
