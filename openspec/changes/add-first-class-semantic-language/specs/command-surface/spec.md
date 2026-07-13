## ADDED Requirements

### Requirement: Semantic Unit Parameters Are Consistent Across Surfaces
The single command registry SHALL expose `result_level`, `categories`, and `kinds` on recall commands; exact unit selection on read commands; category/kind contract controls on schema commands; and semantic-unit feedback on writers consistently across MCP, REST, CLI, OpenAPI, and generated capability documentation.

#### Scenario: Recall schema parity
- **WHEN** generated schemas are inspected for MCP `ask_memory`, REST `/api/ask_memory`, CLI `ask-memory`, and OpenAPI
- **THEN** the same accepted result-level/category/kind parameters and descriptions are present

#### Scenario: Existing default schema remains compatible
- **WHEN** callers omit every semantic-unit parameter
- **THEN** the existing page-level request and response behavior remains compatible

### Requirement: Observe Memory Is A Generated Core Operation
The command registry SHALL define `observe_memory` once and expose its add/update/remove/validate contract on MCP, REST, CLI, and OpenAPI with shared write annotations, drift guards, compact-relation rejection, result envelope, and error codes.

#### Scenario: One registry entry exposes observe everywhere
- **WHEN** the command surface is generated
- **THEN** `observe_memory`, `/api/observe_memory`, the CLI observe verb, and its OpenAPI schema derive from one registry entry

#### Scenario: Mutation errors match across surfaces
- **WHEN** an observe update fails for a stale unit fingerprint through CLI and REST
- **THEN** both return the same machine-readable stale-reference code and remediation

### Requirement: Creation Draft Review Protocol Is Surface-Complete
Every creation-capable governed writer surface SHALL expose `validate_only`, `draft_id`, `draft_hash`, `relation_disposition`, `relation_review_hash`, and `relation_review_reason` consistently. Validation responses SHALL preserve deterministic relation findings/candidates; commit responses/errors SHALL preserve hash, identity, and atomic-review status across MCP, REST, CLI, OpenAPI, and generated capability documentation.

#### Scenario: Reviewed-none creation round-trips across facades
- **WHEN** a caller validates a disconnected page through MCP and commits the unchanged draft through REST
- **THEN** the shared draft identity/hash is accepted and the page plus portable reviewed-none state commit atomically

#### Scenario: Draft mismatch error is identical across facades
- **WHEN** a reviewed-none commit changes content after validation
- **THEN** every facade returns the same machine-readable draft-mismatch code and fresh-validation remediation

### Requirement: Semantic Unit Response Shape Is Documented And Fidelity-Tested
Generated schemas and fidelity snapshots SHALL cover page, unit, and mixed recall envelopes; semantic-unit writer feedback; category registry/schema outputs; and exact unit reads. No surface may silently drop unit identity, parent citation, category/kind, degradation, or contract findings.

#### Scenario: Unit response survives every facade
- **WHEN** the same unit lookup runs through MCP, REST, and CLI JSON
- **THEN** each structured response preserves the same unit and parent identity fields and contract metadata
