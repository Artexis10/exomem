# Spec Delta

## ADDED Requirements

### Requirement: A cloud cell takes the client's name and context from the gateway

On a cloud cell, the caller's client name SHALL come from the request's `clientInfo`, then from the `x-exomem-oauth-client` header, then from `User-Agent`.
The applied engagement context SHALL come from the `x-exomem-client-context` header when it holds `coding` or `conversation`, before name detection.
Only a cloud cell SHALL read either header: there the cell bearer proves the gateway sent the request.
Neither header name SHALL start with `mcp-param-`, the one prefix the gateway forwards from a caller.
Both headers MUST tune eagerness only, never authority.

#### Scenario: A chat app behind the gateway

- **WHEN** a stateless handshake-protocol tool call reaches a cloud cell with `x-exomem-client-context: conversation`
- **THEN** the call resolves in the conversation context and receives that context's level

#### Scenario: A local server ignores the headers

- **WHEN** the same request reaches a server that is not a cloud cell
- **THEN** the client name comes from `User-Agent` and surface detection decides the context

### Requirement: A cloud cell serves its owner's activity and note count through MCP

`bootstrap(profile="compact", section="activity")` SHALL return `{since, as_of, note_count, events}` to the owner and refuse every other principal with `ACTIVITY_OWNER_REQUIRED`.
`events` SHALL hold at most 20 events, newest first, each `{app_id, client, action, items, at}`.
Only successful calls SHALL become events. `action` SHALL be `recalled` or `saved`, derived from the command registry's product actions and the row's `access`.
Rows of one validated app ID, client family and action SHALL join the newest event of that key when they are at most 60 seconds older than its newest row.
`items` SHALL be null when any joined row's count is unknown.
`note_count` SHALL count every page saved in the Knowledge Base, sources, records and compiled notes alike, without navigation pages or collection manifests.
`note_count` SHALL be null when any part of the Knowledge Base cannot be read, and MUST NOT be older than a committed write.
The list SHALL stop at the first row it cannot represent: a row that does not match its `row_hash`, a broken link to the newer row, a version 1 row or a call the full list has no room for.
`since` SHALL be the oldest ledger row the list accounts for, or the ledger writer's start when there is none.
`since` MUST be strictly later, at millisecond precision, than every row the list omits and every call whose row the ledger failed to append. The counts SHALL include only the rows the list accounts for.
On a cloud cell, `since` MUST NOT be earlier than the start of the ledger writer that answers, because a restarted container cannot see a call that its predecessor failed to record.
A disabled or unreadable ledger, or a history with no intact row later than where the list stops, SHALL be refused with `ACTIVITY_UNAVAILABLE`.
The cell MUST NOT register a route for these facts.

#### Scenario: A recall and a save appear

- **WHEN** a chat app recalls notes and another assistant saves a note
- **THEN** the save is listed first, then the recall with the number of notes it released
- **AND** the note count has grown by one

#### Scenario: A full list

- **WHEN** twenty newer events fill the list and an older save has no room
- **THEN** `since` is newer than that save

#### Scenario: A damaged row

- **WHEN** a recall's row cannot be read or no longer matches its `row_hash`, and newer rows are intact
- **THEN** `since` is strictly later than the damaged row, even when a readable row shares its millisecond
- **AND** no count includes the damaged row

#### Scenario: A fresh cell

- **WHEN** the owner asks a cell that has just started on an empty vault
- **THEN** `events` is empty, `since` is the ledger writer's start and `note_count` is zero

#### Scenario: An older image

- **WHEN** Home asks an image that predates the section
- **THEN** the image refuses the unknown section

### Requirement: Cloud actor metadata identifies the authenticated grant

The authenticated gateway SHALL supply the stable OAuth grant UUID in `x-exomem-app-id`.
It MUST NOT forward a caller's copy of that header or derive its value from handshake names.
A cloud cell SHALL read the header through its existing trusted cell-bearer boundary, independently of client-name resolution.
The actor ID SHALL be canonical UUID text parsed by a UUID parser, or null when missing, malformed or unavailable.
A local server SHALL report null even when a request supplies that header.
Each owner activity event SHALL expose the validated actor ID as `app_id`, or null for older or unresolved rows.
Actor metadata MUST NOT change principal resolution, authorization, engagement context or any authority ceiling.

#### Scenario: Two grants share a client family

- **WHEN** two authenticated grants of one client family save within the merge window
- **THEN** each grant has a separate event with its own `app_id` and item count
- **AND** later calls from one grant join only that grant's event of the same family and action

#### Scenario: Identity cannot be resolved

- **WHEN** a cloud request lacks a canonical actor header, or its HTTP headers are unavailable
- **THEN** its row and event report null `app_id` without a client-name fallback

#### Scenario: A local caller supplies an actor ID

- **WHEN** a local request supplies a valid actor header or a matching handshake name
- **THEN** its row and event report null `app_id`

#### Scenario: Older rows remain unattributed

- **WHEN** a schema 2 row lacks `app_id`
- **THEN** activity retains its count and reports null `app_id`
- **AND** it never merges into an event with a resolved actor ID
