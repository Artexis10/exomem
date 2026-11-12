# hosted-tenant-cell Specification Delta

## ADDED Requirements

### Requirement: A cell adopts a staged catalogue at boot instead of rebuilding

Before rebuilding, a hosted cell SHALL check eligible staged copies against its computed identity. It SHALL validate a match through a read-only connection. Under the publication lock, it SHALL handle the live main, WAL and SHM as one set before installing the candidate. It SHALL use ordinary restart reconcile for freshness drift. Missing, invalid or ineligible copies SHALL retain the cold-rebuild fallback.

#### Scenario: The warm-built catalogue is adopted

- **WHEN** an eligible staged copy passes validation and its stored identity equals the booting cell's computed identity
- **THEN** the cell installs it safely over the live SQLite set
- **AND** projected checkpoint comparison accepts identical state across processes without replaying the builder's event history

#### Scenario: The vault changes after the staged build

- **WHEN** ordinary edits, additions or removals make the staged catalogue lag behind the vault
- **THEN** the ordinary restart reconcile walks the vault and heals the changed rows without a full parse
- **AND** drift that cannot be proved still permits a cold rebuild

#### Scenario: A killed pod leaves a stale live WAL

- **WHEN** the old pod dies with committed changes in the live WAL and a valid staged copy becomes eligible for adoption
- **THEN** adoption folds the old live set or quarantines the complete set before installing the staged main
- **AND** no old WAL attaches to the adopted catalogue or replaces its rows

#### Scenario: A staged copy fails validation

- **WHEN** a matching staged copy cannot open or fails schema, identity or checkpoint validation
- **THEN** the cell discards it and rebuilds cold
- **AND** validation does not enable WAL or modify the candidate

#### Scenario: No eligible staged copy exists

- **WHEN** no valid eligible staged copy carries the computed identity
- **THEN** the cell retains its ordinary cold-rebuild path

#### Scenario: Installing the candidate fails

- **WHEN** adoption cannot safely replace the live SQLite set
- **THEN** the cell declines adoption and retains the cold-rebuild fallback
- **AND** the previous set remains intact or isolated, never mixed with the new main

### Requirement: A booting cell removes every unadopted staged copy

At boot, the cell SHALL remove every completed staged copy it did not adopt, regardless of identity. It SHALL preserve active temporary files through their locks and use family-wide age and connection checks for abandoned temporary files.

#### Scenario: A duplicate matches the live identity

- **WHEN** boot leaves a staged copy unadopted, including one matching the live or computed identity
- **THEN** the cell removes that copy
- **AND** no identity-based keep rule retains a duplicate catalogue
