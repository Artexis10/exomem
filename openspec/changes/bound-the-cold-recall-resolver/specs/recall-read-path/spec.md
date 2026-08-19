## ADDED Requirements

### Requirement: Idle Memory Reclamation Keeps What Is Expensive To Rebuild

Reclaiming memory from an idle process SHALL retain the recall resolver.

Measured on a 2,400-page vault: the resolver holds 3.05 MiB, and rebuilding it
costs a vault walk, an admission pass and a read of every admitted page —
39 seconds of page reads alone, paid on the thread of the next reader. Releasing
three megabytes from a process that is also holding a roughly one-gigabyte
embedding model does not justify that.

Eviction for correctness SHALL remain unchanged and SHALL continue to clear it,
because there a stale resolver is a wrong answer rather than a slow one.

#### Scenario: The idle reaper keeps the recall resolver

- **WHEN** an idle process reclaims rebuildable RAM
- **THEN** the page cache and the hot find caches are cleared
- **AND** the recall resolver remains resident

#### Scenario: Correctness eviction still clears it

- **WHEN** a caller evicts caches to force a re-derivation
- **THEN** the recall resolver is cleared

#### Scenario: A reader after idle reclamation does not rebuild

- **GIVEN** a resident recall resolver
- **WHEN** idle reclamation runs and a reader then requests a resolver for the
  same identity
- **THEN** it is served from memory without walking the vault
