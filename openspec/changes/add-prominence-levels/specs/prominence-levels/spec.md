## ADDED Requirements

### Requirement: Prominence Is Four Canonical Levels Over Three Axes

The system SHALL define exactly four canonical prominence levels — `off`, `light`, `balanced`, `maximal` — and each level MUST carry a contract over three behavioural axes: recall, capture, and narration. Every canonical level MUST have both a contract and a hook preset. Aliases MAY resolve to a canonical level and MUST NOT introduce a fifth behaviour.

#### Scenario: Every level is complete

- **WHEN** the level registry is inspected
- **THEN** the contract table and the hook-preset table each contain exactly the four canonical levels
- **AND** every alias resolves to one of those four

#### Scenario: Eagerness is monotonic

- **WHEN** `light`, `balanced`, and `maximal` presets are compared
- **THEN** each successive level has a retrieval length floor, a retrieval cooldown, a capture length floor, and a capture cooldown that is less than or equal to the previous level's
- **AND** no level is simultaneously more eager on one axis and less eager on another

### Requirement: Resolution Precedence Mirrors Compute Mode

The active level SHALL resolve as the `EXOMEM_PROMINENCE` environment variable, then the `prominence` key in the shared configuration file, then the surface default. The configuration file MUST be read explicitly and never injected into the environment, so an exported variable always wins. An unrecognised or corrupt stored value MUST degrade to the default rather than raise.

#### Scenario: Environment beats a stored level

- **WHEN** a level is stored in the configuration file and a different level is exported
- **THEN** the exported level is active
- **AND** the reported source is the environment

#### Scenario: Stored level beats the surface default

- **WHEN** a level is stored and no environment override is set
- **THEN** the stored level is active even on a surface whose default differs

#### Scenario: Unusable configuration degrades

- **WHEN** the configuration file holds an unrecognised level, or is not valid JSON
- **THEN** the surface default is active
- **AND** no exception reaches the caller

### Requirement: Hookless Surfaces Default To Maximal

The system SHALL default hookless surfaces — the hosted service, and assistants configured only through custom instructions — to `maximal`, and hook-capable surfaces to `balanced`. A nudge hook that is executing MUST assume a hook-capable client and MUST NOT infer a hookless default, regardless of surface signals in its environment.

#### Scenario: Hosted cell defaults to maximal

- **WHEN** the active runtime is a hosted cell and no level is stored or exported
- **THEN** the active level is `maximal`

#### Scenario: Local install defaults to balanced

- **WHEN** no surface signal is present and no level is stored or exported
- **THEN** the active level is `balanced`

#### Scenario: A running hook never adopts the hookless default

- **WHEN** a nudge hook resolves its level while a hosted surface signal is present in its environment
- **THEN** it resolves `balanced`, because its own execution proves the client supports hooks

### Requirement: The Level Changes Behaviour, Not Only Prose

Each level SHALL map to concrete nudge-hook tunables. An explicitly set tunable MUST continue to win over the level's value, so the level moves the default and never overrides an operator's own configuration. Selecting `off` MUST stop both nudges before any other work.

#### Scenario: Level supplies the default cadence

- **WHEN** a level is active and a tunable is unset
- **THEN** the hook uses that level's value for the tunable

#### Scenario: Explicit configuration still wins

- **WHEN** a level is active and a tunable is explicitly set to a different value
- **THEN** the hook uses the explicit value

#### Scenario: Off disables both nudges

- **WHEN** the active level is `off`
- **THEN** the capture nudge and the retrieve nudge each return without emitting a reminder

### Requirement: Standalone Hook Copies Cannot Drift

The nudge hooks are deployed as standalone copies into a client's hook directory and cannot import the package, so each SHALL carry its own copy of the preset and alias tables. The test suite MUST assert that every copy is identical to the canonical table for every canonical level, and that each hook resolves the same configuration file the CLI writes.

#### Scenario: A drifted copy fails the suite

- **WHEN** a hook's preset or alias table differs from the canonical table at any level
- **THEN** the drift test fails and names the level

#### Scenario: Hook and CLI agree on the configuration file

- **WHEN** the CLI persists a level
- **THEN** each hook resolves that level from the same path

### Requirement: The Agent Contract Reports The Active Level

`bootstrap` SHALL report the active level, its resolution source, the detected surface, the full three-axis contract, and the available levels. The contract text MUST remain free of tool names so that surface filtering, which drops any recommendation naming an uncallable command, can never strip it. Persisting a level MUST preserve every other key in the shared configuration file.

#### Scenario: Bootstrap advertises the contract

- **WHEN** a client calls `bootstrap`
- **THEN** the payload contains the active level, its source, and the recall, capture, and narration contract

#### Scenario: The contract survives a restricted surface

- **WHEN** `bootstrap` is served through a surface that exports a reduced command set
- **THEN** the engagement contract is present and unmodified

#### Scenario: Neither setting clobbers the other

- **WHEN** a compute mode and a prominence level are written in either order
- **THEN** the configuration file holds both
- **AND** rewriting either one preserves the other
