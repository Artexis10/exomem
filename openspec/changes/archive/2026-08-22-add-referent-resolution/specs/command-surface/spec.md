# command-surface

## ADDED Requirements

### Requirement: Referents is an additive shared envelope key
Eligible cue queries SHALL expose the same optional `referents` block through the existing find and ask-memory leaf on MCP, CLI, and REST without adding an MCP, CLI, or REST parameter.

#### Scenario: Compact and full detail
- **WHEN** the same cue query is requested with compact and full hit detail
- **THEN** the envelope-level referents block is identical
