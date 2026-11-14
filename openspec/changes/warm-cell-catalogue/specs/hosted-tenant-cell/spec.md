# hosted-tenant-cell Specification Delta

## ADDED Requirements

### Requirement: A cell adopts a staged copy at boot instead of rebuilding

After cellctl confirms the build Job's pods ended, the booting cell SHALL check any staged copy against its computed identity. It SHALL validate a match through a read-only connection. Under the publication lock, it SHALL handle the live main, WAL and SHM as one set before installing the staged copy. It SHALL use ordinary restart reconcile for freshness drift. The cell SHALL rebuild cold when no staged copy passes validation.

#### Scenario: The cell adopts a valid staged copy

- **WHEN** a staged copy passes validation and its stored identity equals the booting cell's computed identity
- **THEN** the cell installs it safely over the live SQLite set
- **AND** projected checkpoint comparison accepts identical state across processes without replaying the build Job's event history

#### Scenario: The vault changes after the staged build

- **WHEN** ordinary edits, additions or removals make the staged copy lag behind the vault
- **THEN** the ordinary restart reconcile walks the vault and heals the changed rows without a full parse
- **AND** drift that reconcile cannot prove still permits a cold rebuild

#### Scenario: A killed pod leaves a stale live WAL

- **WHEN** the old pod dies with committed changes in the live WAL and boot finds a valid matching staged copy
- **THEN** adoption folds the old live set or quarantines the complete set before installing the staged copy
- **AND** no old WAL attaches to the adopted catalogue or replaces its rows

#### Scenario: A staged copy fails validation

- **WHEN** a matching staged copy cannot open or fails schema, identity or checkpoint validation
- **THEN** the cell discards it and rebuilds cold
- **AND** validation does not enable WAL or modify the staged copy

#### Scenario: No valid staged copy exists

- **WHEN** no valid staged copy carries the computed identity
- **THEN** the cell retains its ordinary cold-rebuild path

#### Scenario: Installing the staged copy fails

- **WHEN** adoption cannot safely replace the live SQLite set
- **THEN** the cell declines adoption and retains the cold-rebuild fallback
- **AND** the previous set remains intact or isolated, never mixed with the new main

### Requirement: A booting cell removes every unadopted staged copy

At boot, the cell SHALL remove every completed staged copy it did not adopt, regardless of identity. Once cellctl confirms the build Job's pods ended, a released `_RebuildTempLock` SHALL suffice to remove a `.building` family without an age guard. Cleanup SHALL preserve temporary files while their locks remain held.

#### Scenario: A duplicate matches the live identity

- **WHEN** boot leaves a staged copy unadopted, including one matching the live or computed identity
- **THEN** the cell removes that copy
- **AND** no identity-based keep rule retains a duplicate catalogue

#### Scenario: Cancellation leaves a recent temporary family

- **WHEN** cellctl confirms the build Job's pods ended and the remaining `.building` family's `_RebuildTempLock` is released
- **THEN** boot removes the family without waiting for its files to age
- **AND** held locks still protect any active temporary files
