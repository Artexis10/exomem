## MODIFIED Requirements

### Requirement: A user's correction of stored advice is durable, and advice turns recall

The `balanced` and `maximal` capture contracts and the activation `capture_policy` block SHALL serve one sentence that makes stored advice the user says failed, or a claim the user corrects, durable, to be corrected under `proactive_capture`. Under `advisory` the agent offers the correction; under `silent` it makes it. The server instructions and the native MCP activation nudge SHALL count advice on the user's own case as clear relevance for recall. At `balanced` and `maximal` the nudge SHALL also ask the agent to call `recall_context` for a task in the user's own repository or project before it inspects that repository or project; at `light` it SHALL NOT. The agent decides what counts as a correction or an advice turn; no word list or detector SHALL decide it.

#### Scenario: A bootstrap-only client learns the correction rule
- **WHEN** a client reads the `balanced` or `maximal` contract from bootstrap and never calls `recall_context`
- **THEN** its capture contract carries the correction sentence

#### Scenario: An advisory vault offers the correction
- **WHEN** the effective `proactive_capture` is `advisory` and the user says stored advice failed
- **THEN** the guidance tells the agent to offer the correction, not to write it unasked

#### Scenario: Advice on the user's own case is relevant at light
- **WHEN** the level is `light` and the user asks what to do about their own system or project
- **THEN** the server instructions count the turn as clear relevance for recall

#### Scenario: Light carries no repository-task sentence
- **WHEN** the hook's level is `light` and the native MCP activation nudge is sent
- **THEN** the nudge carries no repository-task sentence and still counts advice on the user's own case
