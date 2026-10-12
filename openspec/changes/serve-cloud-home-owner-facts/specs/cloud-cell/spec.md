# Spec Delta

## ADDED Requirements

### Requirement: A cloud cell names the gateway's OAuth client

On a cloud cell, the caller's client name SHALL come from the request's `clientInfo`, then from the `x-exomem-oauth-client` header, then from `User-Agent`.
Only a cloud cell SHALL read that header. The client name MUST tune eagerness only, never authority.

#### Scenario: A chat app behind the gateway

- **WHEN** a stateless handshake-protocol tool call reaches a cloud cell with `x-exomem-oauth-client` naming a conversational client
- **THEN** the call resolves in the conversation context and receives that context's level

#### Scenario: A local server ignores the header

- **WHEN** the same request reaches a server that is not a cloud cell
- **THEN** the client name comes from `User-Agent`

### Requirement: A cloud cell serves its owner's activity and note count through MCP

`bootstrap(profile="compact", section="activity")` SHALL return `{since, as_of, note_count, events}` to the owner and refuse every other principal with `ACTIVITY_OWNER_REQUIRED`.
`events` SHALL hold at most 20 events, newest first, each `{client, action, items, at}`.
Only successful calls SHALL become events. `action` SHALL be `recalled` or `saved`, derived from the command registry's product actions and the row's `access`.
Rows of one client family and action SHALL join the newest event of that pair when they are at most 60 seconds older than its newest row.
`items` SHALL be null when any joined row's count is unknown. `note_count` SHALL be the compiled-note count, or null when it cannot be read.
`since` SHALL be the oldest ledger row the list accounts for, or the ledger writer's start when there is none.
A disabled or unreadable ledger SHALL be refused with `ACTIVITY_UNAVAILABLE`.
The cell MUST NOT register a route for these facts.

#### Scenario: A recall and a save appear

- **WHEN** a chat app recalls notes and another assistant saves a note
- **THEN** the save is listed first, then the recall with the number of notes it released
- **AND** the note count has grown by one

#### Scenario: A fresh cell

- **WHEN** the owner asks a cell that has just started on an empty vault
- **THEN** `events` is empty, `since` is the ledger writer's start and `note_count` is zero

#### Scenario: An older image

- **WHEN** Home asks an image that predates the section
- **THEN** the image refuses the unknown section
