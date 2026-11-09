## MODIFIED Requirements

### Requirement: Compact Success Is The Default Projection

Committed and replayed product mutations SHALL return a compact default projection that carries only what the agent tells the user and what its next call needs: `user_summary` first, then the result fields a later call may take (the primary `path` or `paths`, `unit_ref`, the page's `after_hash`, `warnings`), then at most one `next_action` sentence, present only when the agent must act, and at most one `notice` sentence, present only when the outcome is partial, unknown or stale. The compact projection SHALL NOT carry `ok`, `state`, `terminal`, `status`, `mutated`, the operation, request and receipt identities, the idempotency key, `before_hash`, any `*_sync*` or `graph_sync_*` field, or `warnings_count`; these operational fields SHALL appear under `diagnostics.operational` when `response_detail="full"`, so a program that needs them asks for `full`. A requirement that says a mutation response reports one of these fields describes the stored terminal and the `full` projection. `warnings_count` SHALL stay in the compact projection only when it exceeds the number of warning texts shown, so a trimmed list never reads as the whole. A write that carries the page's `after_hash` keeps it in the compact projection because the next guarded edit takes it as `expected_hash`. A record or plan receipt keeps `after_container_hash`, `after_item_hash` and `after_manifest_hash` in the compact projection because the next guarded write takes them as `expected_container_hash`, `expected_item_version` and `expected_manifest_hash`; its other hashes, audit correlation and receipt markers are operational. A background update that failed after the commit SHALL read as a `notice`; a pending update SHALL read as nothing, because pending is the normal state. A guarded write that stopped for review SHALL carry a `notice` saying nothing was saved yet, and the draft's `operation_id`. `user_summary` SHALL be one plain sentence built only from fields the stored terminal holds, so a repeat request with the same idempotency key returns the same text: a verb, then the path, or the first path and the count of the others. A batch with per-file states SHALL read them from its projected rows: `Saved` SHALL cover only the files this call stored, a batch with a failed or already-stored file SHALL read "Saved S of T files -> <first stored path> and S-1 more; F files could not be saved" plus "K already saved" when K > 0, and a batch that stored nothing SHALL NOT read as "Saved" ("Already saved -> <path>", "Nothing saved; F files could not be saved", or both counts). A single file whose bytes were already stored SHALL read "Already saved -> <path>". The sentence SHALL carry no code, id or reason. Every sentence SHALL end with the name of the store that took the write in parentheses; the name SHALL come from `cloud_cell.store_identity()` at render time and never from the stored terminal, so it is constant per deployment. Every write operation SHALL declare its verb from a closed set (saved, updated, moved, deleted, replaced, discarded or restored), and a write that declares none SHALL carry no `user_summary`. A replacement leads with the new page and names the old one after a comma. A result whose status is `replayed` (the effect was already in place) SHALL read "Already" plus the lower-cased verb. A refusal or a validation result SHALL carry no `user_summary`. `response_detail="full"` SHALL add `diagnostics`, holding `operational` and the complete leaf result; `response_detail="legacy"` SHALL return the pre-change raw leaf result during the compatibility window.

#### Scenario: Default committed response is decisive
- **WHEN** a governed product mutation succeeds without an explicit response detail
- **THEN** the first-level response carries the result fields a later call may take and no operational flag, identity, hash or sync state
- **AND** verbose semantic, index, warning, and transition diagnostics are not mixed into the compact fields

#### Scenario: Full diagnostics are requested
- **WHEN** the same mutation is requested with `response_detail="full"`
- **THEN** the compact fields are unchanged
- **AND** `diagnostics.operational` holds every operational key the previous compact reply carried, and the complete existing leaf payload sits beside it under `diagnostics`

#### Scenario: A failed background update reads as one notice
- **WHEN** a mutation commits and its derived-graph, derived-state or advisory update failed
- **THEN** the compact response carries one `notice` sentence saying the change is saved and may be missing from search results
- **AND** a pending update adds no notice, and the sync fields themselves appear only under `diagnostics.operational`

#### Scenario: A trimmed warning list never reads as the whole
- **WHEN** a mutation emits more warning texts than the compact projection carries
- **THEN** the compact response carries `warnings_count` beside the trimmed `warnings`

#### Scenario: A committed save leads with a plain summary
- **WHEN** a governed product mutation saves one file without an explicit response detail
- **THEN** the first key of the response is `user_summary`, reading "Saved -> <path> (<store name>)"
- **AND** a delete reads "Deleted -> <path> (<store name>)", a move reads "Moved <from> -> <to> (<store name>)", a supersede reads "Replaced -> <new path>, was <old path> (<store name>)", and a result whose status is `replayed` reads "Already saved -> <path> (<store name>)"
- **AND** a repeat request with the same idempotency key returns the first call's text, because it returns the stored terminal

#### Scenario: The summary names the store that took the write
- **WHEN** the same committed terminal is projected by a personal deployment and by an Exomem Cloud cell
- **THEN** the personal sentence ends with "(personal Exomem)" and the Cloud sentence ends with "(Exomem Cloud)"
- **AND** every other key and its order are unchanged

#### Scenario: A partial batch never reports a failed file as saved
- **WHEN** a batch of three files stores two and fails one
- **THEN** `user_summary` reads "Saved 2 of 3 files -> <first stored path> and 1 more; 1 file could not be saved (<store name>)"
- **AND** a batch that stores nothing reads "Already saved -> <path> (<store name>)" or "Nothing saved; 1 file could not be saved (<store name>)", never "Saved"
- **AND** a single file whose bytes were already stored reads "Already saved -> <path> (<store name>)"
- **AND** a repeat request with the same idempotency key returns the same text
