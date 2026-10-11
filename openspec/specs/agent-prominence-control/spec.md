# agent-prominence-control Specification

## Purpose
Allow an authenticated user to select Exomem engagement through an agent and recover that choice on subsequent requests without affecting other users or vaults.

## Requirements

### Requirement: Agent-accessible prominence selection

The system SHALL expose one canonical inspect/set/clear operation through MCP, REST and CLI for Off, Light, Balanced and Maximal. Inspect SHALL be read-only and SHALL report the identity-wide value, each saved context value, the request's detected context and the effective level. Set SHALL return the resulting effective contract and durable revision, and require the revision returned by a prior inspect. Set without a context SHALL write the identity-wide value exactly as before; set with a context SHALL write only that context. Clear SHALL require a context and the inspected revision and SHALL remove only that context value.

#### Scenario: Each level survives a new request
- **WHEN** an authenticated user inspects and sets any supported level
- **THEN** a subsequent bootstrap for the same principal and vault reports that level and its contract without a service restart

#### Scenario: A stale update cannot overwrite a later choice
- **WHEN** a set uses a revision older than the currently stored preference
- **THEN** it is refused without changing the preference and asks the caller to inspect again

#### Scenario: Invalid selection has no side effects
- **WHEN** a caller supplies an unsupported level, an unsupported context or invalid action arguments
- **THEN** the operation reports an input error without writing preference state

#### Scenario: A context value is set and cleared under one revision
- **WHEN** an authenticated user sets Balanced for the coding context with the inspected revision and later clears the coding context with the new revision
- **THEN** the identity-wide value is untouched by both operations, the clear leaves no coding value and clearing an absent context reports no mutation

#### Scenario: An existing client keeps its three-argument set
- **WHEN** a client sets a level with only the action, level and expected revision
- **THEN** the identity-wide value changes and no context value is created or changed

### Requirement: Preference identity and vault isolation

The preference SHALL be scoped to the server-bound vault and verified principal, and SHALL hold one identity-wide value plus at most one value per engagement context in a single record under a single durable revision. Request arguments MUST NOT select another principal, owner identity or storage path; an argument MAY name which context value to write, but the context that applies to a request is derived only from the detected surface. Unresolved identities MUST NOT inspect or mutate preference state. A change MUST NOT modify other principals, other vaults, compute modes or governance/delegation ceilings. A record written by an earlier schema SHALL read as its identity-wide value with no context values and SHALL be upgraded in place by the next successful write.

#### Scenario: Two users and two vaults remain independent
- **WHEN** one principal selects Maximal in one vault
- **THEN** another principal in that vault and the same principal in another vault retain their own effective preferences

#### Scenario: Missing identity is refused
- **WHEN** an unresolved request attempts preference inspection or mutation
- **THEN** it receives a content-free identity error and no preference file is created

#### Scenario: An earlier record is upgraded without loss
- **WHEN** a record written by the previous schema holds Maximal and the user sets Balanced for the coding context
- **THEN** the record reports Maximal identity-wide and Balanced for coding under a new revision, and the previous value is never lost

#### Scenario: A previous reader degrades without erasing
- **WHEN** a service running the previous schema reads a record written by this schema
- **THEN** it reports the preference as unavailable, resolves from configuration or defaults and neither rewrites nor deletes the record

### Requirement: Honest precedence and defaults

Effective prominence SHALL resolve from a valid operator environment override, the current principal's stored value for the request's engagement context, the current principal's stored identity-wide value, existing explicit machine configuration, and finally the detected client default, in that order. The response SHALL identify the effective source, distinguishing a context value from an identity-wide value, the saved values, the request's context, scope and overriding operator value. A conflicting set under an operator override MUST be refused before writing. A clear under an operator override SHALL be applied and SHALL report the override, because removing a saved value cannot contradict the operator's level. Known hookless clients SHALL receive their existing Maximal default when no explicit selection exists; unknown clients SHALL retain the generic default. A stored preference record that cannot be read SHALL resolve to the generic default rather than the client default, SHALL report `preference:unavailable` as the source, SHALL keep the unavailable diagnostic visible, and SHALL withhold proactive capture in every projection of capture authority while it applies -- the capture gate, the delegation envelope's proactive capture class, and the workflow contract's effective capture -- so an unreadable record never grants more proactivity than the user last chose. An explicit operator override or explicit machine configuration SHALL still take precedence over that floor, because each is a choice someone actually made.

#### Scenario: Operator override remains authoritative
- **WHEN** the operator pins Light and an agent requests Maximal
- **THEN** the operation reports that the override prevents the change, leaves storage untouched and continues reporting Light

#### Scenario: A clear proceeds under an operator override
- **WHEN** the operator pins Light and an agent clears a saved coding context value
- **THEN** the value is removed, the identity-wide value is untouched, and the response reports Light from the operator override

#### Scenario: Known web client has no stored selection
- **WHEN** a known hookless client bootstraps without an explicit preference
- **THEN** the response reports Maximal from its surface default

#### Scenario: Context value beats identity-wide value
- **WHEN** a principal has saved Maximal identity-wide and Balanced for the coding context
- **THEN** a coding client resolves Balanced with a context source and a conversational client resolves Maximal with the identity-wide source

#### Scenario: Unreadable preference does not fail open
- **WHEN** a principal who saved Off has a preference record that cannot be read and bootstraps from a hookless client
- **THEN** the response reports Balanced from `preference:unavailable`, keeps the unavailable diagnostic, and does not grant proactive capture

#### Scenario: An explicit choice still outranks the unreadable-record floor
- **WHEN** the preference record cannot be read and the machine configuration names Light
- **THEN** the response reports Light from the configuration source rather than the floor

### Requirement: Consistent contract projection and guidance

Bootstrap, workflow effective capture and the delegation envelope SHALL use the same effective request preference, and SHALL derive every statement of capture authority from the same resolved gate rather than from the level alone, so the served contract and the delegation envelope can never disagree about whether this agent may write unasked. Bootstrap SHALL teach the inspect/set route that the served command set actually offers: the agent-accessible operation when it is served, otherwise the custom-instructions route, and never a command-line route. Changing prominence MUST NOT alter the four levels' existing meanings, compute policy or authority ceilings. Cross-client persistence SHALL be promised only for the same canonical identity and vault/state root; local-owner and OAuth identities SHALL remain distinct. Where the detected client resolves to the coding context, and therefore may run the standalone nudge hooks, bootstrap and every arm of the agent-accessible operation SHALL serve a hook-cadence statement naming what those hooks resolve from, that the saved preference is not among it, and the route that changes the cadence on the client machine; every other surface SHALL serve no such statement. The server MUST NOT claim that a saved level changes hook cadence, because the hooks read the client machine and the preference is held by the server.

#### Scenario: Saved Off disables proactive capture in every request projection
- **WHEN** a user selects Off and then resolves a workflow and bootstraps
- **THEN** both responses prohibit proactive capture while explicit user requests remain available

#### Scenario: The served contract and the envelope agree about proactive capture
- **WHEN** any engagement block is served, at any level and under the unreadable-record floor
- **THEN** the contract's effective capture and the delegation envelope's proactive capture class state the same permission

#### Scenario: Current conversation adopts the new contract
- **WHEN** an agent successfully sets a preference
- **THEN** the result contains the effective contract for immediate use and later bootstrap reports the same selection

#### Scenario: Hook-capable client is told what the hooks read
- **WHEN** a client in the coding context bootstraps, or inspects, sets or clears a preference through the agent-accessible operation
- **THEN** the engagement block states that the hooks resolve from the operator environment and that client machine's configuration file, that the saved preference does not reach them, and that the command-line route run on the client machine changes the hook cadence alone while a saved preference still decides what is served

#### Scenario: A client without hooks is told nothing about hook cadence
- **WHEN** a client in the conversation context bootstraps
- **THEN** the engagement block carries no hook-cadence statement

#### Scenario: Hookless surface is told a route it can take
- **WHEN** bootstrap is served through a descriptor that excludes the agent-accessible operation
- **THEN** the guidance names the custom-instructions route and does not name a command-line route

### Requirement: Client context tunes eagerness only

The engagement context that applies to a request SHALL be derived only from the server-detected client surface or the operator's explicit surface override. Surfaces in the known hookless set SHALL map to the conversation context; Codex, Claude Code and unknown or undetected clients SHALL map to the coding context. The applied context MUST NOT be supplied by a request argument, and the context string MUST NOT reach principal resolution, audience derivation, storage path selection or authorization, and MUST NOT change any authority ceiling. A context value saved through one client SHALL apply to every client of the same identity, vault and context on its next request without a restart.

#### Scenario: Unknown client stays in the coding context
- **WHEN** a request presents no client name or an unrecognized client name
- **THEN** it resolves in the coding context and receives the generic default when nothing is saved

#### Scenario: A second client of the same identity observes the change
- **WHEN** one coding client saves Balanced for the coding context
- **THEN** a fresh connection from another coding client with the same identity and vault resolves Balanced, and a conversational client with the same identity is unaffected

#### Scenario: Context cannot widen authority
- **WHEN** a client presents any client name or surface
- **THEN** the delegation envelope and authorization ceilings are identical to those of the same identity under any other client name
