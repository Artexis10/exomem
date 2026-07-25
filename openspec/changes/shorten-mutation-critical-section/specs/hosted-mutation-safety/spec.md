## MODIFIED Requirements

### Requirement: One Process-Safe Mutation Boundary Per Vault

The system SHALL serialize every operation that can modify a vault's
canonical Markdown, media, governed indexes, logs, or mutation-owned runtime
state through one process-safe boundary keyed by the vault's canonical
identity. MCP, REST, CLI, transfer routes, and background workers MUST NOT
maintain independent write locks or bypass that boundary.

For a command on the narrowed-boundary set, the boundary the command holds
MUST cover only the commit seam — the canonical write, index/log updates,
and fencing — not corpus validation, relation-review evaluation, or
embedding-model loading, all of which MUST complete before the boundary is
acquired. A validation failure that occurs before the boundary is acquired
MUST raise without ever acquiring it. This narrowing MUST NOT change the
`MUTATION_BUSY` wire shape, the fencing guarantee at the atomic-write
boundary, or the outcome of any semantic-contract verdict; it only changes
when validation runs relative to lock acquisition. A command not on the
narrowed-boundary set, or with the wide-boundary escape hatch set, continues
to hold the boundary across its entire mutation section as before.

#### Scenario: Concurrent commands from different product surfaces

- **WHEN** MCP and REST submit write-capable commands against the same vault at the same time
- **THEN** at most one command executes its mutation section at a time
- **AND** both commands reach the same existing command leaves after acquiring the shared boundary

#### Scenario: Separate processes target the same vault

- **WHEN** two Exomem processes resolve different path spellings to the same canonical vault and attempt mutations concurrently
- **THEN** they contend on the same process-safe vault boundary
- **AND** they cannot both enter their mutation sections

#### Scenario: A narrowed command holds the boundary only around its commit

- **WHEN** a command on the narrowed-boundary set (for example `remember`) runs corpus validation and a possible embedding-model load ahead of committing
- **THEN** the mutation boundary is acquired only immediately before the commit seam (canonical write, index/log updates, fencing)
- **AND** the validation and any model load already completed before acquisition are not repeated inside the held boundary

#### Scenario: Pre-boundary validation fails before any lock is touched

- **WHEN** pre-commit semantic-contract validation for a narrowed command raises a blocking error
- **THEN** the error is returned to the caller without the mutation boundary ever having been acquired
- **AND** the vault is unchanged, exactly as if the boundary had been acquired and immediately released
