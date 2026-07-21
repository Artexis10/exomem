## ADDED Requirements

### Requirement: Bootstrap Advertises Only Active Surface Capabilities

Every bootstrap response SHALL be built against an immutable descriptor for the invoking surface and active Tier-2 policy. Every named tool in routes, defaults, examples, catalogs, advanced guidance, and `common_tools` MUST be present in that surface's actual exported command set.

#### Scenario: MCP runs with Tier 2 disabled
- **WHEN** bootstrap is called over an MCP server whose Tier-2 tools are disabled
- **THEN** no Tier-2 command is recommended or listed as available
- **AND** every remaining bootstrap tool reference exists in live `tools/list`

#### Scenario: REST and CLI bootstrap differ from MCP
- **WHEN** bootstrap is called through REST or CLI
- **THEN** MCP-only commands are omitted from that response
- **AND** every advertised tool maps to an actual REST/OpenAPI operation or CLI command respectively

### Requirement: Canonical And Active Surface Identity Are Distinct

Bootstrap SHALL label the packaged canonical MCP discovery fingerprint separately from the active surface descriptor. It MUST NOT present the canonical full-surface fingerprint as proof that a filtered deployment exports those tools.

#### Scenario: Filtered deployment reports capabilities
- **WHEN** active command names differ from the packaged canonical MCP surface
- **THEN** bootstrap reports the active surface name, Tier-2 policy, and command names
- **AND** retains the canonical fingerprint only under an explicitly canonical label

### Requirement: Bootstrap Profiles Conform To Their Exported Surface

A conformance test SHALL inspect `compact`, `full`, and `diagnostics` bootstrap profiles for MCP, REST, CLI, and hosted surfaces and compare all tool references with the respective exported schemas.

#### Scenario: Bootstrap gains a new recommendation
- **WHEN** a tool name is added to any profile or workflow example
- **THEN** the conformance test fails unless that tool is exported on every surface where the recommendation appears or the response filters it out
