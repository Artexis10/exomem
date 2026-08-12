## Context

Exomem currently has strong durable-knowledge and observed-state layers but no first-class future-intent layer. Sources preserve received material, Evidence preserves proof, Notes preserve distilled conclusions, Entities preserve reusable identity, and Records now preserve mutable events and observed state. Planning remains only a conceptual profile accepted by the generic collection loader; `record_memory` intentionally refuses to operate on it.

The Records delivery already solved most of the difficult mechanics: human-readable manifests, stable collection and item identity, strict schemas, Markdown-item parsing, bounded query and derived rendering, same-vault serialization, optimistic concurrency, guarded batch publication with caught-error rollback, idempotency, audit history, direct-edit inspection, governance before reduction, and structured-only recall policy. Those mechanics are still named around Records in several modules and serialized contracts (`record_id`, `record_audit`, `exomem://record/...`). Planning must reuse the machinery without making existing Records users migrate.

The semantic boundary is fixed:

- Sources are externally received raw material.
- Evidence is proof-bearing material.
- Notes are distilled conclusions and decisions.
- Entities are stable reusable identities.
- Planning is intended future state.
- Records are observed state and event history.
- Review compares intended and observed state and may propose explicit changes.
- Imported remains adoption staging.

For software, Exomem Planning owns capture, priority, horizon, sequencing, and durable coordination context. OpenSpec and the repository own accepted requirements, design, implementation tasks, tests, code, and execution truth. Planning retains a thin tracking node after promotion; it does not mirror the repository.

This change is pure deterministic substrate. It adds no reasoning model, optional model lane, hidden database, external planning service, or Obsidian runtime dependency.

## Goals / Non-Goals

**Goals:**

- Make bugs, feature candidates, ideas, goals, desired outcomes, initiatives, and concrete work first-class human-owned Planning items.
- Support useful planning across inbox, week, month, quarter, year, and multi-year horizons without separate schemas for each timescale.
- Preserve a clear hierarchy of ongoing areas, outcomes, initiatives, and work items.
- Provide one small `plan_memory` front door across MCP, REST, and CLI.
- Reuse one collection substrate with Records while preserving distinct language, validation, references, and product behavior.
- Keep direct file editing canonical and make agent mutation conflict-safe and auditable.
- Preserve opaque Records evidence descriptors and thin external-execution pointers without inferring progress or mirroring execution systems.
- Apply governance before identity, hierarchy, counts, grouping, and rendering.
- Prevent raw Planning work from flooding ordinary semantic recall.
- Prove both software and non-software planning through real public product paths.

**Non-Goals:**

- Planned-versus-recorded Review, inferred progress, success/failure judgments, or automatic Planning updates.
- Dependency graphs, inline Records query evidence, recursive rollups, or cross-collection hierarchy.
- Task execution, reminders, scheduling, calendaring, time tracking, notifications, or background agents.
- Capacity optimization, effort estimation, automatic prioritization, or automatic horizon rebucketing.
- Automatic GitHub/OpenSpec/PR/deployment synchronization; this delivery stores and queries explicit pointers only.
- A complete roadmap dashboard, dedicated Planning TUI, forms, charts, calendar UI, or Obsidian plugin.
- Persisted summaries or dashboards, writable tabular Planning datasets, or a general relational/spreadsheet engine.
- Migrating existing notes, tasks, issue trackers, Records, or hand-authored folders into Planning automatically.

## Decisions

### 1. Planning is a separate product profile over the shared collection engine

`semantic_profile: planning` remains distinct from `records`. Planning uses the generic manifest loader, schema system, Markdown-item adapter, query evaluator, snapshots, guarded publication, and audit-chain implementation through profile-neutral internal services. It adds a Planning semantic validator and Planning-specific response projector rather than branching Records behavior throughout the engine.

The extraction boundary is a small immutable profile contract that supplies the exact placement root, item-ID property, standalone-reference namespace, manifest audit property, per-item audit marker, reserved system fields, operation labels, semantic validator, and product projector. Existing Records facades remain supported and byte-compatible. Internals may move only where reuse materially removes duplicate mechanics.

Alternative rejected: implement `planning.py` by copying `records.py`, `record_formats.py`, or `record_governance.py`. That would immediately create two identity, concurrency, governance, and reconciliation implementations.

Alternative rejected: route Planning through `record_memory`. Records describe facts; Planning describes intent. Sharing a command would make language and validation vague and would weaken the existing cross-profile refusal.

### 2. The first canonical Planning shape is one Markdown file per item

An explicit Planning collection lives under exact portable `Knowledge Base/Planning/**` path segments. Its manifest is `_collection.md`, and its canonical source uses `storage.strategy: markdown-items`. Each item is an ordinary UTF-8 Markdown file with typed YAML properties and an optional readable body. The source directory may use human-chosen subfolders, but identity and hierarchy never depend on the path.

Planning creation refuses chronological-log and dataset storage in this delivery. The generic substrate continues to support those strategies for other profiles; restricting the first Planning product shape avoids inventing unsafe row updates or awkward block hierarchies. There is no automatic representation migration.

Alternative rejected: store all planning state in one roadmap file. It is pleasant for small manual lists but makes targeted concurrent mutation, stable identity, hierarchy validation, and governance granularity substantially weaker.

Alternative rejected: make SQLite canonical. It breaks ordinary-editor ownership and makes direct use without Exomem impossible.

### 3. One finite `plan_memory` command owns six user actions

The public selector contains exactly:

- `inspect`: report collection, schema, hierarchy, references, and audit/direct-edit findings without mutation.
- `create`: create only a reviewed Planning manifest and optional empty Markdown-item source; never adopt or overwrite existing files.
- `query`: run bounded filters, saved views, horizon/date queries, deterministic sorting, pagination, hierarchy expansion, and derived JSON/Markdown/CSV rendering.
- `add`: add one structured Planning item with a stable UUID and optional readable body.
- `update`: apply targeted field/body changes with current container and item-version guards.
- `triage`: apply a guarded constrained planning transition over kind, status, priority, commitment, horizon, area, or parent; it uses the same stale-write guards and audit path as update.

`inspect` and `query` are read-only. `create`, `add`, `update`, and `triage` enter the existing writer lease, idempotency, precommit governance, guarded publication, and terminal-receipt paths. Every action rejects irrelevant arguments rather than ignoring them. The command is generated once into MCP, REST, and CLI and remains conservatively write-annotated at command level.

The Planning leaf raises the existing internal `OpError` for deliberate refusals. Planning-specific messages and remediations are non-empty, non-sensitive, and capped at 512 UTF-8 bytes. Generated MCP projects these as normal `{success:false,error:{code,message,remediation}}` tool content; REST and JSON CLI use the same envelope, while human CLI renders the same fields through its canonical operation-error path. Unexpected exceptions remain native MCP errors or the existing internal-error path. Existing shared busy and committed-terminal errors keep their richer retry and commit metadata.

Triage exists as a user-facing workflow rather than a second mutation engine. Internally it compiles a validated, explicit update against the current item. It accepts a non-empty `transition` mapping containing only `kind`, `status`, `priority`, `commitment`, `horizon`, `area`, or `parent`, operates only on an active-lifecycle outcome/initiative/work-item, and cannot guess values. `area` and `parent` may be explicit null to clear them; kind changes stay among the three deliverable kinds, and area/non-area conversion is forbidden after creation. Archival, health, dates, tags, evidence, execution pointers, domain fields, title, and body remain under guarded `update`; a manually invalid collection is report-only and human-repair only in this MVP.

The exact first-delivery arguments are:

| Action | Required | Optional | Defaults and exclusions |
| --- | --- | --- | --- |
| `inspect` | `collection: string` | none | Every other argument is forbidden. |
| `create` | `manifest_path: string`, `manifest_text: string`, `why: string` | `scaffold: boolean` | `scaffold=true`; create-only and refuses any existing canonical target. |
| `query` | `collection: string` | `view`, `filters`, `columns`, `sort_by`, `descending`, `limit`, `aggregate`, `date_from`, `date_to`, `date_column`, `lifecycle`, `hierarchy_mode`, `hierarchy_depth`, `hierarchy_limit`, `continuation`, `include_agent_history`, `output_format` | `descending=false`, `limit=100` with hard cap 1,000, `lifecycle=active`, `hierarchy_mode=none`, `hierarchy_depth=3` with hard cap 8, `hierarchy_limit=100` with hard cap 500, `include_agent_history=false`, `output_format=json`; a saved view excludes inline filter/projection/sort/date/aggregate/lifecycle shaping; hierarchy expansion is orthogonal, but is forbidden with an aggregate and CSV. |
| `add` | `collection: string`, `item: object`, `why: string` | `plan_id: UUID`, `body: string`, `expected_container_hash: sha256` | A title-only item receives the documented capture defaults; omitted `plan_id` receives a UUID; `body=""`; no update or triage fields. |
| `update` | `collection: string`, `plan_id: UUID`, `expected_container_hash: sha256`, `expected_item_version: sha256`, `why: string`, and at least one of `changes` or `body` | `changes: object`, `body: string` | `body` is a complete body replacement; non-null `changes` values replace top-level properties, while null deletes only optional health/date/relationship/evidence/execution/tags or manifest-declared optional domain fields; required/core/system fields cannot be deleted; the normalized final item must differ and validate; no `transition`. |
| `triage` | `collection: string`, `plan_id: UUID`, `transition: object`, `expected_container_hash: sha256`, `expected_item_version: sha256`, `why: string` | none | `transition` is non-empty and contains only the seven triage fields above; arbitrary item/body replacement is forbidden. |

Strings and arrays use the schema bounds in Decision 4. Query filters, aggregates, continuations, output formats, and the 64 KiB response ceiling reuse the existing structured-query grammar. `hierarchy_mode` is exactly `none`, `ancestors`, or `descendants`. Expansion starts from the ordinary page of query rows, places authorized expanded rows and `{parent, child}` edges in a separate `hierarchy` member, never changes `total_matched` or pagination, and stops deterministically at the requested depth/node caps.

### 4. The core Planning schema is small, exact, and extensible

Every Planning manifest must declare compatible fields for the core semantic contract. Domain collections may add typed fields and saved views, but may not weaken the core types or enums.

The canonical item envelope uses:

- `type: plan`
- `collection_id`: stable collection UUID
- `plan_id`: stable item UUID
- `schema_version`: positive schema version
- `title`: required non-empty string
- `kind`: `area`, `outcome`, `initiative`, or `work-item`
- `status`: `candidate`, `planned`, `active`, `blocked`, `completed`, or `cancelled`
- `lifecycle`: `active` or `archived`
- `priority`: `critical`, `high`, `medium`, `low`, or `none`
- `commitment`: `uncommitted`, `considering`, or `committed`
- `horizon`: `inbox`, `week`, `month`, `quarter`, `year`, or `multi-year`
- optional `health`: `unknown`, `on-track`, `at-risk`, or `off-track`
- optional `window_start` and `window_end` ISO dates
- optional `area` and `parent` Planning references
- optional `progress_evidence` bounded Records saved-view pointers
- optional `execution` thin external pointers
- optional open-vocabulary `tags`

Title, kind, and lifecycle are universal. Status, priority, commitment, and horizon are required for outcomes, initiatives, and work items and forbidden for ongoing areas, where a delivery state or bucket is meaningless; area health and lifecycle express whether the container needs attention or is archived. The body holds context, rationale, or completion notes. It is not parsed into a hidden second state model.

Horizon is an authored planning bucket, not an automatically moving date calculation. Named default views select the exact horizon value; optional date-window filters are independent and deterministic. A `week` result means “currently authored into the week bucket,” not “calendar-current by inference,” and may expose planning drift until explicit triage. Bootstrap and renderings label this authored-bucket meaning. Exomem never silently rebuckets an item as time passes. If both dates exist, `window_start` must not be after `window_end`.

`candidate + active lifecycle + inbox + uncommitted` is the default capture state. Areas carry no status, delivery horizon, priority, commitment, area, or parent. A candidate is always active-lifecycle, uncommitted, and inbox. `planned` requires considering or committed intent outside inbox; `active` and `blocked` require active lifecycle, committed intent, and a non-inbox horizon; `completed` requires committed intent and a non-inbox horizon. `cancelled` permits any declared commitment/horizon combination because cancellation may occur before or after commitment and the final fields are authored rather than inferred from history. Only completed or cancelled deliverable items may be archived; an area may be archived directly because its lifecycle is its only operational state. Reopening is an explicit update that restores a coherent active-lifecycle state. Completion, cancellation, archival, reopening, and health changes are explicit; nothing is inferred from Records or elapsed dates.

### 5. Hierarchy uses references with strict semantic validation

Areas are ongoing containers, not goals. The optional `area` reference may associate an outcome, initiative, or work item with one area. The desired-outcome hierarchy is separate:

- an `outcome` has no `parent`;
- an `initiative` may name one `outcome` parent;
- a `work-item` may name one `initiative` parent;
- an `area` has no parent and cannot be used as an outcome substitute.

Candidate and considering items may omit area and parent while awaiting commitment. A committed initiative must name one outcome parent; a committed work item must name one initiative parent. Once supplied, references must resolve within an authorized view of the same Planning collection and match the required kind. An active-lifecycle source item may reference only active-lifecycle area/parent targets; an archived source item may retain correctly typed links to active or archived targets so leaf-first archival remains coherent. If a child and its parent both declare an area, the references must match; absence never creates an inferred canonical area. Archiving an area with active-lifecycle members or a parent with active-lifecycle children refuses. Self-links, hierarchy cycles, and references to missing, withheld, or structurally invalid targets refuse mutation without disclosing the hidden target. Direct edits that introduce those conditions remain canonical and visible, but `inspect` reports them and every agent mutation of that collection refuses until a human restores a uniquely parseable valid collection. Duplicate IDs are likewise manual-repair only.

Cross-collection Planning relationships and computed rollups are deferred. External coordination belongs in explicit execution pointers, not in a guessed hierarchy.

### 6. Planning identity and audit names are profile-specific compatibility layers

New Planning items use `exomem://plan/<collection-uuid>/<percent-encoded-plan-id>` references and a `plan_id` property. New Planning manifests use `plan_audit: {version: 1, head: <transition-id>}` and profile-specific content-free item audit markers. The shared identity/audit engine receives these names from the profile contract.

Existing Records continue to use `record_id`, `record_audit`, `exomem://record/...`, and existing markers. No Record file or serialized response is rewritten or renamed. Generic internal models use collection/item terminology while compatibility facades translate the exact v1 Records shape.

The manifest's existing `exomem_id` remains both stable collection UUID and resolvable `exomem://memory/<uuid>` collection selector. Item references remain collection-scoped and do not enter the general page-ID namespace.

### 7. Agent writes are targeted, serialized, idempotent, and direct-edit aware

Planning add/update/triage reuse the Markdown-item snapshot and guarded batch-publication contract. The writer re-resolves the manifest, source inventory, item, and hierarchy targets inside the same-vault boundary. It validates schema and semantic transitions before staging. Update and triage require the exact current container hash and item version; missing or stale targets never fall back to title or fuzzy-text matching.

An exact add replay with the same plan ID and normalized payload is idempotent; reusing the ID with different content is a conflict. Guarded batch publication rolls every completed replacement back after a caught publication error. Abrupt process termination may leave canonical human-owned truth ahead of the activity log; inspection must report that positive gap and never claim a transaction that did not complete. A concise `why` is mandatory for every mutation.

Manual edits remain valid canonical changes. A fresh query reads current files. Inspection reports schema, hierarchy, identity, and positive audit gaps but never repairs or invents history. `maintain_memory(mode="reconcile")` may rebuild derived indexes only.

### 8. Multi-horizon and hierarchy output is bounded derived state

Newly scaffolded manifests provide default saved views named `inbox`, `week`, `month`, `quarter`, `year`, and `multi-year`, each explicitly filtering active lifecycle and its authored horizon. A hand-authored compatible manifest must declare a named view before a call such as `view=week` works; Exomem never synthesizes or persists a missing view, and explicit bounded horizon filters always remain available. All queries bind to the exact authorized collection snapshot, use deterministic sorting, carry bounded continuation metadata, and may render JSON, Markdown, or CSV.

Optional hierarchy expansion returns the bounded nodes and edges defined by the public action matrix. The MVP does not compute completion percentages, recursive progress rollups, capacity, critical paths, dependencies, or automatic blocker status. Counts and groupings are derived query output and never become canonical Planning facts.

Every rendering identifies the collection, exact saved-view or query definition, source snapshot, generation time, and `derived: true`. Nothing writes `_summary.md`, dashboards, or exports automatically.

### 9. Records evidence and external execution remain opaque, typed pointers

`progress_evidence` is a list of at most 16 exact mappings `{collection, role, view}` with no extra keys. `collection` is an opaque syntactically valid `exomem://memory/<uuid>` collection reference, `role` is `progress` or `completion`, and `view` is a non-empty saved-view name of at most 128 UTF-8 bytes. Planning validates syntax and bounds and round-trips the pointer. It does not resolve or target-authorize the collection, confirm that the view exists, execute Records, inspect rows, infer a metric, or change plan state. Disclosure is authorized only through the containing L6 Planning item; the separate planned-versus-recorded Review change will own governed target resolution, view validation, and evaluation. Inline Records queries are deferred with Review.

`execution` is a list of at most 16 exact mappings `{kind, ref}` or `{kind, ref, label}` with no extra keys. `kind` is `openspec`, `repository`, `issue`, `pull-request`, `release`, `deployment`, or `other`; `ref` is non-empty opaque text of at most 2,048 UTF-8 bytes; optional `label` is at most 256 UTF-8 bytes. Exomem does not fetch or authorize the remote system in this delivery. Phase, task state, tests, code, remote health, and discussion stay in OpenSpec and the repository. The Planning item owns only its single top-level authored health value and durable pointer.

Neither pointer type is a wikilink-resolution shortcut. Hidden or nonexistent targets must not change public ambiguity or disclosure shapes.

### 10. Governance runs before Planning reduction or relationship assembly

Planning structured operations require the same full-value release floor as Records. Collection discovery authorizes each candidate path before parsing identity. Markdown-item candidates are authorized before public counts, byte/file caps, parsing, schema findings, identity ambiguity, hierarchy resolution, snapshots, sorting, grouping, or rendering. Mutation requires a complete authorized canonical snapshot; partial visibility refuses.

Typed default-deny Planning envelopes explicitly validate/project rows, links, evidence descriptors, execution pointers, snapshots, history, conflicts, and receipts. A withheld item behaves as absent. Hidden items cannot influence totals, views, tree shape, continuation identity, or error wording. Precommit authorization and disclosure receipts occur before guarded batch publication and remain distinct from Planning activity audit and terminal mutation receipts.

### 11. Raw Planning items are structured-only recall data

The centralized recall policy expands from exact `Knowledge Base/Records/**` handling to both exact structured layers. A strictly valid Planning `_collection.md` remains semantically discoverable. Every other descendant—raw item, generated view, archive, dataset, and template—is structured-only and reachable through `plan_memory`, not ordinary `ask_memory`.

This avoids turning a backlog into thousands of semantic-memory hits. Bootstrap and the skill route planning questions to `plan_memory`; durable conclusions derived from Planning belong in compiled Notes with links to the collection. Recall freshness changes for a Planning manifest but not for raw-item edits; generic identity/resolver freshness still observes every direct edit. Reconciliation prunes stale semantic rows without deleting canonical Planning files or item-reference state.

Alternative rejected: index active or high-priority work items individually. It looks convenient at small scale but creates unstable ranking, governance complexity, and raw-work flooding. A future governed materialized summary can be considered separately.

### 12. Core guidance, not a thin command-alias skill

The hand-authored generic skill scaffold, bootstrap, product model, capability docs, and relevant knowledge packs teach natural Planning verbs and the Planning/Records/OpenSpec boundary. Packs may suggest fields, views, templates, and review cadences; selecting a pack creates nothing and never forks core semantics.

This change does not add an `exomem-planning` workflow skill that merely repeats command arguments. A future workflow skill is justified only for a coherent job such as weekly/quarterly review or collection adoption. Ordinary capture and query stay in the core contract.

## Risks / Trade-offs

- **[Profile-neutral extraction could regress Records]** → Keep Records facades and serialized contracts exact; add byte/response compatibility tests before Planning implementation and run the full Records acceptance suite throughout.
- **[Planning semantics become an inflexible universal taxonomy]** → Keep only four structural kinds and a small reserved core; allow domain fields, tags, templates, and saved views through manifests and packs.
- **[Horizon and dates disagree]** → Treat horizon as explicit intent and dates as independent constraints; validate date order, never infer or silently rebucket, and expose both in queries.
- **[Hierarchy validation leaks hidden targets]** → Authorize target paths before parsing identity/kind; use indistinguishable missing/invalid refusal shapes and pre-reduction tests.
- **[Direct edits break hierarchy or audit history]** → Preserve the edit as canonical, surface bounded inspection findings, and refuse collection mutations until a human restores a uniquely parseable valid state; do not build a second permissive repair parser in this MVP.
- **[A Planning command becomes a task executor]** → Keep execution links opaque and mutations explicitly user/agent initiated; no background synchronization, reminders, auto-priority, or task-level repo mirroring.
- **[Multi-horizon views are mistaken for canonical truth]** → Return provenance-bearing derived output only; never persist or auto-promote views.
- **[Planning items disappear from ordinary recall]** → Keep manifests discoverable and teach intent routing; compile durable conclusions into Notes rather than indexing mutable work rows.
- **[Six actions feel like tool proliferation]** → Keep one command and one selector; triage compiles to the same mutation engine and exists because capture-to-commitment is a real user workflow.
- **[Dependencies or cross-collection planning are needed later]** → Stable references leave room for them, but first-delivery hierarchy stays parent-only and same-collection so validation, snapshots, and governance remain coherent.

## Migration Plan

1. Add failing compatibility tests around current Records manifests, item references, audit markers, query shapes, and command responses.
2. Extract profile-neutral identity, adapter, mutation, audit, query, and governance seams behind unchanged Records facades.
3. Add the Planning profile contract and strict `Knowledge Base/Planning/**` manifest/source placement validation.
4. Add Planning schema/semantic validators, reference parsing, relationship validation, guarded writers, query/view rendering, and direct-edit inspection.
5. Register `plan_memory` once and regenerate MCP/REST/CLI/capability artifacts deliberately.
6. Extend recall policy, freshness/reconciliation, bootstrap, scaffold, packs, and documentation.
7. Exercise software and non-software fixtures through generated public surfaces and the installed-wheel product loop.
8. Deploy additively. Existing vaults change only when a user explicitly creates or hand-authors a Planning manifest and items.

Rollback removes the Planning command/profile and optional Planning manifests remain ordinary readable Markdown. Existing Records behavior and canonical state remain unchanged. No hidden state is required to recover either profile.

## Open Questions

No unresolved decision blocks implementation. Planned-versus-recorded Review, recursive progress rollups, capacity, cross-collection hierarchy, automatic repository reconciliation, persisted summaries, forms/charts, dedicated TUI screens, and richer workflow skills are explicit follow-up changes rather than open requirements in this delivery.
