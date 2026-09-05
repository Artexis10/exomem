## ADDED Requirements

### Requirement: Runtime Serves The Trusted Deployment Profile

A hosted cell SHALL serve exactly the registered agent profile selected by its trusted deployment configuration. Provisioning MUST preserve the selected runtime target's profile through to the running cell. An unknown explicit profile MUST fail closed; public request fields MUST NOT override the configured selection. Omission of explicit selection MUST preserve the existing v1/v2 fallback. Profile selection MUST NOT enable Records lifecycle actions independently of their existing gate, and a profile exposing Records MUST require reader version 2.

#### Scenario: Provisioned v4 cell exposes its selected contract

- **WHEN** a cell is provisioned with a runtime target selecting `hosted-alpha-agent-v4`
- **THEN** its authenticated v4 contract route returns the canonical v4 contract and ordered command membership
- **AND** an authenticated request for an unselected profile is rejected

#### Scenario: Existing deployment omits explicit selection

- **WHEN** a deployment provides no explicit agent profile
- **THEN** the runtime retains its existing v1 selection with lifecycle actions disabled or v2 selection with lifecycle actions enabled

#### Scenario: Unsupported explicit selection is rejected

- **WHEN** trusted configuration explicitly names an unregistered profile or a Records profile with an incompatible reader
- **THEN** startup fails with a stable configuration error before registering agent routes

#### Scenario: Selection does not grant lifecycle mutations

- **WHEN** v3 or v4 is explicitly selected with Records lifecycle actions disabled
- **THEN** the selected profile's contract remains available
- **AND** lifecycle mutations remain rejected through the existing action gate
