## MODIFIED Requirements

### Requirement: Honest precedence and defaults

Effective prominence SHALL resolve from a valid operator environment override, the current principal's stored value for the request's engagement context, the current principal's stored identity-wide value, existing explicit machine configuration, and finally the detected client default, in that order. The response SHALL identify the effective source, distinguishing a context value from an identity-wide value, the saved values, the request's context, scope and overriding operator value. A conflicting set under an operator override MUST be refused before writing. A clear under an operator override SHALL be applied and SHALL report the override, because removing a saved value cannot contradict the operator's level. Known hookless clients SHALL receive their existing Maximal default when no explicit selection exists; unknown clients SHALL retain the generic default. A stored preference record that cannot be read SHALL resolve to the generic default rather than the client default, SHALL report `preference:unavailable` as the source, SHALL keep the unavailable diagnostic visible, and SHALL withhold proactive capture while it applies, so an unreadable record never grants more proactivity than the user last chose. An explicit operator override or explicit machine configuration SHALL still take precedence over that floor, because each is a choice someone actually made.

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
- **THEN** the response reports Balanced from `preference:unavailable`, keeps the unavailable diagnostic, and grants no proactive capture in the served contract

#### Scenario: An explicit choice still outranks the unreadable-record floor
- **WHEN** the preference record cannot be read and the machine configuration names Light
- **THEN** the response reports Light from the configuration source rather than the floor

### Requirement: Consistent contract projection and guidance

Bootstrap, workflow effective capture and the delegation envelope SHALL use the same effective request preference. Bootstrap SHALL teach the inspect/set route that the served command set actually offers: the agent-accessible operation when it is served, otherwise the custom-instructions route, and never a command-line route. Changing prominence MUST NOT alter the four levels' existing meanings, compute policy or authority ceilings. Cross-client persistence SHALL be promised only for the same canonical identity and vault/state root; local-owner and OAuth identities SHALL remain distinct. Where the detected client is one that runs the standalone nudge hooks, bootstrap and every arm of the agent-accessible operation SHALL serve a hook-cadence statement naming what those hooks resolve from, that the saved preference is not among it, and the route that changes the cadence on the client machine; every other surface SHALL serve no such statement. The server MUST NOT claim that a saved level changes hook cadence, because the hooks read the client machine and the preference is held by the server.

#### Scenario: Saved Off disables proactive capture in every request projection
- **WHEN** a user selects Off and then resolves a workflow and bootstraps
- **THEN** both responses prohibit proactive capture while explicit user requests remain available

#### Scenario: Current conversation adopts the new contract
- **WHEN** an agent successfully sets a preference
- **THEN** the result contains the effective contract for immediate use and later bootstrap reports the same selection

#### Scenario: Hook-capable client is told what the hooks read
- **WHEN** a Claude Code or Codex client bootstraps, or inspects, sets or clears a preference through the agent-accessible operation
- **THEN** the engagement block states that the hooks resolve from the operator environment and that client machine's configuration file, that the saved preference does not reach them, and that the cadence changes with the command-line route run on the client machine

#### Scenario: A client without hooks is told nothing about hook cadence
- **WHEN** a hookless or unrecognized client bootstraps
- **THEN** the engagement block carries no hook-cadence statement

#### Scenario: Hookless surface is told a route it can take
- **WHEN** bootstrap is served through a descriptor that excludes the agent-accessible operation
- **THEN** the guidance names the custom-instructions route and does not name a command-line route
