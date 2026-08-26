## MODIFIED Requirements

### Requirement: One multiplexed Planning product command
The product surface SHALL expose one `plan_memory` command rather than separate capture, horizon, hierarchy, manifest-lifecycle, or storage-specific tools. Its finite selector SHALL contain exactly nine actions: read-only `inspect`, `validate`, and `query`, plus mutating `create`, `add`, `update`, `triage`, `revise`, and `rebaseline`. `inspect` without a collection SHALL return the Planning inventory; with a collection it SHALL inspect that collection. Query SHALL cover bounded horizon/date/history/hierarchy/render/export-shaped responses through explicit arguments; generic derived-index repair and previewed structured-file migration SHALL remain under `maintain_memory`.

#### Scenario: Natural planning intent uses one front door
- **WHEN** an agent receives “save this feature idea”, “file this bug for later”, “make this a quarterly initiative”, “what matters this week”, “show my multi-year outcomes”, or “revise this Planning collection”
- **THEN** bootstrap routes the intent through `plan_memory` with the appropriate finite action instead of advertising a family of narrow tools

#### Scenario: Planning storage is not a tool choice
- **WHEN** an agent captures, queries, validates, or revises Planning intent
- **THEN** the same command resolves the Planning collection and Markdown-item adapter without asking the user to select an internal storage operation

#### Scenario: Existing manifest uses the same front door
- **WHEN** an agent needs to validate, revise, or explicitly rebaseline an existing Planning collection manifest
- **THEN** it uses the finite lifecycle actions on `plan_memory` rather than a generic file editor or storage-specific tool

#### Scenario: Inventory before a collection is known
- **WHEN** an agent calls `plan_memory(action="inspect")` with no collection
- **THEN** the response is the bounded Planning inventory and nothing is created or resolved

#### Scenario: Review does not hide inside query
- **WHEN** a caller queries a plan that carries Records evidence descriptors
- **THEN** `plan_memory` returns authored Planning state and descriptors without evaluating planned-versus-recorded progress or silently invoking epistemic `review_memory`

### Requirement: Planning actions validate arguments explicitly
The generated signature SHALL expose exactly `action`, `collection`, `manifest_path`, `manifest_text`, `why`, `scaffold`, `view`, `filters`, `columns`, `sort_by`, `descending`, `limit`, `aggregate`, `date_from`, `date_to`, `date_column`, `lifecycle`, `hierarchy_mode`, `hierarchy_depth`, `hierarchy_limit`, `continuation`, `include_agent_history`, `output_format`, `item`, `plan_id`, `expected_manifest_hash`, `expected_container_hash`, `acknowledged_gap_codes`, `body`, `changes`, `transition`, and `expected_item_version`. `action` SHALL be required and select the following exact matrix; every non-listed argument SHALL be forbidden rather than ignored:

| Action | Required | Optional and defaults |
| --- | --- | --- |
| `inspect` | none | `collection: string` — omitted returns the Planning inventory |
| `validate` | create mode: `manifest_path: string`, `manifest_text: string`; revision mode: `collection: string`, `manifest_text: string` | none |
| `create` | `manifest_path: string`, `manifest_text: string`, `why: string` | `scaffold: boolean=true` |
| `query` | `collection: string` | `view: string`; existing structured `filters`, `columns`, `sort_by`, `aggregate`, `date_from`, `date_to`, `date_column`; `descending: boolean=false`; `limit: integer=100` capped at 1,000; `lifecycle: active|archived|all=active`; `hierarchy_mode: none|ancestors|descendants=none`; `hierarchy_depth: integer=3` capped at 8; `hierarchy_limit: integer=100` capped at 500; `continuation: string`; `include_agent_history: boolean=false`; `output_format: json|markdown|csv=json` |
| `add` | `collection: string`, `item: object`, `why: string` | `plan_id: UUID` — omitted derives the identity from the declared natural key when every key field is present, `expected_container_hash: sha256`, `body: string=""` |
| `update` | `collection: string`, `plan_id: UUID`, `expected_container_hash: sha256`, `expected_item_version: sha256`, `why: string`, at least one of `changes` or `body` | `changes: non-empty object` using the Planning spec's exact null-as-delete rules; `body: complete string replacement` |
| `triage` | `collection: string`, `plan_id: UUID`, `transition: non-empty object`, `expected_container_hash: sha256`, `expected_item_version: sha256`, `why: string` | none |
| `revise` | `collection: string`, `manifest_text: string`, `expected_manifest_hash: sha256`, `expected_container_hash: sha256`, `why: string` | none |
| `rebaseline` | `collection: string`, `expected_manifest_hash: sha256`, `expected_container_hash: sha256`, `acknowledged_gap_codes: non-empty array[string]`, `why: string` | none |

The two `validate` forms SHALL be mutually exclusive and read-only. Revision-mode `validate` SHALL return lifecycle guards only as the closed object `{"expected_manifest_hash":"<sha256>","expected_container_hash":"<sha256>"}` when the collection can be safely exposed. Saved view SHALL exclude inline filter/projection/sort/date/aggregate/lifecycle shaping, but MAY combine with hierarchy, continuation, history, and output controls. Hierarchy SHALL be forbidden with aggregate or CSV output. `transition` SHALL contain only `kind`, `status`, `priority`, `commitment`, `horizon`, `area`, or `parent`; only `area` and `parent` may be null, and kind changes stay among outcome/initiative/work-item. `update` SHALL reject `transition`; `triage` SHALL reject area source items, item, changes, lifecycle, body, health, dates, tags, evidence, execution, and domain-field convenience arguments. `why` SHALL be non-empty single-line text capped at 512 UTF-8 bytes. No action SHALL ignore explicit false, empty, or zero values before validation.

#### Scenario: Read action rejects mutation payload
- **WHEN** `inspect`, `validate`, or `query` receives an argument outside its declared shape
- **THEN** validation refuses rather than ignoring the ambiguous payload

#### Scenario: Collection-bound actions do not treat inventory as a fallback
- **WHEN** `query`, `add`, `update`, `triage`, `revise`, or `rebaseline` omits `collection`
- **THEN** validation refuses, while `inspect` without a collection returns the inventory

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

## ADDED Requirements

### Requirement: Structured-collection mutations are due-state carriers

`record_memory` `append` and `update` and `plan_memory` `add`, `update` and `triage` responses SHALL carry the bounded advisory due-state block under the same carrier contract and emission governance as page writes: the write applies its delta, the block is served through the release plane, emission is recorded once per delivered block, a batch scope delivers at most once, family dispositions apply, and an unreadable review state yields no block while the write still commits. The leaves SHALL reuse the shared due-state helpers rather than re-deriving any of it.

#### Scenario: The append that opens a gap reports it

- **WHEN** a record append joins an open Planning item for the first time
- **THEN** that append's own response carries a due-state block counting one `unreflected_outcomes` item

#### Scenario: A batch of appends delivers once

- **WHEN** twelve appends run inside one batch scope
- **THEN** at most one block is delivered and the emission ledger records one emission

#### Scenario: Silence on an unreadable store

- **WHEN** the review state cannot be read
- **THEN** the append commits, the response carries no block, and no error is raised for the advisory

### Requirement: Planned-versus-recorded review is discoverable in the tool surface

The `review_memory` `mode` documentation in the generated tool surface SHALL list `plan-progress` with its one-line purpose, and the `plan_memory` description SHALL document the inventory form of `inspect`. The pinned tool-surface digest SHALL move once for both, with the packaged contract, the schema fixture, the release-identities fixture, the hosted generated locks and directory packets, and the ChatGPT plugin contract's pending digest regenerated together; no input parameter is added or removed.

#### Scenario: An MCP client can find plan-progress

- **WHEN** a client reads the `review_memory` tool description from the packaged contract
- **THEN** `plan-progress` is listed among the modes

#### Scenario: One pin move

- **WHEN** the tool surface is regenerated for this change
- **THEN** exactly one digest change is recorded and every generated consumer the repository gates agrees on it; the OpenAI directory packet binds a release input rather than the tool-surface pin and is regenerated at that release step
