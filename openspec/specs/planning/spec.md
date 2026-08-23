# planning Specification

## Purpose
TBD - created by archiving change add-multi-horizon-planning. Update Purpose after archive.
## Requirements
### Requirement: Planning represents intended future state
The Planning profile SHALL represent goals, desired outcomes, ongoing areas, initiatives, priorities, commitments, horizons, and candidate future work. It SHALL NOT represent observed events or measurements, raw received material, proof artifacts, compiled conclusions, or imported staging. Planning SHALL NOT infer success, failure, health, priority, completion, or personal judgment from elapsed time, Records, or external systems.

#### Scenario: Encountered bug becomes candidate work
- **WHEN** a user asks Exomem to retain an encountered bug for possible future work
- **THEN** Planning can capture it as a candidate work item without turning it into a Record, compiled Note, or accepted OpenSpec change

#### Scenario: Observed event remains a Record
- **WHEN** a user reports that a training session, transaction, symptom, measurement, or maintenance event happened
- **THEN** the event remains Records observed state rather than becoming Planning intent

#### Scenario: Review and interpretation remain explicit
- **WHEN** a plan links to recorded observations
- **THEN** Planning preserves only the authored intent and evidence pointer and does not infer progress or compile a conclusion

### Requirement: Human-owned Planning collections
An explicit Planning collection SHALL use an ordinary `_collection.md` manifest under exact portable `Knowledge Base/Planning/**` path segments and SHALL declare `semantic_profile: planning`. Its first-delivery canonical storage SHALL be `markdown-items` under the same exact layer. Each item SHALL be an ordinary human-readable UTF-8 Markdown file with typed YAML properties and an optional readable body. No database, index, audit log, or generated view SHALL become canonical.

#### Scenario: User works without Exomem or a plugin
- **WHEN** a user opens, adds, moves, or edits a Planning item with an ordinary editor
- **THEN** the canonical intent remains understandable and usable without an agent, Obsidian, plugin, or hidden database

#### Scenario: Unsupported Planning storage refuses
- **WHEN** a Planning manifest declares chronological-log or dataset storage in this delivery
- **THEN** Planning validation and mutation refuse it without rewriting or promoting the source

#### Scenario: Planning path cannot bypass structured policy
- **WHEN** a Planning manifest or canonical source resolves outside exact `Knowledge Base/Planning/` path segments through case, separator, dot-segment, or symlink aliases
- **THEN** validation refuses before reading canonical item contents

### Requirement: Stable Planning item identity
Every Planning item SHALL expose `(collection_uuid, plan_id)` identity, with `plan_id` an explicit UUID independent of title and path. Standalone item references SHALL use canonical `exomem://plan/<collection-uuid>/<percent-encoded-plan-id>` form. The collection manifest UUID SHALL remain selectable by UUID or its `exomem://memory/<uuid>` reference. Duplicate titles SHALL be valid and SHALL never provide mutation identity.

#### Scenario: Rename preserves planning identity
- **WHEN** a user changes an item's title or moves its Markdown file within the collection source
- **THEN** its canonical Planning reference remains unchanged and its source version changes

#### Scenario: Duplicate titles remain unambiguous by ID
- **WHEN** two work items use the same human-readable title
- **THEN** query returns both and update/triage requires the exact plan ID rather than choosing by title

#### Scenario: Duplicate plan ID refuses mutation
- **WHEN** two authorized files declare the same plan ID
- **THEN** inspection reports identity ambiguity and targeted mutation names no arbitrary winner

### Requirement: Minimal typed Planning core
Every Planning item SHALL declare `type: plan`, `collection_id`, `plan_id`, positive `schema_version`, non-empty `title`, one `kind`, and one `lifecycle`. Kinds SHALL be exactly `area`, `outcome`, `initiative`, and `work-item`; lifecycle SHALL be `active` or `archived`. Outcomes, initiatives, and work items SHALL also declare `status`, `priority`, `commitment`, and `horizon`. Status SHALL be exactly `candidate`, `planned`, `active`, `blocked`, `completed`, or `cancelled`; priority SHALL be `critical`, `high`, `medium`, `low`, or `none`; commitment SHALL be `uncommitted`, `considering`, or `committed`; horizon SHALL be `inbox`, `week`, `month`, `quarter`, `year`, or `multi-year`. Areas SHALL omit status, priority, commitment, horizon, area, and parent.

The profile SHALL accept optional `health`, `window_start`, `window_end`, `area`, `parent`, `progress_evidence`, `execution`, `tags`, and manifest-declared domain fields. Health SHALL be `unknown`, `on-track`, `at-risk`, or `off-track`. `collection_id` and `plan_id` SHALL be UUIDs; title SHALL be at most 512 UTF-8 bytes; body and each declared domain string SHALL reuse the existing 32 KiB value bound; tags SHALL contain at most 32 distinct non-empty strings of at most 128 UTF-8 bytes. Arrays and nested mappings SHALL be strict JSON-compatible values with no unknown reserved system fields. Add `item` and update `changes` SHALL accept authored fields only and SHALL reject `type`, `collection_id`, `plan_id`, `schema_version`, `item_version`, and audit-marker mutation. Area/non-area kind conversion SHALL refuse after creation; deliverable kind changes MAY occur only among outcome, initiative, and work-item and SHALL validate the complete final state/hierarchy. The schema SHALL remain independent from templates and the Markdown body.

#### Scenario: Minimal capture receives safe explicit defaults
- **WHEN** `add` receives only a title and no explicit structural fields
- **THEN** it creates an active-lifecycle work item with `candidate`, `none`, `uncommitted`, `inbox`, and `unknown` defaults and returns every authored/defaulted value

#### Scenario: Area does not require a delivery horizon
- **WHEN** a user creates an ongoing area without priority, commitment, or horizon
- **THEN** the item validates without pretending the area is a time-bounded goal

#### Scenario: Domain pack extends rather than forks core
- **WHEN** a Planning manifest adds valid typed domain fields, views, tags, or templates
- **THEN** the shared Planning semantics remain unchanged and the extra fields validate through the declared schema

#### Scenario: Unknown field remains visible as a direct-edit finding
- **WHEN** a human adds an undeclared property to an otherwise readable Planning item
- **THEN** inspection reports the schema violation without dropping, rewriting, or silently adopting the property

### Requirement: Explicit lifecycle and horizon semantics
Horizon SHALL be an authored planning bucket, not an automatically moving time calculation. Named horizon views SHALL select exact authored values and SHALL label that provenance; `week` SHALL NOT claim calendar freshness. Optional `window_start` and `window_end` SHALL be ISO dates and SHALL be independently filterable; when both exist, start SHALL NOT be after end.

An area SHALL use lifecycle alone and MAY be archived directly. A candidate deliverable SHALL be active-lifecycle, uncommitted, and inbox. A planned deliverable SHALL be active-lifecycle, considering or committed, and outside inbox. An active or blocked deliverable SHALL be active-lifecycle, committed, and outside inbox. Only completed or cancelled deliverables MAY be archived. A completed deliverable SHALL be committed and outside inbox. A cancelled deliverable MAY use any declared priority, commitment, and horizon combination, including uncommitted inbox cancellation, because those fields are explicit final state rather than inferred prior state. Reopening or changing any state SHALL be explicit and SHALL validate the complete final combination. No automatic transition exists.

#### Scenario: Time passage does not rebucket intent
- **WHEN** an item remains in the `week` horizon after the authored week passes
- **THEN** Exomem leaves the item unchanged, labels it as authored bucket state rather than calendar fact, and waits for explicit triage

#### Scenario: Invalid date window refuses mutation
- **WHEN** an add, update, or triage request makes `window_start` later than `window_end`
- **THEN** validation refuses before canonical publication

#### Scenario: Archive preserves completed status
- **WHEN** a completed item is explicitly archived
- **THEN** lifecycle becomes archived, status remains completed, and audit history preserves the transition

#### Scenario: Archived candidate refuses
- **WHEN** an update attempts to archive a candidate, planned, active, or blocked deliverable
- **THEN** validation refuses until the same guarded update supplies a coherent completed or cancelled terminal state

#### Scenario: Record changes do not change plan status
- **WHEN** linked Records receive new rows that appear relevant to a plan
- **THEN** Planning status, health, commitment, and horizon remain exactly as authored

### Requirement: Outcomes above initiatives and work items
Planning SHALL keep ongoing area membership separate from the desired-outcome hierarchy. An outcome SHALL NOT have a parent. An initiative MAY name exactly one outcome parent. A work item MAY name exactly one initiative parent. A committed initiative must have an outcome parent and a committed work item must have an initiative parent while active-lifecycle; archived deliverables MAY retain their last valid hierarchy. An area SHALL NOT have a parent and MAY be referenced by an outcome, initiative, or work item through the separate `area` property. Candidate and considering items MAY omit parent and area. Parent and area values SHALL be canonical same-collection `exomem://plan/<collection-uuid>/<plan-uuid>` references to authorized items of the required kind. An active-lifecycle source item SHALL reference only active-lifecycle targets; an archived source item MAY retain correctly typed links to active or archived targets. Missing, withheld, or structurally invalid targets SHALL refuse with the same bounded relation error. If child and parent both declare area, both references SHALL match; absent area SHALL remain absent rather than being copied or inferred.

#### Scenario: Valid planning chain is queryable
- **WHEN** an outcome contains an initiative that contains a work item and all three reference one area
- **THEN** bounded hierarchy output preserves outcome above initiative above work item while reporting area as a container rather than another goal level

#### Scenario: Inbox capture requires no premature hierarchy
- **WHEN** a candidate work item is captured before its outcome or initiative is known
- **THEN** add succeeds without a parent and triage can assign the relationship later with current stale-write guards

#### Scenario: Invalid parent kind refuses
- **WHEN** a work item names an outcome or area as its parent
- **THEN** mutation refuses without rewriting either item

#### Scenario: Hierarchy cycle refuses
- **WHEN** an update or triage operation would introduce a direct or transitive parent cycle
- **THEN** it refuses before publication and returns no hidden target content

#### Scenario: Archiving a live parent with active children refuses
- **WHEN** update would archive an outcome or initiative that still has active-lifecycle children
- **THEN** it refuses and requires the children to be moved or archived explicitly

#### Scenario: Archiving an area with active members refuses
- **WHEN** update would archive an area still referenced by an active-lifecycle outcome, initiative, or work item
- **THEN** it refuses and requires those memberships to be moved, cleared, or archived explicitly

### Requirement: Capture and triage remain deliberate
Planning SHALL support rapid candidate capture and an explicit triage transition without automatic classification. Triage SHALL operate only on an active-lifecycle outcome, initiative, or work item, require a non-empty exact `transition` mapping, and MAY change only `kind`, `status`, `priority`, `commitment`, `horizon`, `area`, or `parent`. Kind changes SHALL remain among those three deliverable kinds; `area` and `parent` MAY be explicit null to clear them. Triage SHALL revalidate the complete resulting item and hierarchy with the current container hash and item version and SHALL NOT infer values from title/body text. Areas, lifecycle, health, dates, tags, evidence, execution, domain fields, title, and complete body replacement SHALL remain add/update-only.

#### Scenario: Feature request enters inbox cheaply
- **WHEN** a user asks to retain a feature request without choosing priority or schedule
- **THEN** Planning stores it in the default candidate inbox state and does not ask for irrelevant storage details

#### Scenario: Triage promotes intent without copying execution contracts
- **WHEN** a candidate that already carries an OpenSpec pointer becomes a committed quarterly initiative
- **THEN** one guarded triage mutation updates only the Planning transition fields while detailed requirements and tasks remain absent from the item

#### Scenario: Stale triage refuses
- **WHEN** a direct edit changes the item after the agent read it
- **THEN** triage refuses the prior item version and preserves the human edit

### Requirement: Guarded Planning mutation and audit
Planning `create`, `add`, `update`, and `triage` SHALL require a non-empty single-line reason of at most 512 UTF-8 bytes, enter same-vault mutation serialization, validate the final schema and hierarchy before staging, honor exact container/item drift guards, use guarded batch publication, and return the exact receipt below. Caught publication errors SHALL roll completed replacements back; abrupt termination MAY leave canonical truth plus a detectable positive audit gap. New Planning manifests SHALL use `plan_audit: {version: 1, head: <24-lowercase-hex-transition-id>}` and each agent-touched Markdown item SHALL carry exactly one latest content-free YAML comment `# exomem-plan-audit: <transition-id>`. An exact add retry with the same plan ID and normalized payload SHALL be idempotent; reusing the ID with different content SHALL refuse. Missing IDs SHALL never fall back to fuzzy title or body matching.

For update, each non-null `changes` value SHALL replace one authored top-level property. Null SHALL delete only optional `health`, `window_start`, `window_end`, `area`, `parent`, `progress_evidence`, `execution`, `tags`, or a manifest-declared optional domain field. Null SHALL refuse for required core fields, required domain fields, and all system/audit fields. The complete resulting item SHALL validate before publication; deleting a date, relationship, or optional domain field is not represented by persisting YAML null.

Every successful mutation or exact add replay SHALL return exactly `_plan_receipt: "exomem.planning-mutation"`, `receipt_version: 1`, `operation`, `collection_id`, `plan_id` (null for create), `before_item_hash`, `after_item_hash`, `before_container_hash`, `after_container_hash`, `affected_paths`, `payload_hash`, `outcome`, and `audit_correlation`. Hashes are lowercase SHA-256 or null as appropriate; operation is `create`, `add`, `update`, or `triage`; outcome is `committed` or `replayed`; transition correlation is 24 lowercase hex. Planning activity lines SHALL use prefix `Planning audit-v1 ` followed by the existing strict content-free event keys, with operation `plan_create`, `plan_add`, `plan_update`, or `plan_triage`. Create events SHALL have null `item_key` and name the declared source as `canonical_path`; add/update/triage events SHALL have the normalized UUID in `item_key` and a canonical Markdown item path contained by the declared source.

#### Scenario: Exact add retry creates one item
- **WHEN** a client retries one add with the same identity and payload
- **THEN** Planning returns the existing committed item without adding a duplicate

#### Scenario: Stale update preserves direct edit
- **WHEN** a user changes a canonical item before an agent update carrying prior hashes
- **THEN** update refuses and every canonical byte from the user's edit remains unchanged

#### Scenario: Failed semantic validation publishes nothing
- **WHEN** hierarchy, schema, governance, or drift validation refuses
- **THEN** no item, manifest audit head, activity event, or committed terminal is published

#### Scenario: Caught publication error rolls back
- **WHEN** a caught error occurs after one planned replacement but before the guarded batch completes
- **THEN** Exomem restores every completed replacement and returns no committed receipt

#### Scenario: Abrupt interruption remains detectable
- **WHEN** the process terminates after canonical replacement but before the activity event is durable
- **THEN** canonical Markdown remains truth and report-only inspection returns a positive audit gap

#### Scenario: Same-vault writes serialize
- **WHEN** cooperating agents mutate one Planning collection concurrently
- **THEN** the existing writer boundary prevents torn or silently lost updates while separate vaults remain independent

### Requirement: Bounded multi-horizon Planning query
Planning query SHALL reuse the existing structured filter/aggregate grammar and 64 KiB response bound and SHALL default to 100 rows with a hard cap of 1,000. It SHALL support exact horizon and date-window filters, deterministic sort, bounded pagination, `active`/`archived`/`all` lifecycle selection, saved views, projection, bounded grouping, and optional hierarchy expansion. Newly scaffolded manifests SHALL declare `inbox`, `week`, `month`, `quarter`, `year`, and `multi-year` views over active lifecycle. A compatible hand-authored manifest SHALL expose only views it declares; missing views refuse and are never synthesized, while explicit horizon filters remain available.

`hierarchy_mode` SHALL be `none`, `ancestors`, or `descendants`; depth SHALL default to 3 and cap at 8; hierarchy nodes SHALL default to 100 and cap at 500. Expansion SHALL start from the normal page, leave page totals unchanged, and return either null for `none` or exactly `{mode, roots, nodes, edges, max_depth, max_nodes, truncated}`. `roots` is the ordered page plan IDs, `nodes` is the deterministic authorized expanded row list, and each edge is exactly `{parent, child}`. Aggregates and CSV SHALL refuse hierarchy expansion.

Every query response SHALL contain exactly `collection_id`, `snapshot`, `rows`, `returned`, `total_matched`, `truncated`, `continuation`, `derived`, `generated_at`, `rendered`, `output_format`, `aggregate`, `query`, `source_versions`, `view`, `hierarchy`, and `agent_history`. Rows SHALL contain `collection_id`, `plan_id`, `item_version`, `body`, and projected schema fields; `body` is the complete authorized Markdown body and may be omitted only by explicit column projection. `query` SHALL contain the normalized effective filter/projection/sort/date/lifecycle/hierarchy definition; `source_versions` entries SHALL be exact `{path, hash}` mappings; `view` SHALL be null or exact `{name, definition, identity}`; `agent_history` SHALL be null or exact `{status, complete, truncated, events}` capped at 50 content-free events; `generated_at` SHALL be an ISO-8601 UTC datetime; `derived` SHALL be true; absent optional payloads SHALL be null rather than omitted.

#### Scenario: Week and multi-year use one schema
- **WHEN** a collection contains work across week, quarter, year, and multi-year horizons
- **THEN** named views select the authored horizons without copying items into horizon-specific stores

#### Scenario: Hierarchy expansion is bounded
- **WHEN** a query requests hierarchy expansion over more nodes or depth than allowed
- **THEN** it applies deterministic caps and reports truncation instead of returning an unbounded graph

#### Scenario: Source change invalidates continuation
- **WHEN** a direct edit changes an authorized item between paginated Planning requests
- **THEN** a continuation bound to the prior snapshot refuses rather than skipping or duplicating work

#### Scenario: No computed progress rollup appears
- **WHEN** a caller queries an outcome with descendants and Records evidence descriptors
- **THEN** the result may show authored fields and bounded relationships but does not invent completion percentages, capacity, or success judgments

### Requirement: Planning views are provenance-bearing derived output
Generated Planning JSON, Markdown, and CSV responses SHALL identify the exact collection, saved-view or query definition, source hashes, generation time, and `derived: true`. Query and inspection SHALL NOT persist a dashboard, summary, export, rollup, or changed horizon, and SHALL NOT promote any derived response to canonical status.

#### Scenario: Roadmap rendering is not another source of truth
- **WHEN** a client renders a quarterly roadmap in Markdown
- **THEN** the response names its canonical snapshot and query and no `_summary.md`, dashboard, or export file is written

#### Scenario: Re-render after direct edit uses current files
- **WHEN** a human changes priority or horizon in an item file
- **THEN** the next fresh rendering reflects the canonical edit and reports a changed source snapshot

### Requirement: Opaque Records progress evidence
A Planning item MAY carry at most 16 `progress_evidence` mappings. Each mapping SHALL contain exactly `collection`, `role`, and `view`: `collection` is a syntactically valid opaque `exomem://memory/<uuid>` reference, `role` is `progress` or `completion`, and `view` is a non-empty name of at most 128 UTF-8 bytes. Planning SHALL validate, round-trip, and disclose the descriptor only with its containing L6 Planning item. It SHALL NOT resolve or target-authorize the collection, validate that the view exists, read Records, compare intent with observation, or change either profile. Inline Records query descriptors and governed target evaluation SHALL remain deferred to planned-versus-recorded Review.

#### Scenario: Completion evidence round-trips without evaluation
- **WHEN** an outcome points to a Record collection saved view as completion evidence
- **THEN** inspection/query returns the authorized descriptor unchanged and performs no Records resolution or completion judgment

#### Scenario: Invalid or unbounded descriptor refuses
- **WHEN** a descriptor contains an extra key, malformed opaque reference, unknown role, missing view, or oversized value
- **THEN** Planning mutation refuses before publication

#### Scenario: Hidden or missing collection does not change round-trip shape
- **WHEN** an otherwise valid descriptor names a hidden, absent, or not-yet-created Record collection
- **THEN** Planning returns the same authorized opaque descriptor because this delivery does not resolve the target

#### Scenario: Records remain canonical observation
- **WHEN** a Planning item points at a Record collection
- **THEN** the plan does not copy Record history and the Record does not silently become a goal

### Requirement: Thin external execution pointers
A Planning item MAY carry at most 16 `execution` mappings. Each mapping SHALL contain exactly `kind` and `ref`, plus optional `label`. `kind` SHALL be `openspec`, `repository`, `issue`, `pull-request`, `release`, `deployment`, or `other`; `ref` SHALL be non-empty opaque text of at most 2,048 UTF-8 bytes; `label` SHALL be non-empty text of at most 256 UTF-8 bytes. No phase, health, task state, requirements, tests, code, remote response, or other machine-readable payload is permitted inside the pointer. Planning SHALL NOT fetch, resolve, mirror, or target-authorize it. The Planning item's top-level health is the only coarse authored health projection; detailed execution truth remains in OpenSpec and the repository.

#### Scenario: Promoted software work stays visible but thin
- **WHEN** a Planning initiative is accepted into an OpenSpec change
- **THEN** the item retains planning identity, hierarchy, horizon, top-level authored health, and the OpenSpec pointer without copying the change artifacts or remote phase

#### Scenario: External state does not mutate Planning automatically
- **WHEN** a linked pull request merges or deployment changes outside Exomem
- **THEN** Planning remains unchanged until an explicit edit or future reconciliation operation

#### Scenario: Opaque reference cannot disclose a hidden vault target
- **WHEN** an execution reference resembles a local path, stable ID, or inaccessible target
- **THEN** Planning treats it as bounded opaque text and does not resolve it into content or public ambiguity

### Requirement: Manual-edit inspection is report-only
Planning queries SHALL read current authorized canonical files. Inspection SHALL return exactly `kind`, `report_only`, `contract`, `snapshot`, `source_versions`, `diagnostics`, `audit`, and `saved_views`. `kind` SHALL be `collection`, `report_only` true, and `contract` SHALL contain exactly `collection_id`, `path`, `title`, `semantic_profile`, `schema_version`, and `storage`; `storage` SHALL contain exactly `strategy: markdown-items`, `source`, and `format_version: 1`. Source-version entries SHALL be exact `{path, hash}` mappings. Diagnostics SHALL be a list of at most 64 exact `{code, reason}` mappings; audit SHALL contain exact `status` (`baseline`, `ok`, `gap`, or `history_incomplete`) and `gaps`; saved views SHALL expose exact `{name, definition, identity}`. Inspection SHALL report unsupported versions, schema violations, duplicate/missing identities, invalid states/dates, parent/area problems, missing templates, and positive agent-audit gaps without changing canonical items or inventing history. Generic derived-index repair SHALL remain under explicit `maintain_memory(mode="reconcile", dry_run=false)` and SHALL repair only rebuildable state.

#### Scenario: Direct edit is visible with audit gap
- **WHEN** a user changes one valid Planning item outside Exomem
- **THEN** the next query shows the edit and inspection reports the bounded positive audit gap without repairing it

#### Scenario: Manual invalidity is human-repair only
- **WHEN** direct edits create an invalid date, dangling parent, hierarchy cycle, or duplicate plan ID
- **THEN** inspection reports it, leaves files untouched, and all Planning mutations for the collection refuse until a human restores one uniquely parseable valid collection

### Requirement: Stable Planning refusal envelope
The Python Planning leaf SHALL raise the existing internal `OpError` with `code`, `message`, and `remediation`; it SHALL NOT return an error-shaped success result. For every Planning-specific refusal, `message` SHALL be non-empty, non-sensitive text of at most 512 UTF-8 bytes and `remediation` SHALL be non-empty, non-sensitive text of at most 512 UTF-8 bytes. The generated MCP wrapper SHALL project that deliberate operation error as normal tool content with exactly `{success: false, error: {code, message, remediation}}` rather than a native MCP execution error. REST and CLI `--json` SHALL return the same exact envelope and stable code; human CLI SHALL render `Error [<code>]: <message>`, then the remediation, and use the canonical operation-error exit status. Unexpected exceptions SHALL remain native MCP errors or the existing internal-error handling for the other facades rather than being mislabeled as Planning refusals.

Planning-specific stable codes SHALL include `INVALID_PLAN_ARGUMENTS`, `PLANNING_PROFILE_REQUIRED`, `INVALID_PLAN`, `PLAN_NOT_FOUND`, `AMBIGUOUS_PLAN`, `PLAN_ID_CONFLICT`, `STALE_PLAN_CONTAINER`, `STALE_PLAN_ITEM`, `INVALID_PLAN_RELATION`, `INVALID_PLAN_CONTINUATION`, `STALE_PLAN_SNAPSHOT`, and `PLAN_RESPONSE_TOO_LARGE`; each SHALL have one deterministic remediation in the shared error registry. Existing shared collection, governance, create-only, writer-busy, and committed-terminal errors SHALL retain their existing public fields and retry or commit metadata unchanged. Missing and withheld targets SHALL use the same Planning-specific code, message, and remediation, and stale or ambiguous refusals SHALL contain no current item values, title, body, relationship, or hash.

#### Scenario: Hidden target does not change relation refusal
- **WHEN** an area or parent reference names a withheld rather than nonexistent target
- **THEN** the same bounded `INVALID_PLAN_RELATION` application envelope is returned without target identity or existence

#### Scenario: Deliberate refusal has surface parity
- **WHEN** the same Planning-specific validation refusal is invoked through MCP, REST, CLI `--json`, and human CLI
- **THEN** MCP, REST, and JSON CLI expose the same `{success: false, error: {code, message, remediation}}` data while human CLI renders the same three fields and canonical operation-error exit status

#### Scenario: Unexpected exception stays outside the application-error plane
- **WHEN** an unexpected implementation exception escapes the Planning leaf
- **THEN** the MCP wrapper preserves native execution-error behavior and no facade converts it into a Planning-specific stable code

### Requirement: Additive adoption and compatibility
Planning SHALL be opt-in through an explicit manifest or `create`; it SHALL NOT rewrite existing notes, task files, issue trackers, Records, or arbitrary vault folders. Existing `semantic_profile: planning` manifests that do not satisfy the first product schema SHALL remain inspectable at the generic collection level but SHALL refuse Planning mutation with actionable diagnostics. Existing Records behavior, manifests, `record_id`, `record_audit`, `exomem://record/...`, tracker compatibility, and response shapes SHALL remain unchanged.

#### Scenario: Existing planning note is not auto-adopted
- **WHEN** the vault contains ordinary Markdown discussing goals or a roadmap without a Planning manifest
- **THEN** Exomem leaves it untouched and does not enumerate or mutate it as a Planning collection

#### Scenario: Record command still refuses Planning
- **WHEN** a Planning manifest is supplied to `record_memory`
- **THEN** the existing Records-profile refusal remains in force and no Planning file changes

#### Scenario: Planning command refuses Records
- **WHEN** a Records manifest is supplied to a mutating `plan_memory` action
- **THEN** Planning returns the corresponding profile-required refusal and no Record changes

### Requirement: Planning items may declare motivating knowledge

A Planning item MAY declare an optional `motivation` property: a list of at
most 16 `exomem://memory/<uuid>` references to the knowledge that motivates
the item. Each entry SHALL be validated the same way a `progress_evidence`
collection reference is validated — parsed and refused if malformed — without
resolving the referenced page or checking that it exists. A non-list value, a
list of more than 16 entries, or any entry that is not a well-formed
`exomem://memory/` reference SHALL be refused. Absence of `motivation` SHALL
behave exactly as before this field existed: no default is applied, and no
existing Planning item's validity changes because it omits the field. A
collection MAY accept `motivation` only after declaring it in its manifest
`item_schema.fields`, the same precondition `progress_evidence` and
`execution` already require.

#### Scenario: Valid motivation list is accepted and round-trips
- **WHEN** `add` receives an item with a `motivation` list of one or more
  well-formed `exomem://memory/` references, on a collection whose manifest
  declares `motivation`
- **THEN** the item is captured, the references serialize unchanged into the
  item's frontmatter, and a subsequent query returns the same list

#### Scenario: More than sixteen motivations is refused
- **WHEN** an `add` or `update` request supplies a `motivation` list with
  more than 16 entries
- **THEN** validation refuses before canonical publication

#### Scenario: Malformed motivation reference is refused
- **WHEN** a `motivation` entry is not a well-formed `exomem://memory/<uuid>`
  reference — including an `exomem://plan/...` reference to another Planning
  item
- **THEN** validation refuses the same way an invalid `progress_evidence`
  collection reference is refused

#### Scenario: Non-list motivation is refused
- **WHEN** `motivation` is supplied as a value other than a list
- **THEN** validation refuses before it reaches generic schema type-checking

#### Scenario: Absence of motivation behaves exactly as before
- **WHEN** an item omits `motivation` entirely
- **THEN** capture, update, triage, and query behave exactly as they did
  before this field existed, with no key defaulted in

### Requirement: Motivation is queryable and never becomes a relation or recall edge

Planning queries SHALL support selecting items by a motivating reference
through the existing generic filter mechanism: a filter on the `motivation`
column selects every item whose `motivation` list contains the given
reference, on any collection whose manifest declares `motivation`. This
capability SHALL NOT require a new top-level query parameter, since the
existing `filters` argument already expresses it. `motivation` SHALL NOT
participate in Planning's parent/area relation graph — it SHALL NOT satisfy a
required `parent` or `area` relation, and it SHALL NOT be read when computing
hierarchy edges. Because raw Planning items never enter ordinary semantic
recall or the relation graph regardless of field content, a plan carrying
`motivation` SHALL NOT appear as a memory hit and SHALL NOT create a graph
edge toward the memory it cites. Plans cite knowledge; knowledge never cites
plans back through this field.

#### Scenario: Motivation query filter selects the referencing items
- **WHEN** a query supplies a filter selecting Planning items whose
  `motivation` contains a given `exomem://memory/` reference
- **THEN** only items whose `motivation` list contains that reference are
  returned

#### Scenario: Motivation does not satisfy a required relation
- **WHEN** a committed initiative or work item supplies `motivation` but
  omits the `parent` its commitment requires
- **THEN** validation refuses the missing relation exactly as it would
  without `motivation` present

#### Scenario: Motivated plans remain outside recall and the graph
- **WHEN** a Planning item declares one or more `motivation` references
- **THEN** the item remains excluded from ordinary semantic recall and from
  the relation graph exactly as every other raw Planning item is, and no new
  graph edge is created toward the referenced memory

### Requirement: Planned-versus-recorded review over shipped primitives

Exomem SHALL expose a read-only planned-versus-recorded review as `review_memory(mode="plan-progress")`, reaching MCP, REST, and CLI through the existing generated command without a signature change. The review SHALL select Planning items that are `lifecycle: active`, `status: active`, `commitment: committed`, and carry at least one `progress_evidence` descriptor. Status and commitment SHALL be selected through the shipped bounded Planning query grammar; the presence of `progress_evidence` SHALL be tested on the returned item, because an otherwise valid Planning manifest need not declare that optional field.

For each selected item the review SHALL execute every authored `progress_evidence` descriptor as the named Records saved view over the referenced Records collection, using only shipped primitives: released-manifest resolution, the governed Records query path, and the default-deny query envelope. The review SHALL NOT define a new query grammar, filter operator, saved-view feature, storage adapter, index, cache, or persisted artifact, and SHALL NOT traverse a Records manifest `links.plans` descriptor in the opposite direction.

An optional `collection` selector MAY restrict the review to one Planning collection. Absent a selector, the review SHALL scan authorized Planning collections. A Planning collection that cannot be resolved or queried SHALL increment a bounded unavailable counter and SHALL disclose no path, title, identity, or existence.

#### Scenario: Committed active item with evidence is reviewed
- **WHEN** an active committed Planning item names a Records collection saved view as progress evidence and that collection holds matching records
- **THEN** the review presents the item's authored intent together with the exact number of records the bound saved view matched
- **AND** it names the collection, the role, and the view for every executed binding

#### Scenario: Items outside the reviewed selection are absent
- **WHEN** a Planning collection also contains candidate, planned, blocked, uncommitted, archived, or evidence-free items
- **THEN** those items do not appear in the review and no placeholder, stub, or inferred entry is produced for them

#### Scenario: Undeclared evidence field does not refuse the collection
- **WHEN** a Planning manifest does not declare the optional `progress_evidence` field
- **THEN** the review returns zero reviewed items for that collection instead of refusing it

### Requirement: Bound evidence executes under authorization-precedes-resolution

Every cross-profile evidence hop SHALL authorize before it resolves and SHALL resolve before any canonical Records source is parsed. The review SHALL resolve an evidence target only as a fully released Records manifest, SHALL authorize the named saved view before the query runs, and SHALL read observed numbers only from a non-withheld default-deny query envelope.

An evidence binding that cannot be executed SHALL be reported with exactly one bounded reason from `collection_unavailable`, `profile_mismatch`, `view_unavailable`, `query_unavailable`, `result_withheld`, or `budget_exhausted`. A missing collection and a withheld collection SHALL both report `collection_unavailable`, so the review cannot be used to probe for hidden collections. An unresolved binding SHALL contribute no observed numbers and SHALL NOT remove the item from the review.

The review SHALL be bounded independently of vault size. It SHALL cap the number of reviewed items, cap the number of distinct evidence-query executions per call, execute each distinct `(collection, view)` pair at most once per call and reuse the result, and report truncation explicitly rather than returning a silently partial answer.

#### Scenario: Withheld evidence target matches an absent one
- **WHEN** one item's evidence names a governance-withheld Records collection and another item's evidence names a collection that does not exist
- **THEN** both bindings report `collection_unavailable` with no path, title, identity, snapshot, or count
- **AND** both items remain in the review with the rest of their bindings intact

#### Scenario: Unknown saved view is a bounded reason, not a failure
- **WHEN** an evidence descriptor names a view the Records manifest does not declare
- **THEN** that binding reports `view_unavailable` and the review still returns every other binding and item

#### Scenario: Execution budget truncates explicitly
- **WHEN** the selected items together bind more distinct collection-and-view pairs than the per-call execution budget allows
- **THEN** the unexecuted bindings report `budget_exhausted`, the response reports binding truncation as true, and no binding is silently dropped

#### Scenario: Repeated binding executes once
- **WHEN** several reviewed items bind the same Records collection and the same saved view
- **THEN** that query executes once per call, every item reports the same exact numbers, and the shared execution counts once against the budget

### Requirement: Plan-progress review presents divergence without adjudicating it

The review SHALL present authored intent next to observed numbers and SHALL leave every judgment to the human or calling agent. For each reviewed item it SHALL return the canonical `exomem://plan/<collection-uuid>/<plan-uuid>` reference, the authored intent fields it echoes unchanged, the ordered evidence bindings with their role, view, opaque collection reference, and either observed numbers or an unavailable reason, and a divergence block of non-negative integers containing `evidence_bindings`, `resolved_bindings`, `unresolved_bindings`, `progress_bindings`, `completion_bindings`, `progress_observations`, and `completion_observations`.

Observed numbers per executed binding SHALL be exactly the matched count, the returned count, the truncation flag, and the canonical collection and snapshot identifiers. The review SHALL NOT return Records rows, bodies, or item identities.

A bound saved view's own declared aggregate SHALL NOT be passed through, whatever its shape. The aggregate grammar admits a latest-row selector that carries a complete record including its identity and version, distinct-value and grouped-value shapes that carry record values, and mean/sum/min/max shapes that carry a derived statistic — rows, identities, and a score-shaped value respectively, all three of which this review refuses. The matched count is computed identically under every aggregate shape, so refusing the aggregate withholds no count from the reader.

The response SHALL be marked derived and read-only, SHALL carry a generation timestamp, and SHALL report the number of Planning collections scanned, the number of items matched, the number of items presented, item truncation, binding truncation, and a bounded tally of unavailable reasons.

Items SHALL be ordered deterministically by collection identity then plan identity, and evidence SHALL keep its authored descriptor order. Ordering SHALL carry no ranking meaning.

#### Scenario: Divergence is exact integers
- **WHEN** a committed active item binds two progress views and one completion view, and the completion view matches nothing
- **THEN** the divergence block reports three bindings, two progress bindings, one completion binding, the exact progress match counts, and `completion_observations: 0`
- **AND** the response contains no percentage, ratio, score, estimate, verdict, or severity

#### Scenario: Aggregate-declaring evidence view leaks nothing
- **WHEN** a bound saved view declares a latest-row, distinct-value, grouped-value, or mean aggregate
- **THEN** the binding still resolves and reports its exact matched count
- **AND** no record row, record identity, item version, record value, or derived statistic from that aggregate appears anywhere in the response

#### Scenario: Authored health is echoed, never written
- **WHEN** a reviewed item declares `health: unknown` while its completion evidence matches nothing
- **THEN** the review echoes `unknown` unchanged, proposes no other health value, and the canonical item file is byte-identical afterwards

#### Scenario: Review changes nothing it reads
- **WHEN** the review runs over a vault containing Planning collections, Records collections, and evidence bindings
- **THEN** no plan, record, manifest, audit head, activity event, review state, index, or cache is created, modified, moved, or deleted
- **AND** the only writes a review may produce are the governance kernel's own disclosure receipts for the reads it performed, exactly as any ordinary governed read produces them

#### Scenario: Ordering implies no priority
- **WHEN** the review returns several items with very different observation counts
- **THEN** the items are ordered by collection and plan identity rather than by any measure of divergence

