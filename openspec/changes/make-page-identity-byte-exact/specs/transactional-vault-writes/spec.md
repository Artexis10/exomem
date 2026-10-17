## ADDED Requirements

### Requirement: Canonical page identity hashes stored bytes

A hash that identifies a canonical page, guards its replacement, or proves derived custody SHALL equal SHA-256 of its exact stored bytes. UTF-8 decoding and Markdown parsing MUST NOT normalize bytes before computing that identity. Deliberate semantic fingerprints SHALL remain distinct from canonical page identity.

#### Scenario: A page contains CRLF newlines

- **WHEN** a reader, write guard, or derived consumer identifies the same stored CRLF page
- **THEN** each identity equals SHA-256 of those stored bytes
- **AND** an unchanged page does not fail a guard because another reader normalized its newlines

#### Scenario: A page changes after its guarded read

- **WHEN** another writer changes the bytes after a guarded read
- **THEN** the existing drift checks refuse the stale replacement or publication

### Requirement: Identity backfill preserves existing document bytes

Adding a missing page identity SHALL preserve the document's existing newline representation and content outside the inserted field. Existing malformed or duplicate identity checks SHALL remain unchanged.

#### Scenario: Backfill adds an identity to a CRLF page

- **WHEN** backfill adds an identity to valid frontmatter in a CRLF page
- **THEN** existing document lines retain their newline bytes and the resulting identity hashes the exact installed bytes

### Requirement: Derived identity repair never rewrites canonical pages

Existing recovery MAY refresh derived hashes from current guarded page bytes. A hash mismatch MUST NOT authorize newline conversion or another canonical rewrite. Unprovable visibility SHALL retain its existing explicit unavailable state.

#### Scenario: A legacy derived row has a normalized-text hash

- **WHEN** normal recovery refreshes that row from an unchanged CRLF page
- **THEN** the derived identity becomes byte-exact and the canonical page remains unchanged
