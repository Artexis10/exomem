## ADDED Requirements

### Requirement: A command declares which side executes it

Each registry command SHALL carry a closed attribute `executor` whose value is `cell` or `control_plane`. The default SHALL be `cell`. A `control_plane` command runs in the Cloud control plane and never in a cell. It SHALL be listed only on the MCP surface of a Cloud cell (`EXOMEM_CLOUD_CELL=1`) and SHALL NOT be generated for any other surface or for a local or standalone server. One registry function SHALL build the Cloud tool list from `CLOUD_SURFACE_EXCLUSIONS` and this attribute; the server MUST NOT test command names to decide it. Code MUST branch on the attribute, never on a command name.

#### Scenario: A Cloud cell lists the control-plane commands

- **WHEN** a client lists tools on a Cloud cell, including a read-only Cloud cell
- **THEN** `observe_transfer` and `transfer_memory` are present with the registry's schemas and annotations
- **AND** every other tool that the Cloud surface listed before is still present

#### Scenario: Every other surface omits them

- **WHEN** a client lists tools on a local or standalone server
- **THEN** `observe_transfer` and `transfer_memory` are absent
- **AND** the local tool fixture and fingerprint are unchanged

#### Scenario: A new control-plane command needs no server change

- **WHEN** a registry row declares `executor: control_plane`
- **THEN** the Cloud tool list includes it and every other surface omits it with no edit to `server.py`

### Requirement: A control-plane command that reaches a cell is refused before any cell work

When a call for a `control_plane` command reaches a cell, the registry wrapper SHALL return the application envelope `success:false` with code `CLOUD_GATEWAY_REQUIRED`, a message and a remediation. The result SHALL NOT be a protocol `isError`. The wrapper SHALL return it before the read-only gate, the call ledger, the writer lease and any vault access, so the cell takes no lease, writes no receipt and reads no vault page, including when the cell is read-only.

#### Scenario: A direct call on a Cloud cell

- **WHEN** a client calls `transfer_memory` on a Cloud cell and the call reaches the cell
- **THEN** the result reports `success:false` with `CLOUD_GATEWAY_REQUIRED`
- **AND** no writer lease is taken and no receipt is written

#### Scenario: A direct call on a read-only Cloud cell

- **WHEN** the same call reaches a cell with `EXOMEM_CLOUD_READ_ONLY=1`
- **THEN** the result is the same `CLOUD_GATEWAY_REQUIRED` envelope, not `CLOUD_CELL_READ_ONLY`
