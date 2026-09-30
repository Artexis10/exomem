## Purpose

Allow an authenticated user to select Exomem engagement through an agent and recover that choice on subsequent requests without affecting other users or vaults.

## ADDED Requirements

### Requirement: Agent-accessible prominence selection

The system SHALL expose one canonical inspect/set operation through MCP, REST and CLI for Off, Light, Balanced and Maximal. Inspect SHALL be read-only. Set SHALL return the resulting effective contract and durable revision, and require the revision returned by a prior inspect.

#### Scenario: Each level survives a new request
- **WHEN** an authenticated user inspects and sets any supported level
- **THEN** a subsequent bootstrap for the same principal and vault reports that level and its contract without a service restart

#### Scenario: A stale update cannot overwrite a later choice
- **WHEN** a set uses a revision older than the currently stored preference
- **THEN** it is refused without changing the preference and asks the caller to inspect again

#### Scenario: Invalid selection has no side effects
- **WHEN** a caller supplies an unsupported level or invalid action arguments
- **THEN** the operation reports an input error without writing preference state

### Requirement: Preference identity and vault isolation

The preference SHALL be scoped to the server-bound vault and verified principal. Request arguments MUST NOT select another principal, owner identity or storage path. Unresolved identities MUST NOT inspect or mutate preference state. A change MUST NOT modify other principals, other vaults, compute modes or governance/delegation ceilings.

#### Scenario: Two users and two vaults remain independent
- **WHEN** one principal selects Maximal in one vault
- **THEN** another principal in that vault and the same principal in another vault retain their own effective preferences

#### Scenario: Missing identity is refused
- **WHEN** an unresolved request attempts preference inspection or mutation
- **THEN** it receives a content-free identity error and no preference file is created

### Requirement: Honest precedence and defaults

Effective prominence SHALL resolve from a valid operator environment override, the current principal's stored preference, existing explicit machine configuration, and finally the detected client default, in that order. The response SHALL identify the effective source, saved value, scope and overriding operator value. A conflicting set under an operator override MUST be refused before writing. Known hookless clients SHALL receive their existing Maximal default when no explicit selection exists; unknown clients SHALL retain the generic default.

#### Scenario: Operator override remains authoritative
- **WHEN** the operator pins Light and an agent requests Maximal
- **THEN** the operation reports that the override prevents the change, leaves storage untouched and continues reporting Light

#### Scenario: Known web client has no stored selection
- **WHEN** a known hookless client bootstraps without an explicit preference
- **THEN** the response reports Maximal from its surface default

### Requirement: Consistent contract projection and guidance

Bootstrap, workflow effective capture and the delegation envelope SHALL use the same effective request preference. Bootstrap SHALL teach the available inspect/set route. Changing prominence MUST NOT alter the four levels' existing meanings, compute policy or authority ceilings. Cross-client persistence SHALL be promised only for the same canonical identity and vault/state root; local-owner and OAuth identities SHALL remain distinct.

#### Scenario: Saved Off disables proactive capture in every request projection
- **WHEN** a user selects Off and then resolves a workflow and bootstraps
- **THEN** both responses prohibit proactive capture while explicit user requests remain available

#### Scenario: Current conversation adopts the new contract
- **WHEN** an agent successfully sets a preference
- **THEN** the result contains the effective contract for immediate use and later bootstrap reports the same selection
