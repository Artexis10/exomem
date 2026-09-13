## ADDED Requirements

### Requirement: Coverage distinguishes complete, partial and blocked ledgers
Records inspection SHALL extend its coverage report with `unreflected` (the count and a bounded list of claimed observations not yet reflected in the collection, at most 20 references) and `pending` (observations still inside the grace window), and the inventory SHALL report the `unreflected` count per collection. A collection SHALL read as complete when it holds no held candidates and no unreflected observations, partial when unreflected observations exist, and blocked when held candidates exist. Every count and reference SHALL pass the requesting audience's release filter for both the observation page and the manifest.

#### Scenario: Evidence without a record reads as partial
- **WHEN** an Evidence artifact whose terms cover a collection's claims was preserved and the grace window has passed with no record linking it
- **THEN** inspection reports one unreflected observation naming the artifact, inventory reports the count, and the collection reads as partial

#### Scenario: A linking record completes the ledger
- **WHEN** a record is appended to that collection with a `sources` link to the artifact
- **THEN** the unreflected count returns to zero without any dismissal and the collection reads as complete

#### Scenario: Pending is visible on request only
- **WHEN** the observation is younger than the grace window
- **THEN** inspection lists it under `pending`, and no carrier or attention listing serves it yet

#### Scenario: A withheld page is invisible
- **WHEN** the observation page is withheld from the requesting audience
- **THEN** neither the count nor the reference is served to that audience

### Requirement: Describe teaches the state-ledger convention
`record_memory(action="describe")` SHALL include a generic state-ledger example whose natural key combines an identity with an effective date, whose schema declares a status enum, an observation-quality enum with the values `exact`, `approximate` and `inferred`, and a `sources` array of links, and SHALL state that approximate or inferred values are never recorded as exact and that backfilled items cite the unit or artifact they were taken from.

#### Scenario: A generic client authors a ledger from describe alone
- **WHEN** a client authors a ledger manifest only from the example and validates it
- **THEN** validation succeeds without a guessed field or enum value

#### Scenario: The example contains no personal or product identity
- **WHEN** the scaffold leak guard scans the example
- **THEN** it finds no personal name, product name or vault-structure label
