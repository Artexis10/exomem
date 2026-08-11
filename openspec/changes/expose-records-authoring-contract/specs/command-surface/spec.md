## MODIFIED Requirements

### Requirement: One multiplexed Records product command

The product surface SHALL expose one `record_memory` command rather than separate storage- or action-specific tools. Its finite selector SHALL contain seven actions: read-only `describe`, `validate`, and `inspect`, plus `create`, `query`, `append`, and `update`. Query SHALL cover bounded list/history/render/export-shaped responses through explicit arguments; generic derived-index repair SHALL remain under `maintain_memory`.

#### Scenario: Agent discovers before creating

- **WHEN** an agent needs to create a first Record collection without prior manifest knowledge
- **THEN** it uses `describe` and `validate` through the same `record_memory` front door before `create`

#### Scenario: Storage strategy is not a tool choice

- **WHEN** the selected collection uses a Markdown log, Markdown items, or a dataset
- **THEN** the same product command resolves the manifest and adapter without making the agent select a storage-specific public command

### Requirement: Records actions validate arguments explicitly

Each action SHALL define required and forbidden arguments. `describe` SHALL accept no collection or mutation arguments. Collection-less `inspect` SHALL inventory Records and targeted `inspect` SHALL remain report-only. `validate` SHALL require `manifest_path` and `manifest_text`, MAY accept `scaffold`, and SHALL reject `why` and every mutation argument. `create` SHALL use create-only guards and SHALL NOT adopt or rewrite an existing tracker implicitly. `query` SHALL support bounded filters, `include_agent_history`, saved-view selection, and `output_format` without writing exports. `append` and `update` SHALL require structured item data and a concise reason; `update` SHALL additionally require the collection-scoped item key and current stale-write guards.

#### Scenario: Read action rejects mutation payload

- **WHEN** `describe`, `inspect`, `validate`, or `query` receives an argument outside its declared shape
- **THEN** validation refuses the invocation rather than ignoring ambiguous input

#### Scenario: Validate needs no mutation rationale

- **WHEN** a client submits a manifest through `validate` without `why`
- **THEN** argument validation accepts the read-only preflight
- **AND** supplying `why` to `validate` is refused as a cross-action argument

### Requirement: Records command parity and selector safety

`record_memory` SHALL be generated from the canonical command registry across MCP, REST, and CLI. `describe`, `validate`, `inspect`, and `query` SHALL be read-only at invocation classification; mutating actions SHALL enter writer authority, idempotency, terminal-response, governance-projector, and retry coverage. Unknown or unclassified actions SHALL fail closed at startup or invocation.

#### Scenario: Discovery and validation do not acquire writer authority

- **WHEN** `record_memory` runs `describe`, `validate`, collection-less or targeted `inspect`, or `query`
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Mutations enter writer authority

- **WHEN** `record_memory` runs `create`, `append`, or `update`
- **THEN** it uses the existing same-vault writer lease, idempotency, and committed terminal envelope
