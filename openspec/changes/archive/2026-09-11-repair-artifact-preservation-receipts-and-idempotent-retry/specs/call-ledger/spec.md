## MODIFIED Requirements

### Requirement: Arguments Are Recorded As Shape And Hash, Never Values
The system SHALL record call arguments as their names, per-argument byte length and sha256, and
structural target paths only. It SHALL NOT record any argument value, note content, or raw
caller credential. Caller identity SHALL be recorded as the pre-hashed principal scope, never a
raw token or subject. For artifact and evidence writes (`preserve_artifacts`, `preserve_evidence`,
`capture_source` with files), the committed vault-relative paths from the outcome SHALL be recorded
in `target_paths` so the ledger can state what landed; filenames derived from content stay
structural paths, and no file content or handle URL is recorded.

#### Scenario: Query text never reaches the ledger

- **WHEN** an `ask_memory` call carries free-text query content in its arguments
- **THEN** the ledger row records the argument name, its byte length, and its sha256
- **AND** the query text itself appears nowhere in the row

#### Scenario: Note body is reduced to shape

- **WHEN** a write call carries note body content in its arguments
- **THEN** the body content appears nowhere in the row
- **AND** the addressed note path is recorded structurally in `target_paths`

#### Scenario: Caller identity is a hash, not a credential

- **WHEN** a call is made with a verified principal or a bearer credential
- **THEN** `caller_principal_hash` is the hashed principal scope
- **AND** no raw token, authorization header, or subject value appears in the row

#### Scenario: Artifact write records what landed

- **WHEN** `preserve_artifacts` stores two files and fails a third
- **THEN** the ledger row's `target_paths` holds the two committed vault-relative paths and nothing for the failed file
- **AND** no `download_url`, file content, or handle identifier appears in the row
