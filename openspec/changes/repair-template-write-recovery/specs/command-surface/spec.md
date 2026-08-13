## ADDED Requirements

### Requirement: Existing-page edit eligibility follows the requested operation
The product edit command SHALL allow frontmatter-less Markdown for whole-body, surgical string, batch-string, and section operations when no frontmatter-dependent sibling operation is requested. It SHALL preserve existing append-only, supersession, drift-guard, semantic-preflight, mutation-receipt, and audit-log behavior. It SHALL NOT synthesize frontmatter. Error adapters SHALL append a `missing` suffix only for arguments that were actually absent or invalid.

#### Scenario: Frontmatter-less body validation and commit are symmetric
- **WHEN** a caller validates a supported body edit against a frontmatter-less page and commits the identical candidate with the returned review fields
- **THEN** validation hashes the exact bytes committed and the committed page remains frontmatter-less

#### Scenario: Resolved path is not reported missing
- **WHEN** an operation requires frontmatter but the supplied existing path resolves to a page without delimiters
- **THEN** the error identifies the frontmatter requirement and contains no `missing: ['path']` suffix

### Requirement: Tier-two overwrite validation returns its commit field by name
Validation of `manage_memory_file(operation="create", overwrite=true)` against an existing Markdown file SHALL return `draft_token` containing the exact token accepted by the identical commit request. The response SHALL retain `transition_token` as a byte-identical compatibility alias. `semantic_transition_token` SHALL remain restricted to append review.

#### Scenario: Generic client replays overwrite validation by field name
- **WHEN** a client validates an overwrite and copies the returned `draft_token` into the otherwise identical commit call
- **THEN** the commit accepts the reviewed candidate without requiring the client to infer a response-to-request field rename

#### Scenario: Existing overwrite clients remain compatible
- **WHEN** a client reads the legacy `transition_token` response and supplies its value through `draft_token`
- **THEN** the overwrite commit continues to succeed

### Requirement: Explicit graph lineage reset is discoverable and constrained
The reconcile command and `maintain_memory(mode="reconcile")` SHALL expose `rebuild_graph` as a default-false boolean. The flag SHALL affect only graph-derived state, SHALL be valid only for reconcile, and SHALL be previewable through `dry_run=true`. Ordinary reconcile behavior SHALL remain non-destructive when the flag is omitted or false.

#### Scenario: Ordinary reconcile does not reset ambiguous lineage
- **WHEN** graph lineage is unavailable and a caller runs ordinary reconcile without `rebuild_graph=true`
- **THEN** Exomem leaves the lineage artifacts untouched and returns remediation naming the explicit opt-in action

#### Scenario: Product surfaces share the opt-in parameter
- **WHEN** command schemas are generated for MCP, CLI, and REST
- **THEN** each reconcile surface exposes the same `rebuild_graph` default, description, and validation
