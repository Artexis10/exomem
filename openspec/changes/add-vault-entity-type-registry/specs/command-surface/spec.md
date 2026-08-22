## MODIFIED Requirements

### Requirement: Single Command Registry Generates Every Surface

The system SHALL define a single declarative command registry (`commands.py`) that enumerates each operation with its name, leaf function, description, parameter specs, and exposed surfaces, and the MCP tools, the REST facade, the OpenAPI document, and the CLI SHALL all be generated from it. No surface may maintain its own separate list of operations. The governed entity-type registry save SHALL be exposed by mirroring the existing relation-registry save command as operation `save-entity-types`, with the same validate-first proposal, rationale, and expected-hash argument shape.

#### Scenario: One entry exposes an op everywhere

- **WHEN** a new operation is added as a single registry entry with surfaces `{mcp, rest, cli}`
- **THEN** its MCP tool, its `/api/<name>` REST route, its OpenAPI path, and its `kb <name>` CLI subcommand all exist with no further per-surface edits
- **AND** removing the entry removes it from all surfaces

#### Scenario: Governed entity type save mirrors relation save

- **WHEN** a client invokes `save-entity-types` with `proposal`, `why`, and `expected_hash`
- **THEN** the shared command validates before saving through the entity registry leaf
- **AND** all generated surfaces expose the same parameters and result envelope

### Requirement: MCP Tools Are Generated With Byte-Identical Fidelity

The MCP tools SHALL be generated from the registry via a `bind_vault` helper that presents each leaf's signature (minus the injected `vault_root`) and the registry description to the MCP framework. A snapshot test SHALL assert each generated tool's input-schema and description are byte-identical to a committed baseline of the current tools, so intentional contract changes are explicit. `entity_type` on `create-entity` and `resolve-entity` SHALL be a free string described as a stable ID from the active core-plus-vault registry, and runtime validation SHALL reject unknown values with `ENTITY_TYPE_UNKNOWN` naming active IDs. Any tool that cannot match SHALL remain hand-registered and be named in an explicit exceptions list.

#### Scenario: Generated tool matches the baseline exactly

- **WHEN** the schema-fidelity snapshot test runs over a registry-generated tool
- **THEN** its input-schema and description equal the committed baseline byte-for-byte
- **AND** the test fails if any generated tool's schema or description differs

#### Scenario: Vault-defined entity type is representable

- **WHEN** a vault defines active entity type `place`
- **THEN** `create-entity` and `resolve-entity` accept `entity_type="place"` despite the static schema carrying no per-vault enum
- **AND** an unknown value fails with `ENTITY_TYPE_UNKNOWN` naming the active IDs

#### Scenario: Non-matching tool is an explicit exception

- **WHEN** a tool cannot be generated with a matching schema
- **THEN** it stays hand-registered and appears in the exceptions list
- **AND** the snapshot test asserts the exceptions list is explicit, with no silently-skipped tool
