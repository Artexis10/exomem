## ADDED Requirements

### Requirement: Schema configuration governs workflow contracts without a new product command

The existing `schema_memory` product command SHALL expose subject `workflow-contracts` with operations `inventory`, `inspect`, `validate`, `resolve`, `preview`, `save`, and `refresh`. It SHALL remain one statically mixed/write-capable command because command annotations and active profiles are command-level. A selector-level classifier SHALL make `inventory`, `inspect`, `validate`, `resolve`, and `preview` lease-free and receipt-free, while `save` and `refresh` take the ordinary writer lease and preserve reasons, stale-write guards, mutation receipts, audit, and active-surface filtering. All operations SHALL route through one contract-family implementation across MCP, REST, and CLI.

For this subject, the request SHALL allow existing `operation`, `name`, `proposal`, `expected_hash`, and `why` plus new `context`; legacy `save: bool` false SHALL be ignored and true SHALL refuse `WORKFLOW_CONTRACT_INVALID_ARGUMENTS`. `context` SHALL be an exact mapping of optional `project`, `domain`, and `activity` keys that preserves missing versus explicit null. Accepted fields SHALL be exact by operation: `inventory` accepts none; `inspect` requires a saved-key `name`; `validate` requires exactly one of saved-key `name` or `proposal`; `resolve` requires `context` and accepts at most one of `name` or `proposal`, where `name` is a saved key or exact reserved `@standalone`; `preview` requires `proposal` and optionally a saved-key `name`; `save` requires `proposal` and `why`, with optional saved-key `name` plus mandatory `expected_hash` for update; `refresh` requires saved-key `name`, `expected_hash`, and `why`. `@standalone` SHALL be valid only for resolve and cannot collide because saved keys cannot contain `@`. Surplus, mixed, or missing fields SHALL refuse `WORKFLOW_CONTRACT_INVALID_ARGUMENTS`.

Leaf results SHALL be operation-specific and bounded: inventory returns released summaries/total/projection-truncation/findings only after a complete scan and otherwise refuses without a total; inspect returns normalized contract/released source/presentation drift; validate returns validity/normalized proposal/fingerprint/findings; resolve returns a resolved decision/provenance or stable bounded refusal; preview returns exact target/content/fingerprint/current hash; save and refresh return the standard mutation envelope plus saved identity and hashes. Stable semantic refusal codes SHALL include `WORKFLOW_CONTRACT_INVALID_ARGUMENTS`, `WORKFLOW_CONTRACT_INVALID`, `WORKFLOW_CONTRACT_NOT_FOUND`, `WORKFLOW_CONTRACT_INACTIVE`, `WORKFLOW_CONTRACT_DUPLICATE_IDENTITY`, `WORKFLOW_CONTRACT_INVALID_INVENTORY`, `WORKFLOW_CONTRACT_SCAN_LIMIT`, `WORKFLOW_CONTRACT_CONTEXT_INCOMPLETE`, `WORKFLOW_CONTRACT_AMBIGUOUS`, `WORKFLOW_CONTRACT_MIGRATION_REQUIRED`, `WORKFLOW_CONTRACT_MIGRATION_INDETERMINATE`, `WORKFLOW_CONTRACT_STALE`, and `WORKFLOW_CONTRACT_PATH_CONFLICT`.

#### Scenario: One registered route reaches every surface
- **WHEN** the product command registry is projected to MCP, REST, and CLI
- **THEN** each surface exposes the same workflow-contract schema and canonical implementation route without per-surface parsing or resolution logic

#### Scenario: Read selector remains non-mutating inside a mixed command
- **WHEN** a caller invokes workflow `resolve` through the statically write-capable `schema_memory` command
- **THEN** selector classification takes no writer lease, emits no mutation receipt, and does not claim a separate read-only product command

#### Scenario: Active surface exposes or omits the whole command
- **WHEN** an active profile omits `schema_memory`
- **THEN** bootstrap advertises no workflow-contract route, reports `resolution_available: false`, and uses built-in standalone only for an empty released inventory with no migration requirement; otherwise it reports fixed `workflow_resolution_unavailable` and disables contract-aware proactive routing
