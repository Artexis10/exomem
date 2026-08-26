## MODIFIED Requirements

### Requirement: One multiplexed Planning product command
The product surface SHALL expose one `plan_memory` command rather than separate capture, horizon, hierarchy, manifest-lifecycle, or storage-specific tools. Its finite selector SHALL contain exactly nine actions: read-only `inspect`, `validate`, and `query`, plus mutating `create`, `add`, `update`, `triage`, `revise`, and `rebaseline`. Query SHALL cover bounded horizon/date/history/hierarchy/render/export-shaped responses through explicit arguments; generic derived-index repair and previewed structured-file migration SHALL remain under `maintain_memory`.

#### Scenario: Natural planning intent uses one front door
- **WHEN** an agent receives “save this feature idea”, “file this bug for later”, “make this a quarterly initiative”, “what matters this week”, “show my multi-year outcomes”, or “revise this Planning collection”
- **THEN** bootstrap routes the intent through `plan_memory` with the appropriate finite action instead of advertising a family of narrow tools

#### Scenario: Planning storage is not a tool choice
- **WHEN** an agent captures, queries, validates, or revises Planning intent
- **THEN** the same command resolves the Planning collection and Markdown-item adapter without asking the user to select an internal storage operation

#### Scenario: Existing manifest uses the same front door
- **WHEN** an agent needs to validate, revise, or explicitly rebaseline an existing Planning collection manifest
- **THEN** it uses the finite lifecycle actions on `plan_memory` rather than a generic file editor or storage-specific tool

#### Scenario: Review does not hide inside query
- **WHEN** a caller queries a plan that carries Records evidence descriptors
- **THEN** `plan_memory` returns authored Planning state and descriptors without evaluating planned-versus-recorded progress or silently invoking epistemic `review_memory`

### Requirement: Planning actions validate arguments explicitly
The generated signature SHALL expose exactly `action`, `collection`, `manifest_path`, `manifest_text`, `why`, `scaffold`, `view`, `filters`, `columns`, `sort_by`, `descending`, `limit`, `aggregate`, `date_from`, `date_to`, `date_column`, `lifecycle`, `hierarchy_mode`, `hierarchy_depth`, `hierarchy_limit`, `continuation`, `include_agent_history`, `output_format`, `item`, `plan_id`, `expected_manifest_hash`, `expected_container_hash`, `acknowledged_gap_codes`, `body`, `changes`, `transition`, and `expected_item_version`. `action` SHALL be required and select the following exact matrix; every non-listed argument SHALL be forbidden rather than ignored:

| Action | Required | Optional and defaults |
| --- | --- | --- |
| `inspect` | `collection: string` | none |
| `validate` | create mode: `manifest_path: string`, `manifest_text: string`; revision mode: `collection: string`, `manifest_text: string` | none |
| `create` | `manifest_path: string`, `manifest_text: string`, `why: string` | `scaffold: boolean=true` |
| `query` | `collection: string` | `view: string`; existing structured `filters`, `columns`, `sort_by`, `aggregate`, `date_from`, `date_to`, `date_column`; `descending: boolean=false`; `limit: integer=100` capped at 1,000; `lifecycle: active|archived|all=active`; `hierarchy_mode: none|ancestors|descendants=none`; `hierarchy_depth: integer=3` capped at 8; `hierarchy_limit: integer=100` capped at 500; `continuation: string`; `include_agent_history: boolean=false`; `output_format: json|markdown|csv=json` |
| `add` | `collection: string`, `item: object`, `why: string` | `plan_id: UUID`, `expected_container_hash: sha256`, `body: string=""` |
| `update` | `collection: string`, `plan_id: UUID`, `expected_container_hash: sha256`, `expected_item_version: sha256`, `why: string`, at least one of `changes` or `body` | `changes: non-empty object` using the Planning spec's exact null-as-delete rules; `body: complete string replacement` |
| `triage` | `collection: string`, `plan_id: UUID`, `transition: non-empty object`, `expected_container_hash: sha256`, `expected_item_version: sha256`, `why: string` | none |
| `revise` | `collection: string`, `manifest_text: string`, `expected_manifest_hash: sha256`, `expected_container_hash: sha256`, `why: string` | none |
| `rebaseline` | `collection: string`, `expected_manifest_hash: sha256`, `expected_container_hash: sha256`, `acknowledged_gap_codes: non-empty array[string]`, `why: string` | none |

The two `validate` forms SHALL be mutually exclusive and read-only. Revision-mode `validate` SHALL return lifecycle guards only as the closed object `{"expected_manifest_hash":"<sha256>","expected_container_hash":"<sha256>"}` when the collection can be safely exposed. Saved view SHALL exclude inline filter/projection/sort/date/aggregate/lifecycle shaping, but MAY combine with hierarchy, continuation, history, and output controls. Hierarchy SHALL be forbidden with aggregate or CSV output. `transition` SHALL contain only `kind`, `status`, `priority`, `commitment`, `horizon`, `area`, or `parent`; only `area` and `parent` may be null, and kind changes stay among outcome/initiative/work-item. `update` SHALL reject `transition`; `triage` SHALL reject area source items, item, changes, lifecycle, body, health, dates, tags, evidence, execution, and domain-field convenience arguments. `why` SHALL be non-empty single-line text capped at 512 UTF-8 bytes. No action SHALL ignore explicit false, empty, or zero values before validation.

#### Scenario: Read action rejects mutation payload
- **WHEN** `inspect`, `validate`, or `query` receives an argument outside its declared shape
- **THEN** validation refuses rather than ignoring the ambiguous payload

#### Scenario: Create refuses existing canonical files
- **WHEN** the requested manifest target or its declared canonical source already exists, including an ordinary note at either target
- **THEN** create-only guards refuse and do not adopt, overwrite, or relocate that content while unrelated sibling files remain out of scope

#### Scenario: Validate forms cannot be mixed
- **WHEN** `validate` receives both `manifest_path` and `collection`, or receives neither selector form
- **THEN** it refuses with actionable argument guidance and performs no mutation

#### Scenario: Revision guards are mandatory
- **WHEN** `revise` or `rebaseline` omits an expected manifest hash, expected container hash, exact required gap acknowledgements, or reason
- **THEN** argument validation refuses before writer authority can publish canonical state

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
`plan_memory` SHALL have one Python leaf/signature and SHALL be registered consistently in the repository's canonical command and product metadata registries so MCP, REST, CLI, OpenAPI, capability documentation, and schema-fidelity fixtures are generated from that implementation. `inspect`, `validate`, and `query` SHALL remain read-only at invocation classification; `create`, `add`, `update`, `triage`, `revise`, and `rebaseline` SHALL enter writer authority, idempotency, terminal-response, governance-projector, and retry coverage. Unknown or unclassified actions SHALL fail closed at startup or invocation.

#### Scenario: Planning reads do not acquire writer authority
- **WHEN** `plan_memory` runs `inspect`, either `validate` form, or `query`
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Query does not acquire writer authority
- **WHEN** `plan_memory` runs `query` or `inspect`
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Planning mutation enters writer authority
- **WHEN** `plan_memory` runs `create`, `add`, `update`, `triage`, `revise`, or `rebaseline`
- **THEN** it uses the existing same-vault writer lease, idempotency, committed terminal envelope, governance projector, and retry identity

#### Scenario: Unknown selector cannot bypass coverage
- **WHEN** an unregistered Planning action reaches the command boundary
- **THEN** it is refused and cannot default to a read or mutation path without projector and receipt coverage

#### Scenario: Mixed command is advertised conservatively
- **WHEN** MCP exposes annotations for `plan_memory`
- **THEN** the command-level annotation remains write-capable even though selector dispatch keeps `inspect`, `validate`, and `query` lease-free

#### Scenario: Generated surfaces stay identical
- **WHEN** the Planning command schema is inspected through MCP, REST, CLI, OpenAPI, and generated capability artifacts
- **THEN** all surfaces expose the same selector and parameter semantics from one leaf/signature with no hand-maintained duplicate command implementation

#### Scenario: Planning application errors stay on the shared facade contract
- **WHEN** the Planning leaf raises a deliberate public `OpError`
- **THEN** MCP returns a normal `{success: false, error: {code, message, remediation}}` tool result and REST plus CLI `--json` return the identical shared envelope
- **AND** unexpected exceptions retain the existing native or internal-error path instead of being projected as Planning refusals

### Requirement: The generic Records command exposes exact child expansion and presentation refresh

The existing `record_memory` command SHALL keep one finite product surface and SHALL expose `expand_child` only to query plus `refresh_presentation` only to update. Query SHALL accept either an explicit child-field string or the backward-compatible boolean selector under their declared compatibility rules. Update SHALL accept `refresh_presentation: true` with normal changes or as the sole semantic request, but SHALL refuse false/no-op refresh, refresh on a collection without a valid presentation recipe, and all use outside update. MCP, CLI, REST, action allowlists, saved views, bootstrap guidance, schema fixtures, and generated contracts SHALL expose the same argument names and behavior. Collection-wide readable-path and presentation migration SHALL use the profile-neutral `maintain_memory(mode="structured-files")` surface rather than adding another Records command action or Records-specific renderer.

#### Scenario: Explicit child selector is discoverable everywhere
- **WHEN** a client inspects the public Records schema or calls query over MCP, CLI, or REST
- **THEN** `expand_child` has the same bounded string contract and reaches the same governed query leaf on every surface

#### Scenario: Presentation repair does not add another tool
- **WHEN** a caller needs to backfill a readable body for an existing item
- **THEN** it uses guarded `record_memory(action="update", refresh_presentation=true, ...)` and no separate renderer, migration, or YAML tool is added

#### Scenario: Presentation repair does not add another Records tool
- **WHEN** a caller needs to backfill a readable body for one existing item
- **THEN** it uses guarded `record_memory(action="update", refresh_presentation=true, ...)`, while collection-wide migration uses the shared maintenance mode and neither path exposes a YAML editor

#### Scenario: Selector leakage is refused
- **WHEN** `expand_child` is supplied to a non-query action or `refresh_presentation` is supplied to a non-update action
- **THEN** the command rejects the request as invalid arguments before opening collection or item contents

#### Scenario: Collection migration stays profile-neutral
- **WHEN** a caller previews or applies filenames and presentation across a Records collection
- **THEN** the registry routes it through `maintain_memory(mode="structured-files")` and does not grow the finite `record_memory` selector

## ADDED Requirements

### Requirement: Structured-file maintenance is one generated preview and apply surface

The canonical registry SHALL expose `maintain_memory(mode="structured-files")` consistently through MCP, CLI, REST, OpenAPI, capability guidance, and schema-fidelity fixtures. It SHALL require exactly one collection selector and SHALL default to read-only preview. Mutating apply SHALL additionally require the deterministic preview plan identity and unchanged source snapshot. Preview SHALL remain lease-free; apply SHALL be explicitly classified mutating and SHALL enter the normal writer, idempotency, terminal-response, and projector paths.

#### Scenario: Preview is safe to inspect

- **WHEN** structured-file maintenance is invoked for a collection without an apply plan identity
- **THEN** every surface returns the same bounded read-only representation plan and no canonical file changes

#### Scenario: Apply cannot be inferred from falsey arguments

- **WHEN** a caller supplies an empty, false, unknown, or partial apply selector
- **THEN** validation refuses rather than guessing whether a migration was authorized

#### Scenario: Exact plan applies through every facade

- **WHEN** the same current plan identity and source snapshot are applied through any generated facade
- **THEN** each reaches the same leaf, writer boundary, and terminal result semantics
