## ADDED Requirements

### Requirement: Working-set mode supplies a bounded conversation tail from the local transcript

In `working_set` injection mode, where the client's prompt event carries a transcript path (Claude Code and Codex), the retrieve hook SHALL read only the transcript's final 64 KiB. It SHALL read them through the same safe regular-file open the continuation checkpoint hook uses, and SHALL send the result as the `conversation` argument of the activation request, built as follows.

**Tail.** The tail is parsed by line, and a first partial line is discarded.

**Recent turns.** `conversation.recent` holds the last user and assistant text turns before the current prompt, oldest first, within the `conversation-aware-activation` bounds.

- Only human-typed user text and the assistant's final text SHALL count.
- These are excluded:
  - tool calls and tool results;
  - thinking or reasoning blocks;
  - system and hook messages;
  - image or file payloads;
  - every block this hook or any Exomem hook injected, recognised by the fixed data header;
  - any text inside an Exomem tool result.
- The current prompt SHALL NOT be repeated in `recent`.

**Refs.** `conversation.refs` holds the canonical refs named by the arguments of Exomem read calls (`read_memory` paths) and by `anchor` arguments in the same tail, newest first, deduplicated.

- A ref SHALL be taken from the call's arguments only, never parsed out of result text.

**Focus.** The hook SHALL NOT send `conversation.focus`: it runs no model and does not summarise.

**Formats.** Each client's transcript format SHALL be read by its own closed parser. An unrecognised line SHALL be skipped. A transcript in an unrecognised format SHALL yield no conversation.

**Budget and failure.**

- Reading and parsing SHALL finish within 50 ms of wall time inside the existing injection budget.
- On timeout, an unreadable file or any parse error, the hook SHALL send the request without `conversation`, never a partial parse. It SHALL keep every other behaviour of the mode unchanged.

**No persistence.** The hook SHALL NOT write any part of the conversation to its state files, logs or the continuation checkpoint.

**Old services.** When an older service refuses the `conversation` field, the hook SHALL retry once without it within the same budget, as it already does for attribution fields.

**Origin labels.** When the hook renders a packet, each anchor's `origin` SHALL be shown on its line. A `focus` or `conversation` origin SHALL be rendered as a plain label that names who supplied the cue (for example "from earlier in this conversation"). Hooks send no `focus`, so a hook-rendered packet carries no `focus` origin, never as the user's words. Anchors with origin `turn` SHALL render exactly as today.

**Parity.** The hook script and its plugin mirror SHALL stay byte-identical.

#### Scenario: A Claude Code conversation is sent without tool payloads

- **WHEN** the transcript tail holds three user turns, two assistant replies, a `read_memory` call for one page with its result, and a previously injected Exomem block
- **THEN** the activation request's `conversation.recent` holds those five text turns, oldest first, within bounds
- **AND** `conversation.refs` holds the read page's ref
- **AND** no text from the tool result or the injected block appears in the request

#### Scenario: An unreadable transcript costs nothing

- **WHEN** the transcript path is missing, unreadable, or its tail is not in a recognised format
- **THEN** the hook sends the activation request without `conversation`, and injects exactly what it injects today for that packet

#### Scenario: An older service is not broken by the conversation

- **WHEN** the configured service rejects the request because it does not know `conversation`
- **THEN** the hook retries once without `conversation` within the same budget and injects the resulting packet

#### Scenario: The conversation never lands on disk

- **WHEN** the hook has sent a conversation containing a distinctive invented phrase
- **THEN** none of the hook's state files, logs or checkpoints contains that phrase or its sha256

#### Scenario: A carried anchor is labelled, not presented as the user's words

- **WHEN** the packet carries one anchor with `origin = "conversation"`
- **THEN** its rendered line carries the conversation label
- **AND** a packet whose anchors are all `turn` renders byte-identically to today
