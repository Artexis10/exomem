## ADDED Requirements

### Requirement: Semantic Authoring Guidance Is Projected Consistently And Bounded

Every generated surface that teaches or performs semantic writes SHALL project the same semantic authoring contract identity, short role-first selection rule, one compact example, and route to full bootstrap guidance. Full bootstrap, the public reference, generic scaffold, and workflow skills SHALL project the complete core vocabulary. Parity tests MUST cover contract identity and the appropriate bounded/full projection. Intentional description changes MUST be regenerated and reviewed as a bounded fixture change.

#### Scenario: Generated schemas stay small and cannot drift

- **WHEN** surface projection tests run
- **THEN** MCP, REST/OpenAPI, and CLI write surfaces identify the same contract version and short selection rule without duplicating the full sixteen-label table
- **AND** bootstrap/reference/skill projections expose the exact complete vocabulary under the same contract identity

### Requirement: Semantic Writes Echo Category Resolution

Write results exposed through MCP, REST, and CLI SHALL carry the same bounded category-resolution feedback from the shared leaf operation. A surface MUST NOT invent, suppress, or reinterpret category advice independently.

#### Scenario: Alias advice is identical across surfaces

- **WHEN** equivalent deterministic fixtures invoke a semantic write using a category alias through MCP, REST, and CLI
- **THEN** each result contains the same category-feedback fields, canonical category, advisory status, and omission count
- **AND** generated identifiers that are unrelated to category resolution are excluded from the parity assertion
