# retrieve-inject-hook (delta)

## ADDED Requirements

### Requirement: Working-set injection mode
When `EXOMEM_RETRIEVE_INJECT` resolves to the value `working_set`, the retrieve hook
SHALL call the configured service's `/api/activate_context` with the prompt text and
the persisted continuity token over the existing REST-first transport within the
existing injection budget, and SHALL inject the returned packet as `additionalContext`
under a fixed data header that names it as retrieved memory, never as instructions.
The rendered block SHALL carry current state first, then units, then pointers, each
with its provenance ref, SHALL keep whole items only, and SHALL never exceed the
render ceiling (`EXOMEM_RETRIEVE_INJECT_MAX_CHARS`, default 4,000 characters), dropping
trailing items rather than cutting one. A packet abstained as `ambiguous` SHALL inject
the data header, the competing anchors' titles and refs and one line telling the agent
to call `activate_context` with `anchor` set to the ref it chooses; a packet abstained
for any other reason SHALL inject nothing beyond the ordinary reminder. Any transport
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

#### Scenario: Any other abstention injects nothing extra
- **WHEN** the service answers with `abstained: true` for a reason other than
  `ambiguous`
- **THEN** the hook emits exactly the ordinary reminder

#### Scenario: Failure falls back to the reminder
- **WHEN** the service is unreachable or the response is malformed
- **THEN** the hook emits the ordinary reminder within the budget and never raises

#### Scenario: Continuity token round-trips
- **WHEN** two prompts arrive in one session and the first packet returned a token
- **THEN** the second call carries that token, and after a `PreCompact` event the
  next call carries none
