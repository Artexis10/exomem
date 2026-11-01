## MODIFIED Requirements

### Requirement: Compact Success Is The Default Projection

Committed and replayed product mutations SHALL return a compact default projection led by `user_summary`, then `ok`, `status`, `mutated`, primary `path`, original `request_id`, stable receipt identity, and `warnings_count`. `user_summary` SHALL be one plain sentence built only from fields the stored terminal holds, so a replay yields the same text: a verb from the command's closed `outcome_verb` (saved, updated, moved or deleted), then the path, or the first path and the count of the others. A governed replay SHALL read "Already" plus the lower-cased verb. A refusal or a validation result SHALL carry no `user_summary`. `response_detail="full"` SHALL add the complete leaf result under `diagnostics`; `response_detail="legacy"` SHALL return the pre-change raw leaf result during the compatibility window.

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
- **AND** a delete reads "Deleted -> <path>" and a replay reads "Already saved -> <path>"

## ADDED Requirements

### Requirement: One Narration Rule Reaches The Agent Through Two Carriers

The system SHALL define the rule for narrating a write to the user once, as `mutation_terminal.NARRATION_RULE`: tell the user `user_summary`; mention codes, ids, hashes, retries and sync state only when the user asks or when debugging this write; state a blocker plainly. The compact bootstrap `write.canonical_loop` SHALL carry that sentence as its final step, and the shipped skill scaffold SHALL carry the same sentence, because a client with the skill installed receives the session profile, which omits the `write` block. The scaffold SHALL also say that working on Exomem does not make an unrelated conversation diagnostic.

#### Scenario: Both carriers hold the rule
- **WHEN** the compact bootstrap and the shipped skill scaffold are read
- **THEN** each contains `NARRATION_RULE` verbatim
