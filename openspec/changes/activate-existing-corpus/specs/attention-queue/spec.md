## ADDED Requirements

### Requirement: Dedicated Corpus Activation Review Composition

The system SHALL expose corpus activation through `review_memory(mode="activation")`. It SHALL rank activation findings by equal-weight Reciprocal Rank Fusion with fixed category preference `unregistered_relation` > `provenance_debt` > `typed_relation_debt` > `relation_debt`, deduplicate by anchor path, preserve all contributing reasons, apply the requested cap with explicit truncation, and return the corpus coverage counts alongside the queue. The default `attention` operation and its default categories SHALL remain unchanged.

#### Scenario: Activation backlog stays out of daily attention

- **WHEN** a vault contains activation findings and `review_memory(mode="attention")` is called without categories
- **THEN** the existing daily attention categories and ordering are used unchanged
- **AND** `review_memory(mode="activation")` returns the separately ranked activation backlog and coverage

#### Scenario: Multiple activation deficits deduplicate and rise

- **WHEN** one page has both provenance and typed-relation debt while another has only typed-relation debt at the same intra-category rank
- **THEN** the first page appears once with both reasons and the sum of its RRF votes
- **AND** it ranks above the page with one signal

### Requirement: Activation Items Use Stable Review Lifecycle

Activation items SHALL receive the same stable review reference, content-bound signal fingerprint, open/all/snoozed/dismissed filtering, and triage behavior as daily attention items. Activation references SHALL be deterministically distinct from daily-attention references for the same target so item lookup and triage resolve the intended queue. Item lookup and triage SHALL resolve activation-only references. A materially changed signal SHALL resurface even if its previous fingerprint was dismissed.

#### Scenario: Activation-only item can be triaged

- **WHEN** an activation item that is absent from default attention is dismissed through `triage_memory`
- **THEN** it is hidden from the open activation view and visible in the dismissed or all view
- **AND** default attention behavior is unaffected

#### Scenario: Changed knowledge resurfaces

- **WHEN** a dismissed page is edited so its measured activation signal version changes while a deficit remains
- **THEN** the new activation fingerprint is open again

#### Scenario: Overlapping queues retain independent triage

- **WHEN** the same page appears in both daily attention and corpus activation
- **THEN** the two items have distinct stable review references
- **AND** dismissing the activation item does not dismiss or resolve to the daily-attention item

### Requirement: Activation Mode Is Shared Across Product Surfaces

The activation mode SHALL be implemented in the shared `review_memory` leaf and SHALL therefore be reachable through the generated MCP tool, REST route, OpenAPI operation, and CLI command without surface-specific activation logic.

#### Scenario: Same activation contract on every surface

- **WHEN** activation is invoked through MCP, `/api/review_memory`, and `kb review_memory --mode activation --json` over the same vault state
- **THEN** each surface returns the same coverage, ordered item paths, categories, and stable references
