## ADDED Requirements

### Requirement: One multiplexed Planning product command
The product surface SHALL expose one `plan_memory` command rather than separate capture, horizon, hierarchy, or storage-specific tools. Its finite selector SHALL contain exactly six first-delivery actions: `inspect`, `create`, `query`, `add`, `update`, and `triage`. Query SHALL cover bounded horizon/date/history/hierarchy/render/export-shaped responses through explicit arguments; generic derived-index repair SHALL remain under `maintain_memory`.

#### Scenario: Natural planning intent uses one front door
- **WHEN** an agent receives “save this feature idea”, “file this bug for later”, “make this a quarterly initiative”, “what matters this week”, or “show my multi-year outcomes”
- **THEN** bootstrap routes the intent through `plan_memory` with the appropriate finite action instead of advertising a family of narrow tools

#### Scenario: Planning storage is not a tool choice
- **WHEN** an agent captures or queries Planning intent
- **THEN** the same command resolves the Planning collection and first-delivery Markdown-item adapter without asking the user to select an internal storage operation

#### Scenario: Review does not hide inside query
- **WHEN** a caller queries a plan that carries Records evidence descriptors
- **THEN** `plan_memory` returns authored Planning state and descriptors without evaluating planned-versus-recorded progress or silently invoking epistemic `review_memory`

### Requirement: Planning actions validate arguments explicitly
The generated signature SHALL expose exactly `action`, `collection`, `manifest_path`, `manifest_text`, `why`, `scaffold`, `view`, `filters`, `columns`, `sort_by`, `descending`, `limit`, `aggregate`, `date_from`, `date_to`, `date_column`, `lifecycle`, `hierarchy_mode`, `hierarchy_depth`, `hierarchy_limit`, `continuation`, `include_agent_history`, `output_format`, `item`, `plan_id`, `expected_container_hash`, `body`, `changes`, `transition`, and `expected_item_version`. `action` SHALL be required and select the following exact matrix; every non-listed argument SHALL be forbidden rather than ignored:

| Action | Required | Optional and defaults |
| --- | --- | --- |
| `inspect` | `collection: string` | none |
| `create` | `manifest_path: string`, `manifest_text: string`, `why: string` | `scaffold: boolean=true` |
| `query` | `collection: string` | `view: string`; existing structured `filters`, `columns`, `sort_by`, `aggregate`, `date_from`, `date_to`, `date_column`; `descending: boolean=false`; `limit: integer=100` capped at 1,000; `lifecycle: active|archived|all=active`; `hierarchy_mode: none|ancestors|descendants=none`; `hierarchy_depth: integer=3` capped at 8; `hierarchy_limit: integer=100` capped at 500; `continuation: string`; `include_agent_history: boolean=false`; `output_format: json|markdown|csv=json` |
| `add` | `collection: string`, `item: object`, `why: string` | `plan_id: UUID`, `expected_container_hash: sha256`, `body: string=""` |
| `update` | `collection: string`, `plan_id: UUID`, `expected_container_hash: sha256`, `expected_item_version: sha256`, `why: string`, at least one of `changes` or `body` | `changes: non-empty object` using the Planning spec's exact null-as-delete rules; `body: complete string replacement` |
| `triage` | `collection: string`, `plan_id: UUID`, `transition: non-empty object`, `expected_container_hash: sha256`, `expected_item_version: sha256`, `why: string` | none |

Saved view SHALL exclude inline filter/projection/sort/date/aggregate/lifecycle shaping, but MAY combine with hierarchy, continuation, history, and output controls. Hierarchy SHALL be forbidden with aggregate or CSV output. `transition` SHALL contain only `kind`, `status`, `priority`, `commitment`, `horizon`, `area`, or `parent`; only `area` and `parent` may be null, and kind changes stay among outcome/initiative/work-item. `update` SHALL reject `transition`; `triage` SHALL reject area source items, item, changes, lifecycle, body, health, dates, tags, evidence, execution, and domain-field convenience arguments. `why` SHALL be non-empty single-line text capped at 512 UTF-8 bytes. No action SHALL ignore explicit false, empty, or zero values before validation.

#### Scenario: Read action rejects mutation payload
- **WHEN** `inspect` or `query` receives item changes, transition fields, a mutation reason, or another write-only argument
- **THEN** validation refuses rather than ignoring the ambiguous payload

#### Scenario: Create refuses existing canonical files
- **WHEN** the requested manifest target or its declared canonical source already exists, including an ordinary note at either target
- **THEN** create-only guards refuse and do not adopt, overwrite, or relocate that content while unrelated sibling files remain out of scope

#### Scenario: Update and triage remain distinct
- **WHEN** `triage` receives an arbitrary body replacement or `update` receives triage-only convenience fields in the wrong shape
- **THEN** argument validation refuses instead of routing by best effort

#### Scenario: Complete body update is guarded
- **WHEN** `update` supplies a complete replacement body with exact current container and item hashes
- **THEN** the same final-state validation, writer serialization, guarded publication, audit, and stale refusal apply as for property changes

#### Scenario: Saved view and hierarchy compose predictably
- **WHEN** `query` supplies a declared saved view plus bounded hierarchy controls
- **THEN** the saved view owns row shaping, hierarchy expands only the authorized returned page, and supplying any inline shaping field refuses

### Requirement: Planning command parity and selector safety
`plan_memory` SHALL have one Python leaf/signature and SHALL be registered consistently in the repository's canonical command and product metadata registries so MCP, REST, CLI, OpenAPI, capability documentation, and schema-fidelity fixtures are generated from that implementation. Read-only actions SHALL remain read-only at invocation classification; mutating actions SHALL enter writer authority, idempotency, terminal-response, governance-projector, and retry coverage. Unknown or unclassified actions SHALL fail closed at startup or invocation.

#### Scenario: Query does not acquire writer authority
- **WHEN** `plan_memory` runs `query` or `inspect`
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Planning mutation enters writer authority
- **WHEN** `plan_memory` runs `create`, `add`, `update`, or `triage`
- **THEN** it uses the existing same-vault writer lease, idempotency, committed terminal envelope, and retry identity

#### Scenario: Unknown selector cannot bypass coverage
- **WHEN** an unregistered Planning action reaches the command boundary
- **THEN** it is refused and cannot default to a read or mutation path without projector and receipt coverage

#### Scenario: Mixed command is advertised conservatively
- **WHEN** MCP exposes annotations for `plan_memory`
- **THEN** the command-level annotation remains write-capable even though selector dispatch keeps `inspect` and `query` lease-free

#### Scenario: Generated surfaces stay identical
- **WHEN** the Planning command schema is inspected through MCP, REST, CLI, OpenAPI, and generated capability artifacts
- **THEN** all surfaces expose the same selector and parameter semantics from one leaf/signature with no hand-maintained duplicate command implementation

#### Scenario: Planning application errors stay on the shared facade contract
- **WHEN** the Planning leaf raises a deliberate public `OpError`
- **THEN** MCP returns a normal `{success: false, error: {code, message, remediation}}` tool result and REST plus CLI `--json` return the identical shared envelope
- **AND** unexpected exceptions retain the existing native or internal-error path instead of being projected as Planning refusals
