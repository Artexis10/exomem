## ADDED Requirements

### Requirement: An agent can name a recording's speakers after the fact
A governed operation SHALL take one recording and a mapping from its current speaker labels to names, and SHALL rewrite that recording's transcript labels through the governed writer. When the recording's per-label voice embedding is available and the owner asks, it SHALL also enrol or update a voice profile with that embedding under the existing voice-profile rules, so later recordings are named automatically.

#### Scenario: The person tells the agent who spoke
- **WHEN** the owner tells an agent that Speaker B is the bride, named Mari, and the agent calls the operation
- **THEN** every "Speaker B" turn of that recording reads "Mari", and other recordings are unchanged

#### Scenario: Keep the voice
- **WHEN** the owner asks to keep the voice and the recording's embedding for that label is available
- **THEN** a voice profile for that name is enrolled or updated, and a later recording with that voice is named automatically

#### Scenario: No embedding available
- **WHEN** the owner asks to keep the voice for a recording without a stored embedding
- **THEN** the labels are renamed and the operation reports that no voice profile was made
