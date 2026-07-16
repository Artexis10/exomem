## ADDED Requirements

### Requirement: Semantic Unit Parameters Are Consistent Across Surfaces
The single command registry SHALL expose `result_level`, `categories`, `kinds`, the structured `filters` expression, and `explain` on recall commands; exact unit selection on read commands; category/kind contract controls on schema commands; and semantic-unit feedback on writers consistently across MCP, REST, CLI, OpenAPI, and generated capability documentation.

#### Scenario: Recall schema parity
- **WHEN** generated schemas are inspected for MCP `ask_memory`, REST `/api/ask_memory`, CLI `ask-memory`, and OpenAPI
- **THEN** the same accepted result-level/category/kind/filter/explanation parameters and descriptions are present

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
Every creation-capable governed writer surface SHALL expose `validate_only`, `draft_id`, `draft_hash`, bounded opaque `draft_token`, `relation_disposition`, `relation_review_hash`, and `relation_review_reason` consistently. The token SHALL freeze server-derived render date, destination, and bounded project auto-registration intent without carrying raw content, review reason, registry bytes, or other auxiliary bytes. Validation responses SHALL preserve deterministic relation findings/candidates and the token; commit responses/errors SHALL preserve hash, identity, and logical-commit/prepared-recovery status across MCP, REST, CLI, OpenAPI, and generated capability documentation.

#### Scenario: Reviewed-none creation round-trips across facades
- **WHEN** a caller validates a disconnected page through MCP and commits the unchanged draft through REST
- **THEN** the shared draft identity/hash/token is accepted, portable state is prepared first, and the primary page becomes the logical commit marker across both facades

#### Scenario: Derived date and destination survive delayed commit
- **WHEN** a writer-derived draft is committed after the calendar date changes or a same-slug path appears
- **THEN** every facade uses the validated token's original date and exact destination, and occupation fails explicitly rather than selecting a different path

#### Scenario: Draft mismatch error is identical across facades
- **WHEN** a reviewed-none commit changes content after validation
- **THEN** every facade returns the same machine-readable draft-mismatch code and fresh-validation remediation

### Requirement: Semantic Unit And Explanation Response Shapes Are Documented And Fidelity-Tested
Generated schemas and fidelity snapshots SHALL cover page, unit, and mixed recall envelopes; normalized filter validation; optional retrieval profiles and ranking explanations; semantic-unit writer feedback; category registry/schema outputs; and exact unit reads. No surface may silently drop unit identity, parent citation, category/kind, ranking metric labels, fusion contributions, degradation, normalized filters, or contract findings.

#### Scenario: Unit response survives every facade
- **WHEN** the same unit lookup runs through MCP, REST, and CLI JSON
- **THEN** each structured response preserves the same unit and parent identity fields and contract metadata

#### Scenario: Explanation survives every facade
- **WHEN** the same `explain=true` hybrid lookup runs through MCP, REST, and CLI JSON
- **THEN** each response preserves the same retrieval profile, per-lane metric names, fusion contributions, boost/rerank chain, and final rank

#### Scenario: Invalid filter errors survive every facade
- **WHEN** the same malformed typed filter is submitted through MCP, REST, and CLI JSON
- **THEN** every facade returns the same stable code, JSON path, expected value shape, and remediation
