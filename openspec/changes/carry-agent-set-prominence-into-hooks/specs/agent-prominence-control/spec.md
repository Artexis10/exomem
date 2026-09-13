## MODIFIED Requirements

### Requirement: Honest precedence and defaults

Effective prominence SHALL resolve from a valid operator environment override, the current principal's stored value for the request's engagement context, the current principal's stored identity-wide value, existing explicit machine configuration, and finally the detected client default, in that order. The response SHALL identify the effective source, distinguishing a context value from an identity-wide value, the saved values, the request's context, scope and overriding operator value. A conflicting set under an operator override MUST be refused before writing. A clear under an operator override SHALL be applied and SHALL report the override, because removing a saved value cannot contradict the operator's level. Known hookless clients SHALL receive their existing Maximal default when no explicit selection exists; unknown clients SHALL retain the generic default. A stored preference record that cannot be read SHALL resolve to the generic default rather than the client default, SHALL report `preference:unavailable` as the source, and SHALL keep the unavailable diagnostic visible, so an unreadable record never grants more proactivity than the user last chose.

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

### Requirement: Consistent contract projection and guidance

Bootstrap, workflow effective capture and the delegation envelope SHALL use the same effective request preference. Bootstrap SHALL teach the inspect/set route that the served command set actually offers: the agent-accessible operation when it is served, otherwise the custom-instructions route, and never a command-line route. Changing prominence MUST NOT alter the four levels' existing meanings, compute policy or authority ceilings. Cross-client persistence SHALL be promised only for the same canonical identity and vault/state root; local-owner and OAuth identities SHALL remain distinct. A successful set or clear by the local-owner identity SHALL mirror the level that identity now resolves for coding clients into the shared machine configuration the standalone nudge hooks read, preserving every other key in that file, and the result SHALL report the mirror, so a saved level changes hook cadence as well as served prose.

#### Scenario: Saved Off disables proactive capture in every request projection
- **WHEN** a user selects Off and then resolves a workflow and bootstraps
- **THEN** both responses prohibit proactive capture while explicit user requests remain available

#### Scenario: Current conversation adopts the new contract
- **WHEN** an agent successfully sets a preference
- **THEN** the result contains the effective contract for immediate use and later bootstrap reports the same selection

#### Scenario: Saved Off silences the hooks
- **WHEN** the local-owner identity saves Off through the agent-accessible operation on a hook-capable client
- **THEN** the next run of the capture nudge and of the retrieve nudge each resolve Off from the shared machine configuration and emit nothing, and the compute-mode key in that file is unchanged

#### Scenario: Hookless surface is told a route it can take
- **WHEN** bootstrap is served through a descriptor that excludes the agent-accessible operation
- **THEN** the guidance names the custom-instructions route and does not name a command-line route
