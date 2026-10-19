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

## MODIFIED Requirements

### Requirement: Client context tunes eagerness only

The engagement context that applies to a request SHALL be derived only from the operator's explicit surface override, then, on an Exomem Cloud cell, the context the control plane's client registry assigns to the authenticated OAuth client, then the server-detected client surface. The Cloud gateway SHALL deliver the registry's context in the `x-exomem-client-context` header; a cloud cell SHALL honour only `coding` or `conversation` there and SHALL ignore any other value. A local install SHALL keep surface detection. Surfaces in the known hookless set SHALL map to the conversation context; Codex, Claude Code and unknown or undetected clients SHALL map to the coding context. The applied context MUST NOT be supplied by a request argument, and the context string MUST NOT reach principal resolution, audience derivation, storage path selection or authorization, and MUST NOT change any authority ceiling. A context value saved through one client SHALL apply to every client of the same identity, vault and context on its next request without a restart.

#### Scenario: Unknown client stays in the coding context
- **WHEN** a request presents no client name or an unrecognized client name, and no registry context applies
- **THEN** it resolves in the coding context and receives the generic default when nothing is saved

#### Scenario: The registry decides on a Cloud cell
- **WHEN** a request reaches a cloud cell with `x-exomem-client-context: conversation` and a client name that detection does not recognise
- **THEN** it resolves in the conversation context
- **AND** a request with `x-exomem-client-context: coding` resolves in the coding context whatever its client name

#### Scenario: A value outside the set is ignored
- **WHEN** a cloud cell receives a context header that is not `coding` or `conversation`
- **THEN** the header is ignored and surface detection decides, without an error

#### Scenario: A local install ignores the header
- **WHEN** a request to a server that is not a cloud cell carries the context header
- **THEN** surface detection decides

#### Scenario: A second client of the same identity observes the change
- **WHEN** one coding client saves Balanced for the coding context
- **THEN** a fresh connection from another coding client with the same identity and vault resolves Balanced, and a conversational client with the same identity is unaffected

#### Scenario: Context cannot widen authority
- **WHEN** a client presents any client name, surface or registry context
- **THEN** the delegation envelope and authorization ceilings are identical to those of the same identity under any other client name
