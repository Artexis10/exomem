# mutation-terminal-contract Specification

## Purpose
Persist one versioned canonical terminal record for each committed mutation and project a compact default success response from it. Pre-commit and uncertain outcomes are never reported as success, and completed idempotency rows from before terminal versioning stay replayable.

## Requirements

### Requirement: Committed Mutations Persist One Canonical Terminal
The system SHALL construct a versioned canonical terminal record after the canonical writer commits and before the idempotency receipt is marked completed. The terminal MUST contain `ok=true`, `status="committed"`, `mutated=true`, the original request identity, stable receipt identity when available, primary path/paths, and `warnings_count`, plus the complete leaf result retained for projection. The terminal SHALL be persisted before any derived-state acknowledgement (index upsert, graph refresh, advisory projection) runs for the commit. When that acknowledgement fails or exceeds its budget, the persisted terminal SHALL remain a success terminal whose `derived_sync` reads `"pending"` and whose warnings name the component class without a path; the failure MUST NOT be reported as a committed-uncertain error.

#### Scenario: Acknowledgement is lost after terminal persistence
- **WHEN** a mutation commits and deterministic fault injection interrupts delivery after the terminal is persisted
- **THEN** retrying the same identity returns the original terminal without executing the leaf again
- **AND** only one canonical write exists

#### Scenario: Retry uses a different response detail
- **WHEN** one committed identity is requested first as compact and then as full or legacy detail
- **THEN** every request resolves the same persisted terminal and original request identity
- **AND** response presentation is excluded from the mutation payload digest

#### Scenario: Derived acknowledgement fails after the commit
- **WHEN** a mutation commits and deterministic fault injection makes the derived-state acknowledgement raise or exceed its budget
- **THEN** the call returns a success terminal with `status="committed"`, `derived_sync="pending"` and a warning naming the failed component class
- **AND** the terminal is already persisted, so a same-identity retry replays it without executing the leaf again

### Requirement: Compact Success Is The Default Projection

Committed and replayed product mutations SHALL return a compact default projection led by `user_summary`, then `ok`, `status`, `mutated`, primary `path`, original `request_id`, stable receipt identity, and `warnings_count`. `user_summary` SHALL be one plain sentence built only from fields the stored terminal holds, so a repeat request with the same idempotency key returns the same text: a verb, then the path, or the first path and the count of the others. Every write operation SHALL declare its verb from a closed set (saved, updated, moved, deleted, replaced, discarded or restored), and a write that declares none SHALL carry no `user_summary`. A replacement leads with the new page and names the old one. A result whose status is `replayed` (the effect was already in place) SHALL read "Already" plus the lower-cased verb. A refusal or a validation result SHALL carry no `user_summary`. `response_detail="full"` SHALL add the complete leaf result under `diagnostics`; `response_detail="legacy"` SHALL return the pre-change raw leaf result during the compatibility window.

#### Scenario: Default committed response is decisive
- **WHEN** a governed product mutation succeeds without an explicit response detail
- **THEN** the first-level response identifies it as committed and mutated
- **AND** verbose semantic, index, warning, and transition diagnostics are not mixed into the compact terminal fields

#### Scenario: Full diagnostics are requested
- **WHEN** the same mutation is requested with `response_detail="full"`
- **THEN** the compact terminal fields are unchanged
- **AND** the complete existing leaf payload is available under `diagnostics`

#### Scenario: A committed save leads with a plain summary
- **WHEN** a governed product mutation saves one file without an explicit response detail
- **THEN** the first key of the response is `user_summary`, reading "Saved -> <path>"
- **AND** a delete reads "Deleted -> <path>", a move reads "Moved <from> -> <to>", a supersede reads "Replaced -> <new path> (was <old path>)", and a result whose status is `replayed` reads "Already saved -> <path>"
- **AND** a repeat request with the same idempotency key returns the first call's text, because it returns the stored terminal

### Requirement: Pre-Commit And Uncertain Errors Remain Unambiguous
The terminal projector MUST NOT convert rejected, busy, pending, committed-failure, or committed-uncertain outcomes into successful result dictionaries. `MUTATION_BUSY` SHALL remain retryable only when no commit occurred, and post-commit uncertainty SHALL retain its committed semantics and same-identity remediation. Committed-uncertain SHALL be returned only when the canonical commit is proven and persisting the terminal record itself failed; a failure in derived-state acknowledgement after a persisted terminal is not uncertainty and SHALL NOT produce this error.

#### Scenario: Mutation boundary is busy
- **WHEN** boundary acquisition times out before the command leaf runs
- **THEN** the call fails with structured `MUTATION_BUSY`, `status="retryable"`, and `committed=false`
- **AND** no success terminal is returned

#### Scenario: Exact post-commit terminal is unavailable
- **WHEN** canonical commit occurred but exact terminal persistence cannot be proven
- **THEN** the existing committed-uncertain error is returned
- **AND** the caller is told not to retry with a new identity

#### Scenario: Terminal persistence fails but acknowledgement would have succeeded
- **WHEN** canonical commit occurred and deterministic fault injection makes terminal persistence fail
- **THEN** the call returns committed-uncertain with `status="committed"` and `committed=true`
- **AND** a same-identity retry resolves the canonical row rather than executing the leaf again

### Requirement: Legacy Receipt Rows Stay Replayable

Completed idempotency rows created before terminal versioning SHALL remain replayable for their normal bounded retention period. The system MUST return their stored legacy result without fabricating terminal fields that were never persisted.

#### Scenario: Retry reaches a pre-upgrade completed row
- **WHEN** the idempotency store contains a valid completed raw result from the previous release
- **THEN** the result replays without leaf execution or reconciliation failure
- **AND** the row expires under the existing retention policy

### Requirement: One Narration Rule Reaches The Agent Through Two Carriers

The system SHALL define the rule for narrating a write to the user once, as `mutation_terminal.NARRATION_RULE`: tell the user `user_summary`; mention codes, ids, hashes, retries and sync state only if asked; state a blocker plainly. The compact bootstrap `write.canonical_loop` SHALL carry that sentence as its final step, and the shipped skill scaffold SHALL carry the same sentence, because a client with the skill installed receives the session profile, which omits the `write` block. The scaffold SHALL also say that working on Exomem does not make an unrelated conversation diagnostic, and every standalone workflow skill SHALL embed the same operating rules.

#### Scenario: Both carriers hold the rule
- **WHEN** the compact bootstrap and the shipped skill scaffold are read
- **THEN** each contains `NARRATION_RULE` verbatim
