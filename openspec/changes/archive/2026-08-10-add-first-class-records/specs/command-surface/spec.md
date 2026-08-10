## ADDED Requirements

### Requirement: One multiplexed Records product command
The product surface SHALL expose one `record_memory` command rather than separate storage- or action-specific tools. Its finite selector SHALL contain exactly five first-delivery actions: `inspect`, `create`, `query`, `append`, and `update`. Query SHALL cover bounded list/history/render/export-shaped responses through explicit arguments; generic derived-index repair SHALL remain under `maintain_memory`.

#### Scenario: Natural log intent uses one front door
- **WHEN** an agent receives “Log this training session”, “Record today’s symptoms”, “Update the car mileage”, or “Show the last three months”
- **THEN** bootstrap routes the intent through `record_memory` with the appropriate finite action rather than advertising a family of narrow tools

#### Scenario: Storage strategy is not a tool choice
- **WHEN** the selected collection uses a Markdown log, Markdown items, or a dataset
- **THEN** the same product command resolves the manifest and adapter without making the agent select a storage-specific public command

#### Scenario: Dataset mutation refuses through the same front door
- **WHEN** `append` or `update` resolves a dataset-backed collection
- **THEN** the command refuses that action as unsupported in this delivery and does not accept a caller-supplied replacement file

### Requirement: Records actions validate arguments explicitly
Each action SHALL define required and forbidden arguments. `create` SHALL use create-only guards and SHALL NOT adopt or rewrite an existing tracker implicitly. `inspect` SHALL be report-only. `query` SHALL support bounded filters, `include_agent_history`, saved-view selection, and `output_format` without writing exports. `append` and `update` SHALL require structured item data and a concise reason; `update` SHALL additionally require the collection-scoped item key and current stale-write guards.

#### Scenario: Read action rejects mutation payload
- **WHEN** `inspect` or `query` receives item changes, a mutation reason, or another write-only argument
- **THEN** validation refuses the invocation rather than ignoring ambiguous input

#### Scenario: Create does not silently adopt tracker
- **WHEN** `create` targets a path that already contains a tracker, manifest, or canonical source
- **THEN** create-only guards refuse and direct the user to add an explicit reviewed manifest instead

### Requirement: Records command parity and selector safety
`record_memory` SHALL be generated from the canonical command registry across MCP, REST, and CLI. Read-only actions SHALL remain read-only at invocation classification; mutating actions SHALL enter writer authority, idempotency, terminal-response, governance-projector, and retry coverage. Unknown or unclassified actions SHALL fail closed at startup or invocation.

#### Scenario: Query does not acquire writer authority
- **WHEN** `record_memory` runs `query` or `inspect`
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Append enters writer authority
- **WHEN** `record_memory` runs `create`, `append`, or `update`
- **THEN** it uses the existing same-vault writer lease, idempotency, and committed terminal envelope

#### Scenario: Unknown selector cannot bypass coverage
- **WHEN** an unregistered Records action reaches the command boundary
- **THEN** it is refused and cannot default to a read or mutation path without projector and receipt coverage

#### Scenario: Mixed command is advertised conservatively
- **WHEN** MCP exposes annotations for `record_memory`
- **THEN** the command-level annotation remains write-capable even though selector dispatch keeps `inspect` and `query` lease-free
