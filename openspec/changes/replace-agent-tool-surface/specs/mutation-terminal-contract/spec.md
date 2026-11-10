## MODIFIED Requirements

### Requirement: One Narration Rule Reaches The Agent Through Two Carriers

The system SHALL define the rule for narrating a write to the user once, as `mutation_terminal.NARRATION_RULE`: tell the user `user_summary`; mention codes, ids, hashes, retries and sync state only if asked; state a blocker plainly. The compact `read_instructions` profile, `write.canonical_loop` SHALL carry that sentence as its final step, and the shipped skill scaffold SHALL carry the same sentence, because a client with the skill installed receives the session profile, which omits the `write` block. The scaffold SHALL also say that working on Exomem does not make an unrelated conversation diagnostic. It SHALL tell the agent that the bootstrap's `store` names the Exomem the connector reaches, that when the user asks for a different Exomem it uses the connector the user named or says plainly that it is unavailable and writes nothing elsewhere, and that it never reports a write as landing in a store other than the one `user_summary` names. Every standalone workflow skill SHALL embed the same operating rules.

#### Scenario: Both carriers hold the rule
- **WHEN** the compact bootstrap and the shipped skill scaffold are read
- **THEN** each contains `NARRATION_RULE` verbatim

#### Scenario: Every skill carries the store rule
- **WHEN** the core skill and each standalone workflow skill are read
- **THEN** each tells the agent to use the connector the user named and never to report a write in another store
