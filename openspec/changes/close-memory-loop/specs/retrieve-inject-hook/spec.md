## MODIFIED Requirements

### Requirement: Working-set injection mode
When `EXOMEM_RETRIEVE_INJECT` resolves to the value `working_set`, the retrieve hook
SHALL call the configured service's `/api/activate_context` with the prompt text and
the persisted continuity token over the existing REST-first transport within the
existing injection budget, and SHALL inject the returned packet as `additionalContext`
under a fixed data header that names it as retrieved memory, never as instructions.
The request SHALL also carry `client` and `session` attribution, with the session sent
as the session's episode key rather than the host's session identifier; when an older
service refuses those fields, the hook SHALL retry once without them within the same
budget, so attribution never costs the packet.
The rendered block SHALL carry current state first, then units, then pointers, each
with its provenance ref, SHALL keep whole items only, and SHALL never exceed the
render ceiling (`EXOMEM_RETRIEVE_INJECT_MAX_CHARS`, default 4,000 characters), dropping
trailing items rather than cutting one. A recent-context entry for an episode recap
SHALL render like one for a session capture, labelled `session`, so the agent reads it
with `read_memory`. A packet abstained as `ambiguous` SHALL inject
the data header, the competing anchors' titles and refs and one line telling the agent
to call `activate_context` with `anchor` set to the ref it chooses. A packet abstained
as `unresolved` that lists at least one candidate reached by the turn's own words (a
candidate carrying `exact_alias`, `lexical_overlap`, `claims_match` or `rare_term`)
SHALL inject the data header, at most five such candidates in the packet's order, each
as one whole line with its kind, title and ref, and the same one-line instruction;
candidates reached only by retrieval SHALL NOT be rendered, and the ordinary reminder
SHALL follow the block, because the agent may still need ordinary recall. A packet
abstained for any other reason, or as `unresolved` with no worded candidate, SHALL
inject nothing beyond the ordinary reminder. Any transport
failure, non-JSON response or budget exhaustion SHALL fall back to the existing
reminder behaviour. The mode SHALL honour the same prominence presets, prompt-length
gate, cooldown, control-prompt silence and absent-client skip as stub mode, SHALL
default off, and SHALL persist the packet's `continuity` token beside the continuation
checkpoint keyed by client and session, dropping it on every session lifecycle event
the client delivers (Claude Code: `SessionStart`, `PreCompact`, `SessionEnd`; Codex:
`SessionStart`, `PreCompact`).

#### Scenario: Packet injected under a data header
- **WHEN** the mode is `working_set`, the service answers within budget with a
  resolved packet
- **THEN** the hook output's `additionalContext` starts with the fixed data header,
  carries the packet's current state, units and pointers as whole items within the
  render ceiling, and the reminder text is not repeated

#### Scenario: Ambiguity is handed to the agent
- **WHEN** the service answers with an abstention whose reason is `ambiguous`
- **THEN** the hook injects the data header, the competing anchors' titles and refs
  and the instruction to call `activate_context` with `anchor`, and no unit text

#### Scenario: An unresolved turn hands its worded candidates to the agent
- **WHEN** the service abstains as `unresolved` and lists two candidates carrying
  `lexical_overlap` and three carrying only `retrieval`
- **THEN** the hook injects the data header, the two worded candidates with kind, title
  and ref, the instruction to call `activate_context` with `anchor`, no unit text and
  none of the retrieval-only candidates, followed by the ordinary reminder

#### Scenario: Any other abstention injects nothing extra
- **WHEN** the service answers with `abstained: true` for a reason other than
  `ambiguous`, or as `unresolved` with no candidate reached by the turn's own words
- **THEN** the hook emits exactly the ordinary reminder

#### Scenario: Failure falls back to the reminder
- **WHEN** the service is unreachable or the response is malformed
- **THEN** the hook emits the ordinary reminder within the budget and never raises

#### Scenario: Continuity token round-trips
- **WHEN** two prompts arrive in one session and the first packet returned a token
- **THEN** the second call carries that token, and after a `PreCompact` event the
  next call carries none

#### Scenario: An older service that refuses attribution still serves the packet
- **WHEN** the service rejects a request because it does not know `client` or `session`
- **THEN** the hook retries once without them and injects the returned packet as usual

#### Scenario: An episode recap renders as a session line
- **WHEN** the packet's recent-context block carries an entry whose reason is `episode`
- **THEN** the hook renders it as one whole `session` line with its title and ref

## ADDED Requirements

### Requirement: Bounded lifecycle checkpoints for task refresh

A client with applicable host events SHALL expose bounded refresh checkpoints after
substantive external evidence, before an action or final response, and after continuation
restoration. Its capability report SHALL name the events it actually supports.
The active agent SHALL decide whether new evidence or a task transition warrants refresh
and supply bounded task focus. Hooks SHALL NOT infer semantic need from tool-name lists,
word lists, regular expressions or response wording.
Tool-only clients SHALL retain agent-requested refresh with best-effort initiation.

The client SHALL deduplicate checkpoints within bounded existing conversation state using
human-turn identity, task revision and inspected source versions.
It MAY use compiler generation only when the caller already receives it.
Deduplication SHALL NOT disclose vault-wide generation or freshness counters to restricted callers.
Checkpoint bookkeeping SHALL NOT persist conversation text or its hashes.
It SHALL impose a finite refresh count and cumulative work budget for each human turn.
Refresh results SHALL NOT schedule another refresh.
Memory-only reads and activations SHALL remain within their current checkpoint budget.
Another checkpoint SHALL require distinct external evidence, a source correction or a
real task transition; a rephrased summary alone SHALL NOT qualify.

These bounds prevent recursive memory work from consuming the turn.
A limit can omit useful context, so the client SHALL report exhaustion explicitly and
leave deeper source inspection available under its existing operation budget.
Refresh unavailability SHALL NOT create a blanket action or release gate.
An action SHALL retain its own evidence and authorization requirements.
Injected packets SHALL retain the existing retrieved-data header and rendering ceiling.

When native events omit input origin, the client SHALL report correlation-scope accounting and unavailable human-origin enforcement separately.
The first controller MAY cover only statically read-only operations generated from the canonical command registry.
Its capability report SHALL identify mixed invocations and unmatched dispatch as uncovered.
Every recognized memory response SHALL remain ineligible to create an external-evidence checkpoint, including uncovered mixed responses.
An explicit end SHALL clear association without resetting totals or refunding reserved work.
An exhausted checkpoint SHALL NOT block canonical writes, imports or ordinary inspection outside its active association.

#### Scenario: The agent prepares a response after new evidence

- **WHEN** a supported checkpoint exposes newly inspected external evidence before the final response
- **THEN** the agent can request task refresh without another human message
- **AND** the original human turn remains unchanged and retrieved context remains labelled data

#### Scenario: Memory results do not trigger a recursive loop

- **WHEN** refresh leads to memory reads and another activation inside one checkpoint
- **THEN** those operations consume the existing checkpoint budget without scheduling another checkpoint
- **AND** repeated summaries cannot reset the budget or manufacture new evidence

#### Scenario: A client lacks a lifecycle event

- **WHEN** a client exposes tools but lacks the relevant post-tool or response checkpoint
- **THEN** its capability report states best-effort initiation for that checkpoint
- **AND** the tools remain usable without claiming native enforcement

#### Scenario: An exhausted checkpoint leaves ordinary work available

- **WHEN** associated static reads exhaust a native correlation scope and the agent ends their checkpoint
- **THEN** the controller allows ordinary inspection to reach its dispatcher
- **AND** repeated end and late completion receipts neither restore the allowance nor reopen association

#### Scenario: A mixed memory response cannot renew the allowance

- **WHEN** a recognized mixed memory tool returns under partial accounting coverage
- **THEN** the controller reports that invocation as uncovered
- **AND** its response creates no external-evidence invitation or new allowance

#### Scenario: A restricted caller receives no vault-wide generation

- **WHEN** disclosure removes vault-wide generation and freshness counters from a packet
- **THEN** checkpoint deduplication uses only the available caller-visible evidence
- **AND** it neither requests nor exposes those counters to complete bookkeeping

#### Scenario: Refresh is unavailable or reaches its bound

- **WHEN** refresh fails or its finite budget is exhausted
- **THEN** the client reports the limitation within the existing hook budget
- **AND** the agent follows the action's existing evidence contract rather than a new blanket refusal
