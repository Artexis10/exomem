## MODIFIED Requirements

### Requirement: Compact Success Is The Default Projection

Committed and replayed product mutations SHALL return a compact default projection led by `user_summary`, then `ok`, `status`, `mutated`, primary `path`, original `request_id`, stable receipt identity, and `warnings_count`. `user_summary` SHALL be one plain sentence built only from fields the stored terminal holds, so a repeat request with the same idempotency key returns the same text: a verb, then the path, or the first path and the count of the others, then the name of the store that took the write in parentheses. The store name SHALL come from `cloud_cell.store_identity()` at render time and never from the stored terminal, so it is constant per deployment. Every write operation SHALL declare its verb from a closed set (saved, updated, moved, deleted, replaced, discarded or restored), and a write that declares none SHALL carry no `user_summary`. A replacement leads with the new page and names the old one after a comma. A result whose status is `replayed` (the effect was already in place) SHALL read "Already" plus the lower-cased verb. A refusal or a validation result SHALL carry no `user_summary`. `response_detail="full"` SHALL add the complete leaf result under `diagnostics`; `response_detail="legacy"` SHALL return the pre-change raw leaf result during the compatibility window.

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
- **THEN** the first key of the response is `user_summary`, reading "Saved -> <path> (<store name>)"
- **AND** a delete reads "Deleted -> <path> (<store name>)", a move reads "Moved <from> -> <to> (<store name>)", a supersede reads "Replaced -> <new path>, was <old path> (<store name>)", and a result whose status is `replayed` reads "Already saved -> <path> (<store name>)"
- **AND** a repeat request with the same idempotency key returns the first call's text, because it returns the stored terminal

#### Scenario: The summary names the store that took the write
- **WHEN** the same committed terminal is projected by a personal deployment and by an Exomem Cloud cell
- **THEN** the personal sentence ends with "(personal Exomem)" and the Cloud sentence ends with "(Exomem Cloud)"
- **AND** every other key and its order are unchanged

### Requirement: One Narration Rule Reaches The Agent Through Two Carriers

The system SHALL define the rule for narrating a write to the user once, as `mutation_terminal.NARRATION_RULE`: tell the user `user_summary`; mention codes, ids, hashes, retries and sync state only if asked; state a blocker plainly. The compact bootstrap `write.canonical_loop` SHALL carry that sentence as its final step, and the shipped skill scaffold SHALL carry the same sentence, because a client with the skill installed receives the session profile, which omits the `write` block. The scaffold SHALL also say that working on Exomem does not make an unrelated conversation diagnostic. It SHALL tell the agent that the bootstrap's `store` names the Exomem the connector reaches, that when the user asks for a different Exomem it uses the connector the user named or says plainly that it is unavailable and writes nothing elsewhere, and that it never reports a write as landing in a store other than the one `user_summary` names. Every standalone workflow skill SHALL embed the same operating rules.

#### Scenario: Both carriers hold the rule
- **WHEN** the compact bootstrap and the shipped skill scaffold are read
- **THEN** each contains `NARRATION_RULE` verbatim

#### Scenario: Every skill carries the store rule
- **WHEN** the core skill and each standalone workflow skill are read
- **THEN** each tells the agent to use the connector the user named and never to report a write in another store
