## MODIFIED Requirements

### Requirement: Bounded optional conversation argument

`activate_context` SHALL accept an optional `conversation` object on every door: MCP, the CLI and the REST facade, all over the same leaf. The object has five optional fields:

- `focus`: a string of at most 240 characters;
- `recent`: a list of at most 6 entries `{role, text}`, where `role` is `user` or `assistant`, ordered oldest first;
- `refs`: a list of at most 12 canonical page refs;
- `activities`: a list of at most 4 activity keys that declare the kind of task (task-guidance);
- `entities`: a list of at most 4 canonical page refs, each within `REF_MAX_CHARS`, that declare the entities the task involves (task-guidance).

The server SHALL enforce every bound itself, deterministically:

- A `focus` over its bound SHALL be cut at the last whitespace at or before the bound.
- A `recent` entry's text SHALL be cut at the last whitespace at or before 600 characters for a `user` entry and 300 for an `assistant` entry, keeping its head.
- Entries beyond six SHALL be dropped oldest first.
- Entries SHALL then be dropped oldest first until the texts total at most 2,400 characters.
- Refs SHALL be deduplicated keeping first occurrence, non-string refs SHALL be dropped, and refs beyond twelve SHALL be dropped from the end.
- An entry with an unknown role or empty text SHALL be dropped.
- `activities` and `entities` SHALL each be deduplicated keeping first occurrence, and entries beyond four SHALL be dropped from the end. The guidance block SHALL report every dropped, malformed or unresolved entry of these two fields as a count, never as its value (task-guidance).

A request whose `conversation` is not an object, or whose every field is empty after bounding, SHALL be served exactly as a request without it. The argument SHALL never cause a refusal or an error.

The packet SHALL report `generation.conversation` for `focus`, `recent` and `refs`:

- `absent` when nothing survived bounding;
- `truncated` when any cut or drop above was applied;
- `applied` otherwise.

`activities` and `entities` SHALL NOT change `generation.conversation`. In `anchors` mode they SHALL be ignored. The value SHALL be computed before any visibility filtering. A request without `conversation` SHALL produce a packet byte-identical to the packet it produces before this change, apart from `generation.conversation = "absent"`. The argument is distinct from the continuity token's keyless thread (`generation.continuity_thread`) and SHALL NOT read or change it.

The CLI SHALL expose the fields as:

- `--focus`;
- repeatable `--recent-user` and `--recent-assistant`, kept in the order given;
- repeatable `--conversation-ref`;
- repeatable `--activity` and `--entity`, kept in the order given.

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

#### Scenario: Declared activities do not change the conversation generation

- **WHEN** a maps-mode caller sends only `activities` with six keys
- **THEN** the packet reports `generation.conversation = "absent"`, resolves at most the first four distinct keys, and reports the dropped keys as a count in the guidance block

### Requirement: Conversation content is ephemeral and never crosses callers

Conversation content SHALL be request-scoped.

**What is never written.** No conversation text, and no digest or hash of it computed by the activation path, SHALL be written to any of:

- the vault or the activation index;
- the packet cache or the heat projection;
- the activation log (`activations.jsonl`);
- the episode ledger or capture state;
- timings, logs or any other machine-local state file.

**Resolved declarations.** An activity key that resolved against the caller's view of the activity registry, and an entity ref that resolved in the caller's view, name vault vocabulary and vault pages, not conversation text. The served-context record (served-context-record) MAY store them, and the packet MAY list them. An unresolved key or ref SHALL be recorded and reported as a count only.

**Call ledger.** The call ledger SHALL record `conversation` by its name and byte length only, never a hash (call-ledger: "Arguments Are Recorded As Shape And Hash, Never Values", as modified by this change).

**Credentials.** Before anything is matched, the server SHALL run the shared egress scrubber over `focus` and every `recent` entry. A credential it recognises SHALL be removed, never matched, and its removal SHALL report `truncated`.

**Activation log.** The activation log MAY record only these, per call:

- whether `conversation` was present;
- the count of surviving `recent` entries and refs;
- the `generation.conversation` value.

**Hot profile.** Conversation refs SHALL NOT be recorded as a pick, read, citation or any other hot-profile event.

**Packet cache.** A request carrying a non-empty `conversation` SHALL be neither served from nor stored in the packet cache. The stance block cache (task-guidance) holds no conversation field and MAY serve such a request.

**Packet and token.**

- No packet field SHALL quote conversation text. Every served string still comes from vault material under the egress guard.
- The continuity token SHALL NOT encode any conversation field, a carried anchor, or a promoted anchor that did not also resolve without `conversation`.

**Hooks.** Hooks SHALL NOT forward conversation text to capture or episode recording.

#### Scenario: Machine-local state holds no conversation text

- **WHEN** a request carries a `recent` entry containing a distinctive invented phrase
- **THEN** after the call, no file under the vault's machine-local state root contains that phrase
- **AND** no file, the call ledger included, contains its sha256 or the sha256 of the serialised `conversation` argument

#### Scenario: A request with a conversation bypasses the cache

- **WHEN** two requests carry the same turn but different conversations
- **THEN** the second is compiled afresh, and is not served the first's packet

#### Scenario: An unresolved declaration leaves no trace

- **WHEN** a request declares the activity key `invented-unknown-kind`, which the registry does not hold
- **THEN** no file under the vault's machine-local state root contains that key, and the served-context record holds an unknown count of one
