## MODIFIED Requirements

### Requirement: A user's correction of stored advice is durable, and advice turns recall

The `balanced` and `maximal` capture contracts and the activation `capture_policy` block SHALL serve one sentence that makes stored advice the user says failed, a claim the user corrects, and a decision or standing rule agreed in the conversation durable, to be saved under `proactive_capture`. Under `advisory` the agent offers the correction or the save; under `silent` it makes it. The server instructions and the native MCP activation nudge SHALL count advice on the user's own case as clear relevance for recall. At `balanced` and `maximal` the nudge SHALL also ask the agent to call `recall_context` for a task in the user's own repository or project before it inspects that repository or project; at `light` it SHALL NOT. The agent decides what counts as a correction, an agreed decision or an advice turn; no word list or detector SHALL decide it.

#### Scenario: A bootstrap-only client learns the correction rule
- **WHEN** a client reads the `balanced` or `maximal` contract from bootstrap and never calls `recall_context`
- **THEN** its capture contract carries the correction sentence

#### Scenario: An advisory vault offers the correction
- **WHEN** the effective `proactive_capture` is `advisory` and the user says stored advice failed
- **THEN** the guidance tells the agent to offer the correction, not to write it unasked

#### Scenario: An agreed decision is durable
- **WHEN** a client reads the `balanced` or `maximal` contract, or the activation `capture_policy` block, and a decision or standing rule is agreed in the conversation
- **THEN** the same served sentence tells the agent to save it under `proactive_capture`, offering it under `advisory` and saving it under `silent`

#### Scenario: Advice on the user's own case is relevant at light
- **WHEN** the level is `light` and the user asks what to do about their own system or project
- **THEN** the server instructions count the turn as clear relevance for recall

#### Scenario: Light carries no repository-task sentence
- **WHEN** the hook's level is `light` and the native MCP activation nudge is sent
- **THEN** the nudge carries no repository-task sentence and still counts advice on the user's own case

## ADDED Requirements

### Requirement: Recurring structured work carries a recognition cue on every turn
The activation `capture_policy` block SHALL carry, in its `rules` key, a short cue that routes recurring structured work to Records: named items that recur, are compared or change go to a matching collection by natural key, else to a minimal collection under the served `proactive_capture`, and narrative stays in Notes. The cue SHALL be defined once in `prominence.py` and SHALL fit the hook's render budget for `rules`. Under `off` the block SHALL carry no `rules` key and so no cue. Recognising the work SHALL be the agent's judgement of meaning; no part of the product SHALL decide it from a word list.

#### Scenario: A hookless client learns the cue on every turn
- **WHEN** a hookless client calls `recall_context` at `balanced`
- **THEN** the `capture_policy` block's `rules` carry the structured-work cue

#### Scenario: Off carries no instruction to create a collection
- **WHEN** the active disposition of `proactive_capture` is `off`
- **THEN** the `capture_policy` block carries no `rules` key and gives no instruction to save or create unasked
