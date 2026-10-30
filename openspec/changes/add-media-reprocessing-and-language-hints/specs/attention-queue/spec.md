## MODIFIED Requirements

### Requirement: Source coverage requires a live derivative

The `unprocessed_source` family SHALL select Sources by the note-type registry's `source` role, except a Source whose status resolves to the registry's `superseded` class: a live page replaced it, so no derived note is owed for it. It SHALL count a Source as compiled only when at least one page named by the Source's `ingested_into` exists, is not itself a Source or Evidence page, has a status whose registry class is `live`, and cites the Source in its own `sources:` claim as resolved by source closure. Every other selected Source SHALL remain in the family. Its finding SHALL list each derivative that fails the rule with one reason: `pending`, `retired`, `missing` or `claim_unresolved`. The rule SHALL branch only on the registry's closed `class` and `role` attributes. The finding's fingerprint SHALL cover the Source's content hash and each derivative's reference, status class and claim result.

#### Scenario: A draft derivative keeps its Source outstanding

- **WHEN** a draft note cites a Source and the Source's `ingested_into` names that note
- **THEN** the Source remains an `unprocessed_source` finding that lists the note with reason `pending`

#### Scenario: Promoting the draft completes the Source

- **WHEN** that note's status changes to one whose class is `live` and its claim still resolves
- **THEN** the Source leaves the family

#### Scenario: A stale back-reference does not count

- **WHEN** a Source's `ingested_into` names a page that no longer exists
- **THEN** the Source remains a finding that lists the name with reason `missing`

#### Scenario: A changed derivative state resurfaces a dismissed finding

- **WHEN** an `unprocessed_source` finding is dismissed and its derivative later changes from draft to retired
- **THEN** the fingerprint changes and the finding is open again

#### Scenario: A replaced extraction is not backlog

- **WHEN** a Source page has a status of the superseded class and no note compiles it
- **THEN** the `unprocessed_source` audit does not list it

#### Scenario: A live Source still waits for compilation

- **WHEN** a Source page has a live status and no note compiles it
- **THEN** the `unprocessed_source` audit lists it as before
