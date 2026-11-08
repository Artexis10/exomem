## ADDED Requirements

### Requirement: Source attachment capture supports exact selected-artifact adoption
`capture_source` SHALL accept the same optional validated single-artifact adoption envelope as `preserve_artifacts` while preserving ordinary Source attachment behavior. The envelope SHALL name one supplied `file_id`, an explicit trigger, and a scoped durable adoption key. Only that handle SHALL commit. The exact bytes and versioned adoption block SHALL publish atomically in the Source lane, with the Source artifact page as the canonical receipt owner. The block and stable response SHALL bind the trigger, key digest, selected identifier, Source lane, destination, path, SHA-256, size, content type, and media identity.

Transport-window and durable adoption replay SHALL use the same semantics and stable failures as Evidence adoption: cached transport replay performs no second fetch; replay after expiry re-stages and hashes bytes; mismatch fails `ADOPTION_KEY_REUSED`; unavailable bytes fail `ADOPTION_REPLAY_UNVERIFIABLE`; no failure writes or claims replay.

#### Scenario: Existing Source handle capture remains compatible
- **WHEN** an existing client calls `capture_source` with file handles and no adoption envelope
- **THEN** its ordered outcomes, exact-byte Source storage, source taxonomy, source page, and replay behavior remain unchanged

#### Scenario: Selected reasoning artifact commits to Source only
- **WHEN** an adoption envelope selects one reasoning-input artifact from several handles supplied to `capture_source`
- **THEN** only that handle is stored under Sources and its Source artifact page carries the committed adoption block
- **AND** no sibling or Evidence artifact is written

#### Scenario: Source receipt matches Evidence vocabulary
- **WHEN** the same synthetic bytes are separately adopted once as Source and once as Evidence under distinct keys
- **THEN** both receipts expose the same trigger, key-digest, selected-id, hash, size, content-type, and media-identity fields
- **AND** their lane, destination, path, and canonical companion/page correctly differ

#### Scenario: Durable Source replay reproves bytes
- **WHEN** a Source adoption key is retried after transport replay expiry
- **THEN** `capture_source` re-stages and hashes the selected handle before returning the original receipt
- **AND** changed or unavailable bytes fail closed without another Source write
