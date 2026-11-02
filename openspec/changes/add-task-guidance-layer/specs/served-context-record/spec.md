## ADDED Requirements

### Requirement: One append-only table records what each activation served

Exomem SHALL keep one SQLite table, the served-context record, in the vault state directory beside the heat sidecar. Each maps-mode activation SHALL write one row with these fields:

- a row id, the derived session key that heat uses, and the time;
- the caller's principal and view;
- the map generation, never the map items;
- the slice refs, each with its match value;
- the guidance refs, each with its `why`;
- the declared activity keys that resolved, and the count of unknown keys;
- the declared entity refs that resolved, and the count of unknown entity refs, when `conversation.entities` exists;
- the slice state and the guidance state;
- the upkeep items that the response surfaced, each as its reading id and fingerprint, when the response carries any.

A row SHALL hold refs, registry keys, counts and closed states only, never vault text or conversation text. An unknown activity key or entity ref SHALL be recorded as a count only. The table SHALL be append-only: no path SHALL update a row, and only the retention sweep SHALL delete one. Opens and cites SHALL NOT be copied into the record; they stay in heat under the same session key.

#### Scenario: A row holds refs and states, not text

- **WHEN** a client declares `review-contract` and the packet serves one guidance item and three slice items
- **THEN** the record gains one row with the map generation, the three slice refs with their match, the guidance ref with its `why`, the key `review-contract`, an unknown count of zero, slice state `ready` and guidance state `selected`
- **AND** no column holds the turn, a conversation field or any page text

#### Scenario: An unknown key is only counted

- **WHEN** a client declares one registered key and one unregistered key
- **THEN** the row records the registered key and `unknown_activities = 1`, and no column holds the unregistered key

### Requirement: Recording never slows or fails the activation

The activation SHALL write its row after the packet is built, with a short busy timeout, and SHALL never fail, retry or wait on the write. The response SHALL carry the row id as `served_context_id`, added after the packet cache lookup and never stored in the cached packet. When the write fails, the response SHALL omit `served_context_id` and its diagnostics part SHALL name `served_context_unrecorded`. The record SHALL NOT share a cache with the heat ring, so a row write invalidates no heat cache.

#### Scenario: A locked table leaves the activation served

- **WHEN** the record's database is locked past the busy timeout
- **THEN** the packet is served unchanged, `served_context_id` is absent, and the diagnostics part names `served_context_unrecorded`

#### Scenario: A cached packet gets a fresh row id

- **WHEN** two identical requests are served, the second from the packet cache
- **THEN** each response carries its own `served_context_id`, and the cached packet holds neither

### Requirement: Rows expire by age and by count

A retention sweep SHALL delete rows older than `SERVED_CONTEXT_RETENTION_DAYS` (30) and the oldest rows above `SERVED_CONTEXT_MAX_ROWS` (100,000). The sweep SHALL run outside the activation request. The read-out SHALL report the window that the remaining rows cover.

#### Scenario: Old rows leave the table

- **WHEN** the sweep runs and the table holds rows from 31 days ago
- **THEN** those rows are deleted and every younger row remains

### Requirement: Rows stay with their caller and out of host logs

Rows SHALL live only in the vault state directory, which is tenant state in a hosted cell, and SHALL never be written to a host log, a timing record or the activation log. Every row SHALL be recorded under the caller's principal and view. No tool, route or packet SHALL serve one caller's rows to another caller. Only the owner read-out SHALL read rows across principals.

#### Scenario: A delegated caller cannot read rows

- **WHEN** a nonowner principal asks any surface for served-context rows
- **THEN** no surface returns them

### Requirement: The owner reads the record through one command

An owner-only command SHALL report, for each activity key and for the declared-nothing case, the served, opened and cited counts of guidance refs, with their sample sizes in rows and sessions. It SHALL join the record with heat `read` and `cite` events by session key and time, and it SHALL report the windows that both sources cover, because the heat ring can cover less time than the record. A count that a source could not cover SHALL be reported as unknown, never as zero. Given a `served_context_id`, the command SHALL show that one row, or report it as absent or expired. The command SHALL be reachable on the CLI and on the owner's REST facade over one leaf.

#### Scenario: An incident's row id resolves

- **WHEN** the owner asks the command for a `served_context_id` that an incident links
- **THEN** the command shows that row's map generation, slice refs and guidance refs, or reports that the row has expired

#### Scenario: The read-out names its windows

- **WHEN** the record covers 30 days and the heat ring covers 9 days
- **THEN** the read-out reports both windows, and its opened and cited counts cover only the 9 days with heat

### Requirement: The record learns nothing by itself

The served-context record SHALL add no automatic learning: no path SHALL change a declaration, a registry entry, a ranking or a packet because of a row. Any later change built on the record SHALL reach the vault only through a proposal that the owner reviews.

#### Scenario: A guidance item that nobody opens stays declared

- **WHEN** a guidance item is served in many rows and opened in none
- **THEN** its declaration and its selection are unchanged until the owner edits them
