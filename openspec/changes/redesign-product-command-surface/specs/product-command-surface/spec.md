## ADDED Requirements

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
