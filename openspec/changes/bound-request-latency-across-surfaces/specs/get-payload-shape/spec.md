## ADDED Requirements

### Requirement: Optional inbound links do not block the page

Direct reads with `links=true` SHALL return the permitted held page without building, walking or waiting for an inbound index.
The summary SHALL use `inbound: null` when current completeness is unknown, and a list only after complete proof.
Outbound targets SHALL use held caller-projected prose.
Existing release, exclusion and terminal checks SHALL remain in force on every transport.

#### Scenario: Cold inbound information preserves the page

- **WHEN** a direct read requests links while the inbound index is cold or recovering
- **THEN** the response retains the permitted page and outbound targets with `inbound: null`
- **AND** the read does not build, walk or wait for that index

#### Scenario: A complete lookup finds no inbound links

- **WHEN** a current complete index proves that no permitted inbound link resolves to the page
- **THEN** the summary contains `inbound: []`, distinct from unknown information

#### Scenario: A current index retains link semantics

- **WHEN** a current complete index serves optional inbound links
- **THEN** its entries retain exact contexts, line numbers and caller-visible basename resolution
- **AND** existing disclosure checks remove evidence that the caller cannot receive

#### Scenario: A changed proof withdraws completeness

- **WHEN** freshness, publication or external-pending evidence changes during a cached lookup
- **THEN** the response uses unknown inbound information instead of stale or partial entries
- **AND** it still releases the permitted held page through current terminal checks

#### Scenario: A later patch misses an earlier change

- **WHEN** an earlier event's link update is delayed and a later event patches the index first
- **THEN** that patch cannot establish complete inbound information without covering the missing change
- **AND** page reads remain available while the existing owner restores the complete index

### Requirement: Existing background ownership restores requested links

The existing background owner SHALL rebuild requested or previously used inbound state after required startup and recovery work.
It SHALL retain resource-mode cadence, foreground priority, shutdown cancellation and lazy startup.
Incomplete or obsolete work MUST NOT establish a complete index.
Explicit inbound operations and mutation safety SHALL retain their complete lookup.

#### Scenario: Requested links converge after a cold read

- **WHEN** the existing owner completes a requested build against current authority
- **THEN** a later optional summary serves the same complete inbound evidence as the exact lookup
- **AND** correctness eviction returns summaries to unknown until a current replacement publishes

#### Scenario: Observation cannot restore links yet

- **WHEN** observation is disabled, required work remains active, or changes prevent complete publication
- **THEN** optional inbound information remains visibly unknown without blocking page reads
- **AND** no additional worker or readiness gate starts

#### Scenario: Mutation safety cannot borrow an incomplete answer

- **WHEN** a move or delete needs inbound evidence while the optional index is unavailable
- **THEN** its existing complete safety lookup still runs
- **AND** unknown optional information never establishes that no inbound links exist
