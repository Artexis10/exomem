# retrieve-inject-hook (delta)

## ADDED Requirements

### Requirement: Working-set injection mode
When `EXOMEM_RETRIEVE_INJECT` resolves to the value `working_set`, the retrieve hook
SHALL call the configured service's `/api/activate_context` with the prompt text and
the persisted continuity token over the existing REST-first transport within the
existing injection budget, and SHALL inject the returned packet as `additionalContext`
under a fixed data header that names it as retrieved memory, never as instructions,
bounded by `max_chars` (default 4,000 characters). An abstained packet SHALL inject
nothing beyond the ordinary reminder. Any transport failure, non-JSON response,
budget exhaustion or a packet over the bound SHALL fall back to the existing reminder
behaviour. The mode SHALL honour the same prominence presets, prompt-length gate,
cooldown, control-prompt silence and absent-client skip as stub mode, SHALL default
off, and SHALL persist the packet's `continuity` token beside the continuation
checkpoint keyed by client and session, dropping it on `SessionStart`, `PreCompact`
and `SessionEnd`.

#### Scenario: Packet injected under a data header
- **WHEN** the mode is `working_set`, the service answers within budget with a
  resolved packet
- **THEN** the hook output's `additionalContext` starts with the fixed data header,
  carries the packet's units and pointers within `max_chars`, and the reminder text
  is not repeated

#### Scenario: Abstained packet injects nothing extra
- **WHEN** the service answers with `abstained: true`
- **THEN** the hook emits exactly the ordinary reminder

#### Scenario: Failure falls back to the reminder
- **WHEN** the service is unreachable or the response is malformed
- **THEN** the hook emits the ordinary reminder within the budget and never raises

#### Scenario: Continuity token round-trips
- **WHEN** two prompts arrive in one session and the first packet returned a token
- **THEN** the second call carries that token, and after a `PreCompact` event the
  next call carries none
