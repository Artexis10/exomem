## MODIFIED Requirements

### Requirement: One multiplexed Records product command

The product surface SHALL expose one `record_memory` command rather than separate storage- or action-specific tools. Its finite selector SHALL contain nine actions: read-only `describe`, `validate`, `inspect`, and `query`, plus mutating `create`, `append`, `update`, `revise`, and `rebaseline`. Query SHALL cover bounded list/history/render/export-shaped responses through explicit arguments; generic derived-index repair SHALL remain under `maintain_memory`.

#### Scenario: Agent discovers before creating
- **WHEN** an agent needs to create a first Record collection without prior manifest knowledge
- **THEN** it uses `describe` and `validate` through the same `record_memory` front door before `create`

#### Scenario: Natural log intent uses one front door
- **WHEN** an agent receives “Log this training session”, “Record today’s symptoms”, “Update the car mileage”, or “Show the last three months”
- **THEN** bootstrap routes the intent through `record_memory` with the appropriate finite action rather than advertising a family of narrow tools

#### Scenario: Existing manifest uses the same front door
- **WHEN** an agent needs to validate, revise, or explicitly rebaseline an existing collection manifest
- **THEN** it uses the finite lifecycle actions on `record_memory` rather than a generic file editor or storage-specific tool

#### Scenario: Storage strategy is not a tool choice
- **WHEN** the selected collection uses a Markdown log, Markdown items, or a dataset
- **THEN** the same product command resolves the manifest and adapter without making the agent select a storage-specific public command

#### Scenario: Dataset mutation refuses through the same front door
- **WHEN** `append` or `update` resolves a dataset-backed collection
- **THEN** the command refuses that action as unsupported in this delivery and does not accept a caller-supplied replacement file

### Requirement: Records actions validate arguments explicitly

Each action SHALL define required and forbidden arguments. `describe` SHALL accept no collection or mutation arguments. Collection-less `inspect` SHALL inventory Records and targeted `inspect` SHALL remain report-only. Create-mode `validate` SHALL require `manifest_path` and `manifest_text`; revision-mode `validate` SHALL require `collection` and `manifest_text`; the two forms SHALL be mutually exclusive and read-only. `create` SHALL use create-only guards and SHALL NOT adopt or rewrite an existing tracker implicitly. `query` SHALL support bounded filters, `include_agent_history`, saved-view selection, and `output_format` without writing exports. `append` and `update` SHALL require structured item data and a concise reason; `update` SHALL additionally require the collection-scoped item key and current stale-write guards. `revise` SHALL require `collection`, complete `manifest_text`, `expected_manifest_hash`, `expected_container_hash`, and `why`. `rebaseline` SHALL require `collection`, `expected_manifest_hash`, `expected_container_hash`, `acknowledged_gap_codes`, and `why`; alternate lifecycle guard or acknowledgement argument names SHALL be refused.

Targeted `inspect` and revision-mode `validate` SHALL return lifecycle guards only as the closed object `{"expected_manifest_hash":"<sha256>","expected_container_hash":"<sha256>"}`. It SHALL contain exactly those two non-null SHA-256 values, or be omitted when the collection cannot be safely exposed.

#### Scenario: Read action rejects mutation payload
- **WHEN** `describe`, `inspect`, `validate`, or `query` receives an argument outside its declared shape
- **THEN** validation refuses the invocation rather than ignoring ambiguous input

#### Scenario: Validate needs no mutation rationale
- **WHEN** a client submits a manifest through either read-only `validate` form without `why`
- **THEN** argument validation accepts the read-only preflight
- **AND** supplying `why` to `validate` is refused as a cross-action argument

#### Scenario: Create does not silently adopt tracker
- **WHEN** `create` targets a path that already contains a tracker, manifest, or canonical source
- **THEN** create-only guards refuse and direct the user to add an explicit reviewed manifest instead

#### Scenario: Validate forms cannot be mixed
- **WHEN** `validate` receives both `manifest_path` and `collection`, or receives neither selector form
- **THEN** it refuses with actionable argument guidance and performs no mutation

#### Scenario: Revision guards are mandatory
- **WHEN** `revise` or `rebaseline` omits `expected_manifest_hash`, `expected_container_hash`, exact required `acknowledged_gap_codes`, or `why`
- **THEN** argument validation refuses before writer authority can publish canonical state

#### Scenario: Inspection returns a closed lifecycle guard object
- **WHEN** targeted `inspect` or revision-mode `validate` can safely expose a selected collection
- **THEN** it returns exactly `expected_manifest_hash` and `expected_container_hash` and no alternate guard aliases or audit fields

### Requirement: Records command parity and selector safety

`record_memory` SHALL be generated from the canonical command registry across MCP, REST, and CLI. `describe`, `validate`, `inspect`, and `query` SHALL be read-only at invocation classification; `create`, `append`, `update`, `revise`, and `rebaseline` SHALL enter writer authority, idempotency, terminal-response, governance-projector, and retry coverage. Unknown or unclassified actions SHALL fail closed at startup or invocation.

#### Scenario: Discovery and validation do not acquire writer authority
- **WHEN** `record_memory` runs `describe`, either `validate` form, collection-less or targeted `inspect`, or `query`
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Every mutation enters writer authority
- **WHEN** `record_memory` runs `create`, `append`, `update`, `revise`, or `rebaseline`
- **THEN** it uses the existing same-vault writer lease, idempotency, committed terminal envelope, and content-safe projector

#### Scenario: Mutations enter writer authority
- **WHEN** `record_memory` runs `create`, `append`, or `update`
- **THEN** it uses the existing same-vault writer lease, idempotency, and committed terminal envelope

#### Scenario: Unknown selector cannot bypass coverage
- **WHEN** an unregistered Records action reaches the command boundary
- **THEN** it is refused and cannot default to a read or mutation path without projector and receipt coverage

#### Scenario: Mixed command is advertised conservatively
- **WHEN** MCP exposes annotations for `record_memory`
- **THEN** the command-level annotation remains write-capable even though selector dispatch keeps its four read actions lease-free

## ADDED Requirements

### Requirement: Active client surfaces cannot advertise unavailable Records

Every MCP capability profile that reports Records as available in bootstrap SHALL export the same canonical `record_memory` command and finite action schema. A deliberately Records-disabled profile SHALL report Records unavailable and SHALL NOT teach an unusable route. Hosted-cell, personal-plugin, and local profiles SHALL NOT drift between bootstrap guidance and callable discovery.

#### Scenario: Hosted disposable cell exposes the advertised route
- **WHEN** the disposable hosted acceptance cell reports Records available
- **THEN** its MCP tool discovery exports `record_memory` with the current nine-action selector and the live lifecycle can call it

#### Scenario: Disabled profile is honest
- **WHEN** an operator profile intentionally excludes `record_memory`
- **THEN** bootstrap marks Records unavailable and omits any instruction that tells the agent to call it

### Requirement: Hosted lifecycle capability uses an additive profile

The disposable Hosted lifecycle surface SHALL use `hosted-alpha-agent-v2`, a separately versioned profile/candidate that retains `hosted-alpha-agent-v1` membership unchanged and adds the canonical nine-action `record_memory` surface. The v2 candidate package and additive deployment lock SHALL bind `minimum_records_reader_version: 2` before advertising `revise` or `rebaseline`. Existing v1 packages, clients, locks, and registered evidence SHALL remain valid and unchanged; v1 SHALL NOT be silently relabelled or mutated to advertise lifecycle selectors.

#### Scenario: Disposable lifecycle runs on v2
- **WHEN** the disposable hosted acceptance runner requests lifecycle capability
- **THEN** discovery reports `hosted-alpha-agent-v2`, `record_memory`, and the exact nine-action selector
- **AND** its candidate and deployment lock bind Records reader version 2

#### Scenario: Existing v1 client remains unchanged
- **WHEN** an existing v1 client connects while a v2 lifecycle candidate is pending or live
- **THEN** it remains bound to its unchanged v1 profile and compatibility identity
- **AND** it is neither required nor allowed to claim `revise` or `rebaseline` availability
