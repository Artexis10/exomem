## MODIFIED Requirements

### Requirement: Bounded optional conversation argument

`activate_context` SHALL accept an optional `conversation` object on every door: MCP, the CLI and the REST facade, all over the same leaf. On the CLI and the REST facade the object has three optional fields:

- `focus`: a string of at most 240 characters;
- `recent`: a list of at most 6 entries `{role, text}`, where `role` is `user` or `assistant`, ordered oldest first;
- `refs`: a list of at most 12 canonical page refs.

The MCP door is the model's door, and it SHALL accept only `focus` and `refs`. Its published schema SHALL NOT advertise `recent`, and it SHALL drop a `recent` field that a client still sends before bounding. The MCP door SHALL also neither advertise nor use `client`, `session` or `workspace`; without a supplied session, attribution SHALL fall back to the continuity token's thread as it already does. The retrieval hook SHALL keep sending earlier turns through the CLI and the REST facade.

The server SHALL enforce every bound itself, deterministically:

- A `focus` over its bound SHALL be cut at the last whitespace at or before the bound.
- A `recent` entry's text SHALL be cut at the last whitespace at or before 600 characters for a `user` entry and 300 for an `assistant` entry, keeping its head.
- Entries beyond six SHALL be dropped oldest first.
- Entries SHALL then be dropped oldest first until the texts total at most 2,400 characters.
- Refs SHALL be deduplicated keeping first occurrence, non-string refs SHALL be dropped, and refs beyond twelve SHALL be dropped from the end.
- An entry with an unknown role or empty text SHALL be dropped.

A request whose `conversation` is not an object, or whose every field is empty after bounding, SHALL be served exactly as a request without it. The argument SHALL never cause a refusal or an error.

The packet SHALL report `generation.conversation`:

- `absent` when nothing survived bounding;
- `truncated` when any cut or drop above was applied;
- `applied` otherwise.

The value SHALL be computed before any visibility filtering. A request without `conversation` SHALL produce a packet byte-identical to the packet it produces before this change, apart from `generation.conversation = "absent"`. The argument is distinct from the continuity token's keyless thread (`generation.continuity_thread`) and SHALL NOT read or change it.

The CLI SHALL expose the fields as:

- `--focus`;
- repeatable `--recent-user` and `--recent-assistant`, kept in the order given;
- repeatable `--conversation-ref`.

#### Scenario: Omitting the conversation changes nothing

- **WHEN** a turn is compiled with no `conversation`, before and after this change, against the same vault state, index generation and continuity token
- **THEN** anchors, roles, units, pointers, current state, missing, ambiguity and budget are identical
- **AND** the new packet reports `generation.conversation = "absent"`

#### Scenario: An oversized conversation is bounded, not refused

- **WHEN** a caller sends ten `recent` entries of 2,000 characters each, a 1,000-character `focus` and thirty refs
- **THEN** the packet is served, compiled from at most six entries totalling at most 2,400 characters with the newest kept, a `focus` of at most 240 characters and the first twelve distinct refs
- **AND** it reports `generation.conversation = "truncated"`

#### Scenario: A malformed conversation is ignored

- **WHEN** `conversation` is a string, or every entry has an unknown role
- **THEN** the packet equals the packet for the same request without `conversation`
- **AND** it reports `generation.conversation = "absent"`

#### Scenario: The MCP door ignores earlier turns

- **WHEN** an MCP client calls `activate_context` with `conversation.recent`, `client`, `session` and `workspace`
- **THEN** the packet equals the packet for the same call without those fields
- **AND** the published MCP input schema of `activate_context` lists none of them

#### Scenario: The hook door still carries earlier turns

- **WHEN** the retrieval hook posts a turn with `recent` entries to the REST facade
- **THEN** the compiler uses those entries exactly as before this change
