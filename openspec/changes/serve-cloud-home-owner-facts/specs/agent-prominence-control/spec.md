# Spec Delta

## ADDED Requirements

### Requirement: Every context's level is reported with its source

`configure_memory` results SHALL carry `levels`, which maps each engagement context to `{level, source}`.
Each entry SHALL come from the same precedence rule that resolves a request in that context: environment, the context's saved value, the identity-wide saved value, machine config, the unreadable-record floor, then the context default.
`source` SHALL be one of `env`, `preference:context`, `preference`, `config`, `preference:unavailable` or `default`.
The map MUST report every context whatever context the caller runs in. Existing result fields SHALL stay unchanged.

#### Scenario: A settings client sees both contexts

- **WHEN** a client in the coding context inspects the configuration after the conversation context was set to Light
- **THEN** `levels.conversation` is `{level: light, source: preference:context}`
- **AND** `levels.coding` keeps the level and source that a coding client resolves

#### Scenario: The view agrees with resolution

- **WHEN** any rung of the precedence decides a level
- **THEN** each context's entry in `levels` equals the level and source that a client of that context resolves

#### Scenario: A set or clear returns the new levels

- **WHEN** a client sets or clears one context's level with a current revision
- **THEN** the result's `levels` already reflects the change
- **AND** a set that presents a stale revision is refused with `PREFERENCE_CONFLICT` and changes nothing
