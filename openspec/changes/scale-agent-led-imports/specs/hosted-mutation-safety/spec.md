## MODIFIED Requirements

### Requirement: One Process-Safe Mutation Boundary Per Vault

The system SHALL serialize every operation that can modify a vault's canonical Markdown, media, governed indexes, logs, or mutation-owned runtime state through one process-safe boundary keyed by the vault's canonical identity. MCP, REST, CLI, transfer routes, and background workers MUST NOT maintain independent write locks or bypass that boundary.

A command that holds the boundary across several committed units MAY lend it between two units to a waiting holder in the same process that declares itself a borrower. Only the derived receipt proof and the derived component completion SHALL declare it, because they are brief and write no canonical content. A canonical write MUST NOT run on a borrowed boundary.

#### Scenario: Concurrent commands from different product surfaces

- **WHEN** MCP and REST submit write-capable commands against the same vault at the same time
- **THEN** at most one command executes its mutation section at a time
- **AND** both commands reach the same existing command leaves after acquiring the shared boundary

#### Scenario: Separate processes target the same vault

- **WHEN** two Exomem processes resolve different path spellings to the same canonical vault and attempt mutations concurrently
- **THEN** they contend on the same process-safe vault boundary
- **AND** they cannot both enter their mutation sections

#### Scenario: A derived proof runs between two units of an import

- **WHEN** an import holds the boundary across several chunks
- **AND** this process has measured a custody handoff earlier
- **AND** a derived receipt proof waits on it that would give up before the import ends
- **THEN** the import hands off the custody of its committed batches, advances the commit generation and lends the boundary between two chunks
- **AND** the proof runs and returns the boundary before the next chunk starts
- **AND** another process cannot enter its mutation section during the loan

#### Scenario: A loan that cannot serve its borrower in time is not made

- **WHEN** the measured custody handoff before a loan would end after the waiting proof gives up, or would take longer than the time left for the next chunk
- **THEN** the import does not hand off custody or lend the boundary at that point
- **AND** no lend offer stays open after the time left for the next chunk

#### Scenario: A holder that does not declare itself a borrower waits

- **WHEN** a background holder, such as a media scan, waits on the boundary of an import
- **THEN** it does not run between the import's chunks
- **AND** it enters after the import releases the boundary, or fails busy at its bound
