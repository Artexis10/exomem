## ADDED Requirements

### Requirement: Explicit attempts bind exact authored input

Optional capture attempts SHALL use fresh server-issued handles bound to exact typed authored input, Source identity and authenticated principal scope.
The binding SHALL precede input-changing normalization and survive credential rotation.
Reservation SHALL retain no capture content.
Ordinary capture without an attempt SHALL retain its existing behavior.

#### Scenario: Canonically equivalent strings differ

- **WHEN** a reserved capture is submitted with different UTF-8 strings that Unicode normalization would equate
- **THEN** input binding refuses it before canonical writes

#### Scenario: A cell bearer rotates

- **WHEN** the authenticated cell-service identity remains unchanged after bearer rotation
- **THEN** its attempt remains bound to that same identity without requiring a gateway origin key

### Requirement: Attempt admission and settlement share canonical exclusion

Commit SHALL validate an existing attempt inside the actual non-borrowed canonical mutation boundary and retain it through terminal persistence.
Settlement SHALL use that same boundary to close admission.
Missing, expired, cancelled or consumed attempts MUST NOT recreate execution.
Admission expiry MUST NOT cancel accepted execution.

#### Scenario: A delayed request arrives after settlement

- **WHEN** settlement closes an unused attempt before its delayed capture reaches the boundary
- **THEN** the later capture refuses without a canonical write

#### Scenario: Accepted execution outlives the caller

- **WHEN** a capture holds the boundary after its caller stops waiting
- **THEN** settlement reports pending or waits within its bounded admission policy
- **AND** it cannot claim admission closed before that capture leaves the boundary

#### Scenario: A nested callback settles its ancestor

- **WHEN** an executing capture synchronously settles its own ticket, including through another nested capture
- **THEN** settlement reports pending without cancelling the ancestor
- **AND** inherited context on another thread does not impersonate ownership of the boundary

### Requirement: Closure reports its actual custody scope

Reservation and settlement SHALL identify observed cell, replica, persistent ledger instance and canonical-boundary identity.
The consumer SHALL distinguish local admission closure from past capture outcome and current Source presence.
Changed or unproved custody SHALL remain unknown.
Settlement MUST NOT scan Sources or execute recovery capture to manufacture a terminal.

#### Scenario: A terminal receipt is unavailable

- **WHEN** the same authoritative scope closes an attempt without a retained success receipt
- **THEN** admission is closed here and past outcome remains unknown
- **AND** complete inventory supplies current Source state separately

#### Scenario: The runtime ledger was replaced

- **WHEN** a new database has a different ledger-instance identity
- **THEN** settlement does not claim closure for a receipt from the earlier ledger

#### Scenario: An earlier issuer remains alive but idle

- **WHEN** settlement closes its attempt under the same canonical boundary and unchanged ledger scope
- **THEN** a later request from that issuer cannot execute the closed attempt
- **AND** closure requires no process-retirement assertion or newer lease token

### Requirement: Protocol observations bypass generic replay

Reservation, settlement and ticketed capture SHALL bypass explicit and implicit generic replay before saved-terminal lookup.
They SHALL preserve ordinary authority, mutation fencing and terminal filtering.
The protocol SHALL retain its own exact terminals and collect eligible history in indexed bounded batches without permanent identity tombstones.

#### Scenario: A caller repeats a reservation request

- **WHEN** a previous reservation was consumed or cancelled
- **THEN** a new reservation call allocates a new handle rather than replaying the old one

#### Scenario: A caller polls settlement with the same invocation key

- **WHEN** an earlier settlement observed pending and the capture has since completed
- **THEN** settlement observes the current state rather than replaying pending

#### Scenario: Collected history no longer has a receipt

- **WHEN** a closed attempt's retained row has been collected
- **THEN** local closure remains provable under its unchanged authoritative scope
- **AND** its past outcome remains unknown without recreating the attempt
