## ADDED Requirements

### Requirement: Agents handle role and current-state review through existing authority

Bootstrap and shipped workflow guidance SHALL teach the active agent to inspect supporting units, choose durable destinations by artifact role, and use existing governed writers for extraction or current-state correction. Signals SHALL grant no additional authority. Existing restructure confirmation and already-authorized edit rules SHALL apply without a new blanket confirmation step. Observed transaction state SHALL continue to use Records claims routing when applicable. Guidance SHALL preserve the current compact bootstrap ceiling and MUST NOT promise that an MCP-only server observes tool-free conversation turns.

#### Scenario: A reusable result receives a role-appropriate proposal
- **WHEN** a normal-disposition role signal identifies a successful reusable method inside an experiment
- **THEN** guidance directs the agent to inspect that method and its outcome, identify a suitable reusable home, and propose any extraction requiring restructure confirmation in ordinary domain language

#### Scenario: An authorized correction preserves history
- **WHEN** a current-state review identifies outdated pending wording and the user has authorized that correction
- **THEN** guidance directs a governed local update or explicit supersession that retains the historical meaning without requesting a redundant confirmation

#### Scenario: A tool-free turn stays outside server observation
- **WHEN** a client makes no Exomem call during a conversation turn
- **THEN** the contract makes no claim that either detector analyzed that turn, while agent guidance still teaches role-first capture from available conversation context
