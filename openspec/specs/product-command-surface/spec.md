# product-command-surface Specification

## Purpose
TBD - created by archiving change redesign-product-command-surface. Update Purpose after archive.

## Requirements

### Requirement: Capability-Complete Product Commands
The system SHALL expose a product command set that is easier for humans and
agents to understand while retaining the full governed capability of Exomem's
existing public command surface.

#### Scenario: Product command names cover the system
- **WHEN** the product command registry is inspected
- **THEN** it includes product commands for bootstrap, recall, reading,
  remembering, editing, supersession, source capture, source compilation,
  evidence preservation, artifact transfer, media reading, review, graph
  connection, vault adoption, maintenance, browsing, file management, and
  dataset querying
- **AND** no public capability that exists on REST or CLI is missing from MCP
  unless it is terminal-local setup/admin

#### Scenario: Product commands route through canonical leaves
- **WHEN** a product command performs work
- **THEN** it calls existing canonical implementation leaves for retrieval,
  writes, graph suggestions, file operations, transfer-token minting, media
  frame extraction, audit, reconcile, or adoption
- **AND** it does not duplicate vault path checks, write validation, append-only
  enforcement, index updates, or binary-blob guards

### Requirement: Product Commands Reduce Tool Calls
The system SHALL collapse common multi-step workflows into product commands when
that reduces agent tool calls without hiding safety choices.

#### Scenario: Remember can connect on write
- **WHEN** `remember` is called with link-suggestion enabled
- **THEN** it runs the canonical link-suggestion path before or after the
  canonical note/entity write as appropriate
- **AND** the response includes created/updated path information and proposed or
  accepted connections without requiring a separate routine `suggest_links` call

#### Scenario: Capture can return compile guidance
- **WHEN** `capture_source` writes a raw source and compile guidance is requested
- **THEN** it routes through raw-source capture and returns compilation guidance
  from the canonical compilation proposal path
- **AND** it preserves the raw source as raw provenance rather than silently
  converting it into a compiled conclusion

#### Scenario: Review unifies health surfaces
- **WHEN** `review_memory` is called with a review mode such as attention, audit,
  provenance, stale, contradiction, or unprocessed sources
- **THEN** it routes to the appropriate canonical read-only review surface
- **AND** the default mode remains read-only

### Requirement: Product Commands Preserve Safety Posture
The system SHALL make destructive or heavy behavior explicit in product command
parameters and metadata.

#### Scenario: Writes remain explicit
- **WHEN** a product command can edit, replace, move, delete, recover, adopt,
  reconcile, or fix content
- **THEN** the command schema and annotations identify the write-capable mode
- **AND** destructive operations require the same confirmation or explicit mode
  used by the canonical leaf

#### Scenario: Heavy measurement remains opt-in or mode-gated
- **WHEN** a product command can invoke embeddings, reranking, packed context,
  graph enrichment, CLIP, OCR, ASR, diarization, video-frame extraction, or
  model-backed relation suggestion
- **THEN** that behavior is off by default or selected by an explicit mode/flag
- **AND** missing optional dependencies soft-fail with actionable guidance

### Requirement: Product Surface Coverage Matrix
The system SHALL maintain a tested mapping from every public product command to
the canonical leaves it may call.

#### Scenario: No orphan product route
- **WHEN** product command metadata is validated
- **THEN** every route references an existing canonical command leaf or explicit
  hand-registered transfer/media helper
- **AND** every referenced route is covered by a test

#### Scenario: No lost canonical capability
- **WHEN** the coverage test compares existing canonical public capabilities
  against product command routes
- **THEN** every non-terminal-local canonical capability has at least one product
  command route
- **AND** the test names any intentionally excluded terminal-local setup/admin
  capability

### Requirement: Product Command Naming
The system SHALL use names that describe Exomem concepts rather than internal
storage primitives.

#### Scenario: Names are specific enough for MCP selection
- **WHEN** MCP tool names are listed
- **THEN** memory, source, evidence, artifact, review, connection, adoption,
  maintenance, file, media, and dataset commands are named distinctly
- **AND** vague names such as a bare `ask`, `get`, `add`, or `link` are not used
  as default public MCP tools

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
