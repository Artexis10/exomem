## ADDED Requirements

### Requirement: Consistent Review Item Context Command

The system SHALL define one read-only Tier 1 `review_item_context` command in the product registry and expose it consistently through MCP, REST, and CLI from the same leaf implementation. The command SHALL accept a stable `exomem://review/<id>` reference plus explicit bounds for target body, related pages, graph nodes and edges, history entries, and evolution versions.

#### Scenario: One registry command reaches every product surface

- **WHEN** the product command registry and generated surfaces are inspected
- **THEN** MCP, `/api/review_item_context`, and CLI all expose the same required review reference and bound parameters
- **AND** each surface returns the same command result shape for equivalent inputs

### Requirement: Stable Item Resolution Across Review Modes

The command SHALL resolve the current review item by stable identity across the daily attention queue and the separate corpus-activation queue without depending on current rank. If no current item matches, or the supplied fingerprint no longer matches an optionally supplied expected fingerprint, the command MUST return an explicit not-found or changed-item error that instructs the caller to refresh rather than silently resolving another page.

#### Scenario: Rank change does not change resolution

- **WHEN** a review item's rank changes but its target identity remains current
- **THEN** the same `exomem://review/<id>` reference resolves the same target and current reasons

#### Scenario: Materially changed item requests refresh

- **WHEN** the caller supplies an expected fingerprint and the current item's fingerprint differs
- **THEN** the command returns a changed-item error with the current stable reference
- **AND** no stale context is presented as if it matched the reviewed signal

### Requirement: Deterministic Bounded Context Composition

The command SHALL compose the review item, target page metadata and bounded body, related-page summaries, canonical references, provenance/evidence links, bounded graph neighborhood, bounded edit history, current review decision, and path-specific recorded supersession evolution. Every capped section SHALL report its shown and omitted counts or an equivalent explicit truncation marker. The command MUST NOT return unrestricted related-page bodies.

#### Scenario: Complete bounded response

- **WHEN** a current review target has related pages, provenance, graph edges, history, and supersession versions
- **THEN** the response groups those records into named sections with canonical references and applied bounds
- **AND** any omitted records are explicitly counted or marked as truncated

#### Scenario: Bounds prevent payload growth

- **WHEN** the target has more related records than the requested limits
- **THEN** each section returns at most its requested limit
- **AND** the response reports that additional records were omitted

### Requirement: Pure-Substrate Assembly And Partial Availability

`review_item_context` SHALL perform deterministic assembly over recorded data and existing measurement helpers. It MUST NOT invoke a reasoning or generative model, infer a relationship, assign epistemic confidence, mutate the vault, or alter retrieval ranking. Optional or absent graph, provenance, history, and evolution sections SHALL soft-fail independently with explicit availability metadata while the remaining permitted sections are returned.

#### Scenario: Command is read-only without models

- **WHEN** the command runs with embeddings and all optional model-backed features disabled
- **THEN** it returns deterministic recorded context without creating, modifying, moving, or deleting vault files
- **AND** no model is loaded or invoked

#### Scenario: One unavailable section does not erase the response

- **WHEN** an optional context helper cannot provide its section
- **THEN** the response marks only that section unavailable with a bounded reason
- **AND** the target, review reasons, and other permitted deterministic sections remain available

### Requirement: Access Policy And Content Minimization

The command SHALL apply the existing access and lifecycle policy before including target or related content and SHALL omit content the caller's product surface cannot read. It SHALL prefer summaries and canonical references for related material, exclude secrets and authentication values from output, and return an explicit permission error when the target itself is not readable.

#### Scenario: Unreadable related page is not disclosed

- **WHEN** a graph or provenance edge points to a page outside the readable policy for the current operation
- **THEN** the response omits that page's body and protected metadata
- **AND** does not reveal it through a fallback file read

### Requirement: Path-Specific Recorded Evolution

The composed evolution section SHALL resolve the selected target's supersession chain directly by canonical path/reference rather than by a topic search. Versions SHALL be ordered by supersession pointers and SHALL include only recorded structural claims and transition reasons. A target outside a multi-version chain SHALL return an empty evolution section rather than a semantic approximation.

#### Scenario: Known target avoids topic ambiguity

- **WHEN** multiple unrelated notes share similar titles or text but the caller supplies one review reference
- **THEN** the evolution section follows only the selected target's recorded supersession chain
- **AND** similar notes are not merged into that timeline
