## ADDED Requirements

### Requirement: Infer a corpus-backed contract
`schema_memory(operation="infer")` SHALL report frontmatter, semantic-block, and relation occurrence frequencies for a selected project/page-type corpus and return a proposed contract. It SHALL mark an element required only when present in every page of a sample of at least five pages.

#### Scenario: Small corpus remains advisory
- **WHEN** inference scans fewer than five matching pages
- **THEN** it reports frequencies but proposes no required fields or blocks

### Requirement: Persist optional contracts safely
Contracts SHALL be optional YAML files under `Knowledge Base/_Schema/contracts/`. Inference SHALL write only with `save=true`, and overwriting an existing contract SHALL require its current content hash.

#### Scenario: Drift guard prevents overwrite
- **WHEN** save targets an existing contract with a missing or stale expected hash
- **THEN** the write is refused and the existing contract remains unchanged

### Requirement: Validate without blocking ordinary writes
`schema_memory(operation="validate")` SHALL return path/span findings for contract violations. Validation SHALL never mutate pages or block ordinary writers; `strict=true` SHALL only provide a failing CLI/CI outcome.

#### Scenario: Validation reports a missing required block
- **WHEN** a page in contract scope lacks a required semantic block
- **THEN** validation returns a finding naming the page, required block, severity, and remediation

### Requirement: Diff contracts and corpus reality
`schema_memory(operation="diff")` SHALL compare a saved contract with the current inferred corpus or another contract and report field, type, enum, block, and relation changes.

#### Scenario: Corpus drift is explicit
- **WHEN** current pages introduce a new relation type and stop using a previously required field
- **THEN** diff reports both changes without modifying the contract
