## ADDED Requirements

### Requirement: Managed Full Rebuild Has One Owner

While managed retrieval is unavailable, the detached single-flight repair worker
SHALL be the only component allowed to perform a full lexical-catalogue rebuild.

#### Scenario: Startup delegates incomplete catalogue repair

- **WHEN** managed startup finds an incomplete or stale lexical catalogue
- **THEN** it schedules the detached repair worker
- **AND** does not start an in-place whole-vault rebuild beside it

#### Scenario: Ordinary maintenance cannot start a competing full rebuild

- **WHEN** a writer or watcher observes stale schema, identity, or checkpoints
  during an active detached repair
- **THEN** it records bounded repair demand for the single-flight owner
- **AND** does not perform an in-place whole-catalogue rebuild

#### Scenario: Refused reads coalesce behind the active generation

- **WHEN** repeated managed reads are refused while the current generation is
  already being repaired
- **THEN** their repair requests coalesce behind that flight
- **AND** one worker does not chain repeated whole-vault passes before yielding
