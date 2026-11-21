## ADDED Requirements

### Requirement: Importers can retry a single-file Source capture by external ID

Single-file Source capture and its hosted upload handoff SHALL accept optional `import_identity`. Exomem SHALL derive the scope from the vault and authenticated importer. It SHALL bind the idempotent receipt to the verified byte hash and authored capture metadata. Commit and receipt SHALL settle together at the existing canonical mutation boundary. Reusing an identifier with different inputs SHALL refuse before a canonical write.

The importer SHALL receive the same governed receipt for the same input after reconnect, restart, and bounded replay expiry. The receipt SHALL use the existing exact-byte duplicate outcome when capture reuses an admitted Source. The ordinary Source relation SHALL let a compiled note cite the returned reference.

#### Scenario: A reconnect retries committed bytes

- **WHEN** the same authenticated importer submits the same identifier, bytes and capture metadata after a restart
- **THEN** Exomem returns the existing receipt without adding a Source

#### Scenario: Two importers choose the same identifier

- **WHEN** two authenticated importers use the same external ID
- **THEN** the IDs resolve independently
- **AND** governed byte reuse exposes only an already readable Source

#### Scenario: Concurrent requests disagree about the bytes

- **WHEN** two requests with the same scoped identifier supply different bytes
- **THEN** at most one commits
- **AND** the other receives a conflict
