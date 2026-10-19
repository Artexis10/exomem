## ADDED Requirements

### Requirement: Due advice follows the maintained client conversation
Portable guidance SHALL ask the active agent to judge semantic relevance and raise at most one due item per maintained client conversation. The agent SHALL remember any prior mention through compaction, continuation, hooks, MCP, REST, and OAuth renewal. Receipt alone SHALL NOT consume this allowance. No new runtime or notification ledger SHALL enforce it.

#### Scenario: A later hook returns another candidate
- **WHEN** the agent already raised a due item in the same client conversation
- **THEN** it does not raise the later candidate, including after compaction or credential renewal

### Requirement: Due advice names the existing review and resolution operations
Guidance SHALL name `review_item_context(ref, expected_fingerprint)` for context and one `triage_memory` dismissal for presentation disposition. Authored resolution SHALL use one applicable existing writer with its own unit or parent guards, without a second dismissal. Guidance SHALL distinguish due fingerprints from authored unit fingerprints, retain authority gates, and refresh the decision after `REVIEW_ITEM_CHANGED`.

#### Scenario: One authored writer resolves the finding
- **WHEN** an applicable authored writer records a verdict or resolving relation with its real unit or parent guard
- **THEN** the existing projection update clears the finding without an extra triage mutation

#### Scenario: One dismissal records presentation disposition
- **WHEN** `triage_memory(ref, action="dismiss", expected_fingerprint, why)` accepts the candidate's current fingerprint
- **THEN** the item stays quiet under the existing review-state rules without claiming an authored resolution

#### Scenario: A stale review fingerprint changes the decision
- **WHEN** an operation returns `REVIEW_ITEM_CHANGED`
- **THEN** the agent refreshes context and decides again before a mutation
- **AND** guidance does not claim that `review_memory` accepts `expected_fingerprint` or that context never requires a wider audit
