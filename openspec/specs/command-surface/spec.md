# command-surface Specification

## Purpose
Keep every operation defined once instead of once per surface: a single
declarative command registry generates the MCP tools, the REST facade, the
OpenAPI document, and the CLI, so adding or removing an operation requires no
per-surface code and MCP tool schemas stay byte-identical to their committed
baseline. The CLI and REST facade share one result/error envelope so a given
failure carries the same machine-readable code on both surfaces.
## Requirements
### Requirement: Single Command Registry Generates Every Surface

The system SHALL define a single declarative command registry (`commands.py`) that enumerates each operation with its name, leaf function, description, parameter specs, and exposed surfaces, and the MCP tools, the REST facade, the OpenAPI document, and the CLI SHALL all be generated from it. No surface may maintain its own separate list of operations. The governed entity-type registry save SHALL be exposed by mirroring the existing relation-registry save command as operation `save-entity-types`, with the same validate-first proposal, rationale, and expected-hash argument shape.

#### Scenario: One entry exposes an op everywhere

- **WHEN** a new operation is added as a single registry entry with surfaces `{mcp, rest, cli}`
- **THEN** its MCP tool, its `/api/<name>` REST route, its OpenAPI path, and its `kb <name>` CLI subcommand all exist with no further per-surface edits
- **AND** removing the entry removes it from all surfaces

#### Scenario: Governed entity type save mirrors relation save

- **WHEN** a client invokes `save-entity-types` with `proposal`, `why`, and `expected_hash`
- **THEN** the shared command validates before saving through the entity registry leaf
- **AND** all generated surfaces expose the same parameters and result envelope

#### Scenario: One product entry exposes an op everywhere
- **WHEN** a new product operation is added as a single product registry entry
  with surfaces `{mcp, rest, cli}`
- **THEN** its MCP tool, its `/api/<name>` REST route, its OpenAPI path, and its
  `exomem <name>` CLI subcommand all exist with no further per-surface edits
- **AND** removing the product entry removes it from all generated public
  surfaces
- **AND** the product entry can still call one or more canonical implementation
  leaves internally

#### Scenario: Primary tools are discoverable without hiding advanced tools
- **WHEN** an agent reads the bootstrap contract or generated tool metadata
- **THEN** it can identify the primary front-door operations for save, adopt,
  ask, prove, review, update, and connect
- **AND** advanced typed/file operations remain available for agents that need
  precise control

### Requirement: MCP Tools Are Generated With Byte-Identical Fidelity

The MCP tools SHALL be generated from the registry via a `bind_vault` helper that presents each leaf's signature (minus the injected `vault_root`) and the registry description to the MCP framework. A snapshot test SHALL assert each generated tool's input-schema and description are byte-identical to a committed baseline of the current tools, so intentional contract changes are explicit. `entity_type` on `create-entity` and `resolve-entity` SHALL be a free string described as a stable ID from the active core-plus-vault registry, and runtime validation SHALL reject unknown values with `ENTITY_TYPE_UNKNOWN` naming active IDs. Any tool that cannot match SHALL remain hand-registered and be named in an explicit exceptions list.

#### Scenario: Generated tool matches the baseline exactly

- **WHEN** the schema-fidelity snapshot test runs over a registry-generated tool
- **THEN** its input-schema and description equal the committed baseline byte-for-byte
- **AND** the test fails if any generated tool's schema or description differs

#### Scenario: Vault-defined entity type is representable

- **WHEN** a vault defines active entity type `place`
- **THEN** `create-entity` and `resolve-entity` accept `entity_type="place"` despite the static schema carrying no per-vault enum
- **AND** an unknown value fails with `ENTITY_TYPE_UNKNOWN` naming the active IDs

#### Scenario: Non-matching tool is an explicit exception

- **WHEN** a tool cannot be generated with a matching schema
- **THEN** it stays hand-registered and appears in the exceptions list
- **AND** the snapshot test asserts the exceptions list is explicit, with no silently-skipped tool

#### Scenario: Generated product tool matches the baseline exactly
- **WHEN** the schema-fidelity snapshot test runs over a registry-generated
  product tool
- **THEN** its input schema and description equal the committed product baseline
  byte-for-byte
- **AND** the test fails if any generated product tool's schema or description
  differs

#### Scenario: Primitive tools are not the default MCP surface
- **WHEN** the MCP server is built with default settings
- **THEN** canonical primitive names such as `find`, `note`, `add`, `preserve`,
  `audit`, `reconcile`, and tier-2 file primitives are not registered as default
  public MCP tools
- **AND** equivalent capability is reachable through product commands

### Requirement: REST Facade And OpenAPI Derive From The Registry

The REST facade SHALL register an `/api/<name>` POST route for every product
registry operation exposed on `rest`, via one generic handler (auth gate -> JSON
body -> coerced product kwargs -> product command call -> envelope), and the
OpenAPI document SHALL be generated from the product registry's parameter specs.
REST SHALL expose the same product command contract as MCP and CLI by default.

#### Scenario: Product routes replace primitive public routes

- **WHEN** the registry-driven facade is built
- **THEN** product routes such as `/api/ask_memory`, `/api/remember`,
  `/api/capture_source`, `/api/preserve_evidence`, `/api/review_memory`, and
  `/api/maintain_memory` exist
- **AND** each route calls the same product command implementation used by MCP
  and CLI

#### Scenario: Existing routes preserved, missing ones added

- **WHEN** the registry-driven facade is built
- **THEN** the previously hand-wired routes (find, get, note, add, edit, audit, reconcile,
  list_directory, suggest_links) still exist at the same paths calling the same leaves
- **AND** operations that previously lacked a route (e.g. replace, link, provenance_report) now have
  one because they are in the registry with `rest`

#### Scenario: OpenAPI documents product parameters

- **WHEN** `GET /api/openapi.json` is requested
- **THEN** each path's request schema lists the product operation's actual
  parameters from the product registry
- **AND** no separate hand-maintained operation list exists to drift

#### Scenario: OpenAPI documents real parameters

- **WHEN** `GET /api/openapi.json` is requested
- **THEN** each path's request schema lists the operation's actual parameters from the registry
- **AND** no separate hand-maintained operation list exists to drift

### Requirement: A First-Class CLI Over All Operations

The system SHALL ship console-script entry points `exomem` and any configured
alias that expose every product registry op on the `cli` surface as a verb-first
subcommand, with positional args for params marked positional and `--flags` for
the rest. It SHALL support a global `--json` structured envelope, emit
structured error codes with remediation, and return exit code 0 on success, 1 on
operation error, and 2 on usage/argument error.

#### Scenario: Query the KB from the terminal

- **WHEN** `exomem ask-memory "carbonation rig" --json` is run
- **THEN** the product search runs against the local vault and prints a
  single-line envelope `{success: true, data: ...}`, exit code 0

#### Scenario: Write from the CLI and usage errors

- **WHEN** `exomem remember --type insight --title "..." --content "..."` is run
  against a temp vault
- **THEN** the note is created through the same product command used by MCP and
  REST
- **AND** running any product op with a missing required argument prints
  `Error [..]: ...` and exits 2

### Requirement: Shared Result And Error Envelope

The CLI (`--json` mode) and the REST facade SHALL use one shared product-command
envelope shape: `{success, data, error: {code, message, remediation}}`. A
success carries `data` with `success:true` and no `error`; a failure carries
`success:false` and an `error` block with a stable, machine-readable `code`. The
REST binary-blob guard for text fields SHALL be preserved.

#### Scenario: Same logical failure, same code on both surfaces

- **WHEN** a product operation fails validation in REST and in CLI `--json` mode
- **THEN** both return `{success: false, error: {code, message, remediation}}`
  with the same `code`

#### Scenario: Binary-blob guard preserved

- **WHEN** a REST request passes an oversized base64 blob in a text field
- **THEN** it is rejected with the existing `BINARY_BLOB_REJECTED`-class error,
  as before

### Requirement: Bootstrap Is Exposed On Every Generated Surface
The system SHALL expose `bootstrap` through the single command registry on MCP,
REST, CLI, and OpenAPI. The tool SHALL be marked read-only and non-destructive in
MCP annotations.

#### Scenario: Bootstrap appears in generated surfaces
- **WHEN** the server is built
- **THEN** `bootstrap` appears in the MCP tool list
- **AND** `/api/bootstrap` appears in the REST facade and OpenAPI document
- **AND** the CLI exposes a `bootstrap` subcommand

#### Scenario: Bootstrap is accounted for by schema fidelity tests
- **WHEN** the MCP schema fidelity test runs
- **THEN** the live tool set includes `bootstrap`
- **AND** `bootstrap` is registry-generated rather than a hand-registered exception

### Requirement: Adoption Studio Is A Single Registered Product Command

The system SHALL expose Adoption Studio as one product command, `adoption_studio`, added as a single `_PRODUCT_SPEC` registry entry so its MCP tool, its `/api/adoption_studio` REST route, its OpenAPI path, and its CLI subcommand are all generated with no per-surface code. The command SHALL multiplex ten actions on a required `action` selector — `start`, `status`, `select`, `plan`, `apply`, `cancel`, `finish`, `work-item`, `propose`, `apply-proposal` — dispatching to the run engine and the proposal engine, and SHALL re-raise engine errors as the shared `{code}: {reason}` envelope. Its registry `routes` metadata SHALL reference the existing canonical `adopt` leaf so `validate_product_registry()` passes, and it SHALL be marked `first_run_safe` because its default read is safe and `start` is explicitly guarded.

#### Scenario: One entry exposes adoption on every surface

- **WHEN** the server is built from the registry
- **THEN** `adoption_studio` appears in the MCP tool list, `/api/adoption_studio` exists in the REST facade and OpenAPI document, and the CLI exposes an `adoption-studio` subcommand
- **AND** `validate_product_registry()` passes because the entry's route references the canonical `adopt` leaf

#### Scenario: An unknown action is rejected

- **WHEN** `adoption_studio` is invoked with an `action` outside the ten defined actions
- **THEN** it is refused with an `INVALID_MODE`-class error naming the valid actions and writes nothing

### Requirement: Adoption Read-Only Actions Are Classified For Lease And Hosted Admission

`invocation_is_read_only` SHALL classify `adoption_studio` invocations by resolving the `action` selector, returning read-only ONLY for `status` and `work-item` and treating every other action as a mutation. Read-only actions SHALL therefore run lease-free and without an idempotency key, while mutating actions SHALL route through `writer_lease.invoke_command` with implicit MCP retry replay. This one classification SHALL serve as both the local lease decision and the hosted read/write admission decision, requiring no bespoke hosted routing for the command.

#### Scenario: Only status and work-item are read-only

- **WHEN** `invocation_is_read_only` is evaluated for `adoption_studio` with each action, including the omitted-versus-explicit `action` selector
- **THEN** it returns true only for `status` and `work-item`
- **AND** all other actions are treated as mutations and acquire the writer lease

### Requirement: Existing Review Verbs Dispatch Adoption Refs

`review_memory`, `triage_memory`, and `review_item_context` SHALL each dispatch adoption-namespaced work rather than growing parallel review machinery: `review_memory(mode="adoption")` SHALL return the per-run grouped adoption proposal queue, `triage_memory` SHALL route an `exomem://review/adoption/<id>` ref to the adoption triage path before the relation dispatch, and `review_item_context` SHALL route an adoption ref to the adoption context assembler before the default review-context path. These docstring and behavior changes SHALL be reflected as an intentional, explicitly-noted regeneration of the golden MCP schema fixture for exactly `adoption_studio`, `review_memory`, `triage_memory`, and `review_item_context`.

#### Scenario: Review verbs route adoption refs correctly

- **WHEN** `review_memory(mode="adoption")` is called and when an adoption ref is passed to `triage_memory` and `review_item_context`
- **THEN** each returns the adoption-specific result, and non-adoption modes and refs behave exactly as before

#### Scenario: Golden schema regeneration is intentional and bounded

- **WHEN** the MCP schema-fidelity test runs after the change
- **THEN** the only tools whose committed baseline changed are `adoption_studio` (new), `review_memory`, `triage_memory`, and `review_item_context`
- **AND** the regenerated fixture is committed with an explicit intentional-change note and the gate passes

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

### Requirement: Technical gap commands preserve registry parity
The product registry SHALL expose `schema_memory`, stable-reference parameters and response fields, `connect_memory(operation="context")`, and `maintain_memory(mode="backfill-ids")` consistently across MCP, REST, CLI, OpenAPI, generated capability docs, and schema-fidelity tests.

#### Scenario: One registry exposes every new route
- **WHEN** generated surfaces are inspected
- **THEN** the new command and modes are present with identical parameter semantics and no hand-maintained duplicate implementation

### Requirement: Paths and references coexist
Commands that accept governed page identifiers SHALL resolve paths and canonical references through one shared resolver and SHALL return both `path` and `ref` where they identify a durable governed artifact.

#### Scenario: Surface responses carry durable identity
- **WHEN** a source, note, entity, or evidence sidecar is created through MCP, REST, or CLI
- **THEN** each surface reports the same vault-relative path and canonical reference

### Requirement: Media runtime appears in resource diagnostics
`exomem status --resources` SHALL include a stable media-runtime object with queue counts,
worker-active state, worker PID when safely known, idle timeout, and job-store health. Doctor
SHALL report blocked media jobs and profile remediation without loading a model.

#### Scenario: JSON status remains no-allocation
- **WHEN** resource status is requested before any media job has run
- **THEN** it reports the media runtime and semantic deferred-work state
- **AND** it does not create a job DB, load a model, or initialize an accelerator

#### Scenario: Doctor finds blocked jobs
- **WHEN** durable media jobs are blocked by a missing optional engine
- **THEN** doctor reports a warning with the blocked count and remediation
- **AND** the overall core service can still pass readiness

### Requirement: Command Registry Carries Simple Action Metadata
The command registry SHALL expose enough metadata to derive the simple product
action catalog without maintaining a separate operation list. That product
metadata SHALL mark tools as primary or advanced, map simple user actions to
typed tools, and provide pack-aware guidance for first-run and selected-pack
workflows without duplicating business logic across MCP, REST, and CLI surfaces.

#### Scenario: Action metadata is registry-derived
- **WHEN** the product action catalog is built
- **THEN** it derives command routes from registry metadata
- **AND** canonical commands remain available on their original MCP, REST, and CLI surfaces

#### Scenario: Advanced tools remain discoverable
- **WHEN** a tool is not part of the primary simple action flow
- **THEN** it remains listed as advanced rather than hidden or removed
- **AND** tier-2 and destructive-operation controls continue to apply

#### Scenario: Front-door metadata is pack-aware
- **WHEN** bootstrap or documentation renders the product front door
- **THEN** each simple action maps to typed tools
- **AND** the response can include selected-pack workflows and agent
  instructions
- **AND** advanced tools remain visible but secondary

#### Scenario: Typed tools remain authoritative
- **WHEN** an agent follows a simple action such as save, ask, prove, review,
  update, adopt, or connect
- **THEN** the actual operation still routes through the existing typed tool
  contracts
- **AND** pack metadata never bypasses write governance

### Requirement: CLI Exposes Simple Aliases Without Breaking Canonical Commands
The CLI SHALL expose simple action aliases for common workflows while preserving every canonical registry subcommand.

#### Scenario: Simple alias and canonical command both work
- **WHEN** a user invokes a supported simple CLI action
- **THEN** the action routes to the same canonical leaf used by the equivalent registry command
- **AND** invoking the canonical command directly still works with the same behavior as before

#### Scenario: JSON envelopes stay consistent
- **WHEN** a user invokes a simple CLI action in JSON mode
- **THEN** the output uses the same success/error envelope semantics as canonical CLI operations
- **AND** failures carry stable error codes and remediation where the canonical operation provides them

### Requirement: Relation governance preserves registry-generated surface parity
The product registry SHALL expose relation registry and traversal-profile
infer/validate/diff/save behavior through the schema-governance command, and
traversal profile selection through the context command, with identical
parameters and error semantics across MCP, REST, CLI, OpenAPI, generated docs,
annotations, and schema-fidelity fixtures. Existing schema/context calls SHALL
remain compatible when the new subject/profile parameters are omitted.

#### Scenario: One registry definition exposes relation governance everywhere
- **WHEN** the generated surfaces are inspected after this change
- **THEN** schema governance accepts relation/profile subjects and reviewed
  proposals, context accepts traversal profiles, and every surface exposes the
  same defaults, bounds, hash guards, and validation codes

#### Scenario: Existing callers retain prior behavior
- **WHEN** callers omit relation-governance subjects and traversal profiles
- **THEN** existing schema contract behavior and broad context traversal remain
  unchanged

### Requirement: Filename Slug Is Consistent Across Generated Surfaces

Every public create command whose shared leaf accepts an optional filename `slug` SHALL expose that same optional input through MCP, REST/OpenAPI, and CLI without duplicating slug validation or title behavior in a surface adapter.

#### Scenario: Product command uses explicit slug through REST

- **WHEN** a REST caller creates a titled page with an explicit valid slug
- **THEN** the shared leaf creates the same path and stored Unicode title as an equivalent MCP or CLI call

#### Scenario: Surface validation returns the shared error

- **WHEN** any generated surface receives an invalid explicit slug
- **THEN** it returns the shared leaf's stable validation error and no page is written

### Requirement: Process Media Is Exposed On Every Generated Surface
The single command registry SHALL define `process_media` once and expose the same process/status/retry contract through MCP, `/api/process_media`, OpenAPI, and the `kb`/`exomem` CLI. All surfaces SHALL call the same orchestration leaf and return the shared result/error envelope.

#### Scenario: One registry entry exposes process media everywhere
- **WHEN** the command registry is built
- **THEN** `process_media` appears in MCP, REST, OpenAPI, and CLI without per-surface business logic

#### Scenario: Process one artifact
- **WHEN** a caller invokes `process_media` with `operation=process` and a supported governed path
- **THEN** the response reports its canonical sidecar and durable pending/completed state without waiting for ASR

#### Scenario: Inspect actionable status
- **WHEN** a caller invokes `process_media` with `operation=status`
- **THEN** the response includes aggregate counts and bounded per-artifact paths, attempts, errors, retryability, and next actions

#### Scenario: Retry failed media
- **WHEN** a caller invokes `process_media` with `operation=retry` and an optional artifact path
- **THEN** retryable matching work returns to pending and the response reports the number requeued

### Requirement: Edit Memory Advertises One Discriminated Operation

The public `edit_memory` command SHALL remain one tool and SHALL advertise a required nested operation discriminated by `kind`. Supported variants MUST preserve current behavior for `replace_body`, `replace_tags`, `replace_string`, `batch_replace`, `edit_section`, `patch_frontmatter`, and `fill_row`. Each variant SHALL forbid unrelated fields and expose only guards the underlying operation enforces.

#### Scenario: Agent selects replace string
- **WHEN** the client inspects the `replace_string` variant
- **THEN** it sees the exact old/new string, replace-all, supported tag composition, drift guard, and preview fields
- **AND** it does not see frontmatter, row, section, batch, or whole-body fields

#### Scenario: Invalid variant fields are submitted
- **WHEN** a caller adds a field belonging to another variant or omits a required variant field
- **THEN** validation fails before mutation with the selected kind and precise field guidance

### Requirement: Legacy Edit Calls Normalize Before Idempotency

For one compatibility release, the runtime SHALL accept the previous flat `edit_memory` arguments even though they are not part of the primary advertised schema. Exactly one legacy mode MUST be present. Both legacy and discriminated forms SHALL normalize to the same canonical operation before payload hashing, lease acquisition, and leaf invocation.

#### Scenario: Old and new clients retry the same edit
- **WHEN** equivalent flat and discriminated edit payloads use the same idempotency identity
- **THEN** they resolve one canonical payload digest and one leaf execution
- **AND** the retry returns the committed terminal rather than `IDEMPOTENCY_KEY_REUSED`

#### Scenario: Legacy modes are mixed
- **WHEN** a flat call supplies fields for multiple exclusive edit modes or combines flat fields with `operation`
- **THEN** it fails before mutation with an `INVALID_EDIT`-class error naming the conflict

### Requirement: Edit Schema Is Consistent With Runtime Acceptance

The MCP discovery schema and REST OpenAPI schema SHALL publish the discriminated primary shape, and black-box calls through those adapters SHALL prove each published variant reaches the existing edit leaf. The compatibility shim MUST be tested separately and marked deprecated with a one-release minimum.

#### Scenario: MCP schema and call agree
- **WHEN** the live FastMCP tool list is inspected and each operation variant is invoked against an isolated vault
- **THEN** the schema contains the discriminator and forbids unrelated fields
- **AND** every valid published call performs or previews exactly its selected edit mode

### Requirement: Authoring Descriptions Project The Canonical Contract

The single command registry SHALL project the canonical concise semantic-authoring
contract into the descriptions and applicable parameter guidance for `remember`,
`replace_memory`, `observe_memory`, `edit_memory` unit removal/activation, and the
create/overwrite/append behavior of `manage_memory_file`. MCP, REST, CLI help, OpenAPI, and generated capability
documentation SHALL inherit that registry text. No facade SHALL maintain a
separate authoring rule or omit the minimum-unit rule from a path that can create or
activate compiled notes.

#### Scenario: Remember content guidance is exact
- **WHEN** the generated `remember` schema or help is inspected
- **THEN** `content` guidance includes `## Observations`, `- [category] content #tags (context) ^anchor`, the open-category rule, the one-valid-unit active-note minimum, and the non-empty rich alternative

#### Scenario: Tier-2 guidance names shared enforcement
- **WHEN** the generated `manage_memory_file` create/overwrite/append description is inspected
- **THEN** it states that compiled-note destinations receive the same semantic precommit contract and points to the typed writer remediation

#### Scenario: Edit guidance covers removal and activation
- **WHEN** generated `edit_memory` guidance is inspected
- **THEN** it states that an edit cannot remove a post-activation page's final valid unit and that inactive-to-active transitions must satisfy the minimum

#### Scenario: Surface fidelity detects drift
- **WHEN** MCP descriptions, CLI help, REST/OpenAPI schemas, capability docs, and committed schema fixtures are compared with the registry
- **THEN** any missing, stale, or independently edited authoring contract fails the existing surface-fidelity gates

### Requirement: Semantic Authoring Failures Preserve One Envelope

All public facades SHALL preserve semantic-authoring validation failures from the
shared writer using the existing result/error envelope. Stable code,
source-addressed findings where available, canonical remediation, validation-only
state, and mutation status SHALL survive without facade-specific rewriting.

#### Scenario: Missing semantic unit fails identically
- **WHEN** the same active note with no valid compact or rich unit is submitted through MCP, REST, and CLI JSON
- **THEN** each response carries `missing_semantic_unit`, canonical compact/rich remediation, and an explicit non-mutated result

#### Scenario: Empty rich unit fails identically
- **WHEN** an applicable in-process write contains an empty recognized rich block
- **THEN** every facade carries `empty_rich_unit` with its heading location and no facade commits or indexes that unit

### Requirement: Expected MCP Operation Failures Use Application Envelopes

The generated MCP command wrapper SHALL return deliberate public operation failures as normal tool content with top-level `success:false` and the shared stable error envelope. It MUST preserve public error details and MUST NOT expose these outcomes as MCP execution failures. Exceptions outside the deliberate public operation-error contract MUST continue through the native MCP error path.

#### Scenario: Busy mutation is retryable application data

- **WHEN** a mutation raises public `MUTATION_BUSY` before commit
- **THEN** the MCP tool result is not protocol `isError`
- **AND** its content reports `success:false`, code `MUTATION_BUSY`, `status:retryable`, `committed:false`, retry guidance, request ID, and receipt ID

#### Scenario: Read remains callable after repeated busy outcomes

- **WHEN** the same MCP effective retry scope and idempotency store receive repeated structured busy outcomes for the same canonical command payload
- **THEN** a subsequent read-only command can execute normally
- **AND** retrying the original mutation with the same identity cannot create a duplicate commit

#### Scenario: Receipt is not a cross-session replay key

- **WHEN** a caller starts a new session without a transferable explicit idempotency identity
- **THEN** the prior receipt remains diagnostic rather than caller-supplied replay authority
- **AND** the client follows reconciliation guidance instead of assuming cross-session duplicate suppression

#### Scenario: Unexpected exception remains a tool failure

- **WHEN** a command raises an unexpected exception that is not a public operation error
- **THEN** the wrapper preserves FastMCP's native execution-error behavior

### Requirement: Validation-Only Replacement Is Read-Only

`replace_memory(validate_only=true)` SHALL run as an advisory weak-snapshot preview without acquiring the writer lease, mutation boundary, or mutation idempotency receipt. It MUST identify itself as validation-only and non-committed and bind the exact draft plus relevant predecessor inputs. The eventual non-preview replacement MUST remain a mutation and MUST freshly revalidate its predecessor, draft, writer, and corpus-dependent preconditions under mutation authority.

#### Scenario: Replacement preview overlaps another writer

- **WHEN** another process holds the vault mutation boundary and `replace_memory(validate_only=true)` is invoked
- **THEN** the preview returns its advisory hash-bound proposal without `MUTATION_BUSY`
- **AND** no mutation receipt or vault write is created

#### Scenario: Replacement commit still serializes

- **WHEN** the same replacement is invoked without validation-only mode
- **THEN** it acquires the normal writer lease and process-safe boundary before committing

### Requirement: Semantic Authoring Guidance Is Projected Consistently And Bounded

Every generated surface that teaches or performs semantic writes SHALL project the same semantic authoring contract identity, short role-first selection rule, one compact example, and route to full bootstrap guidance. Full bootstrap, the public reference, generic scaffold, and workflow skills SHALL project the complete core vocabulary. Parity tests MUST cover contract identity and the appropriate bounded/full projection. Intentional description changes MUST be regenerated and reviewed as a bounded fixture change.

#### Scenario: Generated schemas stay small and cannot drift

- **WHEN** surface projection tests run
- **THEN** MCP, REST/OpenAPI, and CLI write surfaces identify the same contract version and short selection rule without duplicating the full sixteen-label table
- **AND** bootstrap/reference/skill projections expose the exact complete vocabulary under the same contract identity

### Requirement: Semantic Writes Echo Category Resolution

Write results exposed through MCP, REST, and CLI SHALL carry the same bounded category-resolution feedback from the shared leaf operation. A surface MUST NOT invent, suppress, or reinterpret category advice independently.

#### Scenario: Alias advice is identical across surfaces

- **WHEN** equivalent deterministic fixtures invoke a semantic write using a category alias through MCP, REST, and CLI
- **THEN** each result contains the same category-feedback fields, canonical category, advisory status, and omission count
- **AND** generated identifiers that are unrelated to category resolution are excluded from the parity assertion

### Requirement: Self-Describing Reviewed-None Validation

Creation validation responses SHALL expose an additive `relation_review_hash`. When a
reviewed-none decision is required it MUST equal the exact `draft_hash` covered by the
commit; otherwise it SHALL be null. Existing hash fields and their semantics MUST remain
available across MCP, REST, CLI, and direct leaves. Replacement previews MUST rewrite both
public fields to the same predecessor-bound hash required by replacement commit.

#### Scenario: Zero-candidate validation returns the commit hash

- **WHEN** a new compiled note has no qualifying relation and validation requires an
  explicit reviewed-none decision
- **THEN** the response contains `relation_review_hash == draft_hash`
- **AND** the response remains non-mutating

#### Scenario: Reviewed-none is not required

- **WHEN** creation validation is satisfied by a qualifying relation
- **THEN** `relation_review_hash` is null
- **AND** existing validation fields remain unchanged

### Requirement: Reviewed-None Compatibility Alias

The public semantic-write boundary SHALL accept both `reviewed_none` and the previously
advertised `reviewed-none` as input, canonicalize both to `reviewed_none` before hash,
reason, writer-lease idempotency digesting, replay, and persistence checks, and name the
accepted spellings when rejecting any other value. The alias MUST NOT relax applicability,
unchanged-draft, hash-match, reason, or replay requirements.

#### Scenario: Advertised hyphen spelling reaches one canonical receipt

- **WHEN** a caller commits an unchanged reviewed-none draft using `reviewed-none`, the
  returned relation review hash, and a valid explicit reason
- **THEN** the commit succeeds under the same checks as `reviewed_none`
- **AND** durable review state stores only canonical `reviewed_none`

#### Scenario: Alias does not bypass review integrity

- **WHEN** either accepted spelling is supplied with a wrong hash, changed draft, missing
  reason, or inapplicable disposition
- **THEN** the existing semantic-contract error is returned
- **AND** no Markdown or auxiliary review state is written

#### Scenario: Alias and canonical spelling share one explicit replay identity

- **WHEN** the same valid mutation and explicit idempotency key are first sent with
  `reviewed-none` and retried with `reviewed_none`
- **THEN** writer-lease digesting treats both requests as the same canonical mutation
- **AND** the stored receipt remains underscore-only

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

### Requirement: Registry-owned MCP descriptor metadata

The command registry SHALL support immutable optional MCP descriptor metadata and the single generated MCP registration loop SHALL pass that metadata to FastMCP. A command requiring client-specific descriptor extensions MUST remain registry-generated unless its callable schema itself cannot be generated faithfully.

#### Scenario: File parameter metadata reaches discovery

- **WHEN** the generated `preserve_artifacts` MCP tool is listed
- **THEN** its descriptor contains `_meta["openai/fileParams"] == ["files"]`
- **AND** its input schema declares all four supported file properties with only `download_url` and `file_id` required
- **AND** `preserve_artifacts` is absent from the hand-registered exceptions list

#### Scenario: Commands without metadata remain unchanged

- **WHEN** a registry command does not declare MCP descriptor metadata
- **THEN** its generated descriptor and existing schema-fidelity baseline remain byte-identical

### Requirement: Artifact preservation appears in capability guidance

The product command surface SHALL describe `preserve_artifacts` as the canonical binary-evidence capability and `transfer_artifact` as its compatibility transport helper. Full bootstrap guidance SHALL provide copyable direct-file and fallback upload calls and SHALL distinguish obtaining an upload capability from successfully storing bytes.

#### Scenario: Full bootstrap teaches both transport paths

- **WHEN** a client requests the full bootstrap profile
- **THEN** the response shows a `preserve_artifacts(files=[...], scope=..., category=...)` call for clients with file handles
- **AND** it shows `transfer_artifact(operation="upload")` plus `/upload` delivery for clients without file handles
- **AND** neither example embeds binary base64 or a long-lived upload secret

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

### Requirement: Observe Memory Accepts Governed Unit Metadata
The single command registry SHALL expose `verdict`, `check_by`, and `id` on `observe_memory` consistently across MCP, REST, CLI, OpenAPI, and generated capability documentation. `verdict` and `check_by` SHALL require an explicit governed non-observation kind, because the compact form carries no metadata rows, and SHALL be refused with a stable machine-readable code otherwise. `id` SHALL set the unit's authored anchor, SHALL be validated against the existing anchor grammar, and SHALL be refused when it would collide with another unit's anchor on the same parent.

#### Scenario: Governed metadata reaches the rendered rich unit
- **WHEN** a caller adds a unit with kind `prediction`, `verdict` `refuted`, and `check_by` `2026-11-01`
- **THEN** the written rich block carries both metadata rows and the returned unit reports both values

#### Scenario: Governed metadata on a compact observation is refused
- **WHEN** a caller adds a unit with a `verdict` and no explicit governed non-observation kind
- **THEN** the call fails with a stable code stating that governed unit metadata requires the rich form

#### Scenario: An explicit anchor is honoured
- **WHEN** a caller adds a unit with `id` set to a valid anchor
- **THEN** the written unit uses that anchor and the returned unit reference ends with it

#### Scenario: A colliding anchor is refused
- **WHEN** a caller adds a unit with `id` set to an anchor another unit on the same page already uses
- **THEN** the call fails with a stable duplicate-anchor code

#### Scenario: The parameter is present on every generated surface
- **WHEN** the generated MCP, REST, CLI, and capability-documentation surfaces for `observe_memory` are inspected
- **THEN** all of them expose the same `verdict`, `check_by`, and `id` parameters

### Requirement: Observe Memory Reconstruction Preserves Unowned Metadata
`observe_memory` SHALL reconstruct a rich unit without discarding any authored leading metadata row it does not itself own. Rows for keys the command owns — category, id, tags, context, relations, verdict, and check_by — SHALL be re-emitted from the resolved arguments; every other authored row SHALL be preserved verbatim. The governed metadata arguments SHALL be preserve-by-default on update, where omitting an argument keeps the current value and passing an empty string clears it. The command's round-trip assertion SHALL cover the preserved rows, so a dropped row fails the write instead of committing silently.

#### Scenario: An unrelated content edit keeps the verdict
- **WHEN** a caller updates only the content of a rich unit that carries `- verdict: refuted`
- **THEN** the rewritten unit still carries that verdict row and the returned unit still reports it

#### Scenario: An unknown authored metadata row survives an update
- **WHEN** a caller updates only the content of a rich unit that carries an authored metadata row the parser does not interpret
- **THEN** the rewritten unit still carries that row verbatim

#### Scenario: An explicit empty value clears a governed key
- **WHEN** a caller updates a rich unit carrying `- verdict: refuted` and passes `verdict` as an empty string
- **THEN** the rewritten unit carries no verdict row

#### Scenario: A supplied value replaces the current one
- **WHEN** a caller updates a rich unit carrying `- verdict: inconclusive` and passes `verdict` as `confirmed`
- **THEN** the rewritten unit carries `- verdict: confirmed`

### Requirement: Compiled writes may return one advisory structural-promotion suggestion

When a compiled-note mutation commits, the system SHALL evaluate whether the written page now carries recurring durable material outside its own declared scope. When it does, the successful result SHALL include at most one `structure_suggestion`, carried on the existing commit result and projected into the default compact terminal, for every compiled writer: `remember`, `edit_memory`, `observe_memory`, and `replace_memory`.

The suggestion SHALL report `kind`, a `strength` of exactly `strong` or `moderate`, a deterministically ordered list of reason codes, the count of off-scope durable units, and a bounded list of the recurring terms that formed the group. It SHALL NOT report a numeric confidence, score, probability, or any other continuous quantity.

The suggestion is advisory. It SHALL NOT add a value to the closed set of keys a client branches on for mutation outcome, and it SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity, or replay behaviour. When no condition is detected the key SHALL be absent rather than null or empty.

#### Scenario: A diverged compiled page returns a suggestion in the default response

- **WHEN** a compiled page whose declared identity describes one subject accumulates recurring durable units describing a materially different subject, and a further compiled write commits
- **THEN** the write succeeds with its existing committed terminal unchanged
- **AND** the default compact response carries one `structure_suggestion` with `strength` of `strong` or `moderate`
- **AND** the suggestion names at least two reason codes in deterministic order
- **AND** the response reports no numeric confidence for the suggestion

#### Scenario: Every compiled writer can carry the suggestion

- **WHEN** the same diverged page is mutated through `remember`, `edit_memory`, `observe_memory`, or `replace_memory`
- **THEN** each committed result can carry the suggestion
- **AND** the suggestion has the same shape regardless of which writer produced it

#### Scenario: A coherent page returns no suggestion

- **WHEN** a compiled write commits to a page whose durable units remain within its declared scope
- **THEN** the response contains no `structure_suggestion` key

### Requirement: Structural detection is conservative and never triggered by size

The system SHALL require convergent evidence before emitting a suggestion. It SHALL emit nothing on a single signal, and it SHALL reserve `strong` for the case where every reason code holds.

Recurrence SHALL be established over durable semantic units rather than over write events, and the reported reason codes SHALL describe units accordingly. A term SHALL contribute to a group only when it recurs across more than one off-scope unit.

Raw page length, byte size, unit count alone, section count, and category variety SHALL NOT be sufficient to emit a suggestion. A long page whose durable units remain within its declared scope SHALL produce no suggestion regardless of its size.

#### Scenario: A long coherent research note stays quiet

- **WHEN** a compiled write commits to a large note containing many durable units, many categories, and many sections that all remain within one declared subject
- **THEN** no suggestion is emitted
- **AND** the outcome does not depend on the page's length

#### Scenario: One or two tangents do not trigger promotion

- **WHEN** an otherwise coherent page contains one or two durable units outside its declared scope
- **THEN** no suggestion is emitted, because no term recurs across enough off-scope units to form a group

#### Scenario: A single signal is insufficient

- **WHEN** fewer than two reason codes hold for a page
- **THEN** no suggestion is emitted

### Requirement: Structural detection is scoped to compiled knowledge

The system SHALL evaluate only compiled note pages. Sources, evidence, media artifacts, structured collections, planning items, navigational pages, and any page ineligible for recall SHALL NOT be analysed and SHALL NOT produce a suggestion.

A page whose declared identity announces deliberate breadth SHALL NOT be nagged toward further division. A page carrying the established hub or snapshot convention SHALL be excluded from analysis.

#### Scenario: Source and evidence artifacts never enter the advisory path

- **WHEN** a write commits to a source, evidence, or other non-compiled artifact, however large or heterogeneous
- **THEN** no structural analysis runs and no suggestion is emitted

#### Scenario: A deliberate hub is not told to split

- **WHEN** a compiled write commits to a page that declares itself a hub by the established convention
- **THEN** no suggestion is emitted

#### Scenario: Advice stops once the material is correctly routed

- **WHEN** the diverged material has been moved into a destination whose declared identity matches it, and further writes are directed there
- **THEN** neither that destination nor the original page emits a suggestion
- **AND** the silence follows from scope agreement rather than from any recorded dismissal

### Requirement: Structural suggestions never weaken a committed write

Structural analysis SHALL be failure-isolated. An exception, an unavailable optional signal, or an undecidable result SHALL cause the suggestion to be omitted, and SHALL NOT fail, delay, retry, roll back, or alter the committed mutation or its terminal.

The system SHALL NOT move, rename, split, retitle, rescope, create, or delete any page, project, or folder as a consequence of structural detection.

Existing write-latency requirements SHALL continue to hold unchanged, including the absolute commit ceilings and the bound on how commit cost may scale with page size.

#### Scenario: A detector failure leaves the write committed

- **WHEN** structural analysis raises during an otherwise successful compiled write
- **THEN** the mutation remains committed with its existing terminal
- **AND** no `structure_suggestion` is present
- **AND** no warning, error code, or retry is produced for the caller

#### Scenario: Detection never restructures the vault

- **WHEN** a suggestion is emitted at any strength
- **THEN** no page, project, or folder has been created, moved, renamed, or deleted by the system

### Requirement: Structural evidence is confined to the written page

Every fact reported in a suggestion SHALL be derived from the page named in the same response. The suggestion SHALL NOT include or allow inference of any other page's path, title, project, tags, or count, whether or not the caller is permitted to see that page.

#### Scenario: The payload discloses nothing about other pages

- **WHEN** a suggestion is emitted for a page in a vault containing pages the caller may not see
- **THEN** the payload contains no path, title, project, or count belonging to any page other than the one written

#### Scenario: The payload is bounded and deterministic

- **WHEN** the same page state is evaluated twice
- **THEN** the reason codes, counts, and recurring terms are identical and identically ordered
- **AND** at most one suggestion is returned, with its recurring-term list bounded to a small fixed maximum

### Requirement: Source kind is an open, extensible vocabulary

Source capture SHALL accept any source kind that normalizes to a safe canonical key, whether or not the product ships that key as a built-in. Acceptance SHALL NOT depend on a code release, a schema migration, or a prior administrative registration step.

The system SHALL normalize a supplied kind to one canonical key, resolve a registered alias to its canonical key, and accept an unregistered but valid key by canonicalizing it. An unregistered key SHALL be recorded in the vault's source-taxonomy registry as part of the same atomic write that captures the source, so a later capture of the same kind resolves as registered.

The system SHALL refuse a supplied kind only when it cannot be normalized into a safe canonical key, or when it is a near-miss of an already-known key. A near-miss refusal SHALL name the existing key it resembles so the caller can correct itself, and SHALL state how a deliberately similar new key can still be introduced.

Built-in kinds SHALL be generic and free of any user-specific identifier.

#### Scenario: A previously unseen meaningful kind is accepted

- **WHEN** a source is captured with a source kind the product has never seen, which normalizes to a valid canonical key
- **THEN** the capture succeeds
- **AND** the stored kind is the canonical form of the supplied value, not a fallback
- **AND** no code change, schema migration, or separate registration call was required
- **AND** the vault's source-taxonomy registry now carries that key

#### Scenario: A registered alias resolves to its canonical key

- **WHEN** a source is captured with a value registered as an alias of a canonical kind
- **THEN** the stored kind is the canonical key
- **AND** the destination is the one the canonical key projects to

#### Scenario: A near-miss of a known kind is refused with its correction

- **WHEN** a source is captured with a kind that differs from an already-known kind only by a small number of characters
- **THEN** the capture is refused
- **AND** the refusal names the known key it resembles
- **AND** the refusal states how a deliberately distinct new key can be introduced

### Requirement: Subject domain is an independent open vocabulary

Source capture SHALL accept an optional subject domain describing what the artifact is about, resolved through the same open-vocabulary rules as source kind and held on an axis independent of it. Any combination of a valid kind and a valid domain SHALL be permitted; neither axis SHALL constrain the values allowed on the other.

Domain SHALL be optional. A capture that supplies no domain SHALL succeed and SHALL be stored without one.

Built-in domains SHALL be generic and free of any user-specific identifier.

#### Scenario: A previously unseen meaningful domain is accepted

- **WHEN** a source is captured with a subject domain the product has never seen, which normalizes to a valid canonical key
- **THEN** the capture succeeds
- **AND** the stored domain is the canonical form of the supplied value
- **AND** no code change or schema migration was required

#### Scenario: Kind and domain vary independently

- **WHEN** sources are captured pairing the same kind with different domains, and the same domain with different kinds
- **THEN** every combination succeeds
- **AND** each stored source carries exactly the kind and domain supplied for it

#### Scenario: Domain may be omitted

- **WHEN** a source is captured with a kind but no domain
- **THEN** the capture succeeds
- **AND** the stored source declares no domain

### Requirement: Project association is separate, multi-valued, and never a storage constraint

Source capture SHALL accept zero or more project keys for one source, resolved through the existing project-key rules. Project association SHALL be independent of both source kind and subject domain, and SHALL NOT influence where the source is stored.

A single source SHALL be able to serve more than one project simultaneously without duplication. Provenance SHALL remain carried by the existing stable identity and reference fields rather than by project membership.

#### Scenario: One source serves several projects

- **WHEN** a source is captured naming more than one project key
- **THEN** the capture succeeds as a single source with a single stable identity
- **AND** every named project is recorded on it
- **AND** its location is unchanged from the same capture with no project named

#### Scenario: Project keys do not appear in the source location

- **WHEN** two sources sharing a kind and a domain are captured with different project keys
- **THEN** both are stored in the same location
- **AND** neither location contains a project key

### Requirement: The source location is a deterministic projection of canonical semantic metadata

The system SHALL derive a captured source's location from its canonical kind and, when present, its canonical domain. The semantic metadata SHALL be authoritative and the location SHALL be a projection of it; the directory structure SHALL NOT be the classification model.

The projection SHALL distinguish a canonical machine key from its human-facing path segment, so a canonical key and the segment it projects to may differ. A path segment SHALL be derived only from an already-validated canonical key, never from raw caller input. When the registry declares no path segment for a key, the system SHALL derive one deterministically from that canonical key.

The projection SHALL omit the domain level when no domain is present, and SHALL produce at most two levels beneath the source root. The same canonical metadata SHALL always project to the same location.

The system SHALL NOT require or enforce agreement between an already-captured source's location and its recorded metadata.

#### Scenario: A research report about travel is not stored under the fallback

- **WHEN** a source is captured with a research-report kind and a travel domain
- **THEN** the capture succeeds
- **AND** its location is not the fallback location or any descendant of it
- **AND** its location reflects both the kind and the domain

#### Scenario: A canonical key and its path segment may differ

- **WHEN** a kind whose registered path segment differs from its canonical key is captured
- **THEN** the stored metadata carries the canonical key
- **AND** the location carries the registered path segment

#### Scenario: An omitted domain omits a level

- **WHEN** the same kind is captured once with a domain and once without
- **THEN** the capture with a domain is stored one level deeper than the capture without
- **AND** neither location exceeds two levels beneath the source root

#### Scenario: Projection is deterministic

- **WHEN** two sources are captured with identical canonical kind and domain
- **THEN** both resolve to the same directory

#### Scenario: An already-captured source is not required to match the projection

- **WHEN** a source exists whose location does not match what its recorded metadata would project to today
- **THEN** it remains valid, readable, and retrievable
- **AND** no error, finding, or warning is raised about the mismatch
- **AND** it is not moved

### Requirement: Open vocabulary does not weaken filesystem safety

The system SHALL reject or normalize any supplied kind or domain that could otherwise influence the stored location as path input. Path traversal segments, absolute paths, drive-qualified paths, network share paths, embedded path separators, bare or repeated dot segments, trailing dots and spaces, control characters, and values that normalize to nothing SHALL NOT reach a path segment.

The system SHALL refuse a canonical key that would project to a filesystem-reserved device name, and SHALL refuse to register a new key whose path segment would collide case-insensitively with that of a different existing key.

The system SHALL bound the length of a canonical key.

#### Scenario: Traversal and absolute path forms never become a path segment

- **WHEN** a capture supplies a kind or domain containing traversal segments, a leading separator, a drive qualifier, a network share prefix, an embedded separator in either direction, a bare dot, or a repeated dot
- **THEN** the value is either refused or normalized to a safe canonical key
- **AND** the resulting location remains beneath the source root
- **AND** no supplied separator or dot segment appears as a path segment

#### Scenario: Reserved device names are refused

- **WHEN** a capture supplies a kind or domain whose canonical key names a filesystem-reserved device
- **THEN** the capture is refused with a remediation message

#### Scenario: Colliding path segments are refused

- **WHEN** registering a new canonical key would produce a path segment differing from an existing key's segment only by letter case
- **THEN** the registration is refused

#### Scenario: Degenerate and oversized values are refused

- **WHEN** a capture supplies a kind or domain that is empty, consists only of characters that normalize away, or exceeds the canonical key length bound
- **THEN** the capture is refused rather than stored under a fallback

### Requirement: The fallback kind means low confidence, never missing vocabulary

The system SHALL treat the `other` kind as a low-confidence classification. A supplied kind that resolves to a safe canonical key SHALL NOT be recorded or routed as `other` on the grounds that the key was not previously known.

A capture that supplies no kind SHALL remain permitted and SHALL resolve to `other`, so classification is never a precondition for preserving material.

No capture surface SHALL publish the fallback as its default classification argument. Every surface through which a source can be captured SHALL be able to express kind, domain, and project association; a surface that can only express the fallback reproduces the defect this capability removes.

#### Scenario: No capture surface defaults to the fallback

- **WHEN** a source is captured through any supported capture surface without a kind argument
- **THEN** no fallback value was supplied on the caller's behalf by that surface's defaults
- **AND** that surface accepts a kind, a domain, and project keys when the caller has them

#### Scenario: A confidently supplied unknown kind is never demoted

- **WHEN** a source is captured with a meaningful kind the product has never seen
- **THEN** the stored kind is that kind
- **AND** neither the stored kind nor the location is the fallback

#### Scenario: An unclassified capture still succeeds

- **WHEN** a source is captured with no kind supplied
- **THEN** the capture succeeds and resolves to the fallback kind
- **AND** no classification argument was required

### Requirement: Capture may return one advisory source-classification suggestion

When a capture resolves to the fallback kind while carrying evidence that a real kind exists, the successful result SHALL include at most one bounded advisory suggestion, reported through the same advisory-suggestion channel already used for structural advice and distinguished by its own kind value.

The suggestion SHALL report a `strength` of exactly `strong` or `moderate` and a deterministically ordered list of reason codes. It SHALL NOT report a numeric confidence, score, or probability.

Detection SHALL be deterministic and local. It SHALL NOT perform a model call, a network call, or a whole-corpus scan, and it SHALL NOT introduce persistent state. The suggestion is advisory: any detection failure, refusal, or absent optional state SHALL leave the committed capture, its location, and its existing result keys unchanged. When no condition is detected the key SHALL be absent rather than null or empty.

Material that carries the fallback kind as an internal marker rather than as a user classification SHALL NOT be analysed and SHALL NOT produce a suggestion.

The suggestion SHALL reach the caller through the committed-mutation response, and the response layer SHALL re-validate it against bounds declared for its own kind rather than forwarding an unvalidated payload. A suggestion whose payload does not satisfy those bounds SHALL be dropped rather than widening the response contract.

#### Scenario: The suggestion reaches the caller through the committed response

- **WHEN** a capture that produces a classification suggestion commits
- **THEN** the caller's committed response carries that suggestion with its kind, strength, reason codes, domain, and fallback count
- **AND** the response does not carry payload fields belonging to a different advisory kind

#### Scenario: A malformed classification suggestion is dropped, not forwarded

- **WHEN** a classification suggestion whose payload violates the bounds declared for its kind reaches the response layer
- **THEN** the committed response omits the suggestion entirely
- **AND** the capture itself is unaffected

#### Scenario: A recurring fallback pattern in one domain suggests a real kind

- **WHEN** several sources have been captured with the fallback kind and the same domain, and a further such capture commits
- **THEN** the capture succeeds unchanged
- **AND** the result carries one advisory classification suggestion of `strong` strength
- **AND** the suggestion names its reason codes in deterministic order
- **AND** the suggestion reports no numeric confidence

#### Scenario: A single unusual fallback capture stays quiet

- **WHEN** one source is captured with the fallback kind and no domain, and no comparable prior capture exists
- **THEN** the result carries no classification suggestion

#### Scenario: A coherently classified capture stays quiet

- **WHEN** a source is captured with a meaningful kind
- **THEN** the result carries no classification suggestion

#### Scenario: Detection failure does not fail the capture

- **WHEN** classification detection raises during an otherwise successful capture
- **THEN** the capture is still committed at its projected location
- **AND** the result reports its normal success outcome with no suggestion key

#### Scenario: Internal fallback markers are never analysed

- **WHEN** material that uses the fallback kind as an internal marker rather than a user classification is written
- **THEN** no classification suggestion is produced for it

### Requirement: Legacy source clients and already-captured sources remain valid

Every source kind the closed vocabulary previously accepted SHALL remain valid and SHALL resolve to the same location it resolved to before this change, so no existing client requires modification and no capture behaviour silently moves.

Callers SHALL be able to supply the source kind under either the existing parameter name or a preferred equivalent name. When both are supplied with different values the system SHALL refuse rather than silently prefer one. When neither is supplied the fallback kind SHALL apply.

Already-captured sources SHALL remain valid without modification, whether or not they carry the newer metadata axes. No migration SHALL be required to adopt this change.

#### Scenario: Every legacy kind routes exactly as before

- **WHEN** a source is captured with each kind the previous closed vocabulary accepted
- **THEN** every capture succeeds
- **AND** each is stored at the location that kind resolved to before this change

#### Scenario: Either parameter name is accepted

- **WHEN** a source is captured supplying the kind under the existing parameter name, and again under the preferred equivalent name
- **THEN** both captures succeed identically

#### Scenario: A conflicting pair of names is refused

- **WHEN** a source is captured supplying both parameter names with different values
- **THEN** the capture is refused naming the conflict

#### Scenario: Existing sources need no migration

- **WHEN** a vault containing sources captured under the previous vocabulary is read, indexed, and searched after this change
- **THEN** every existing source remains valid and retrievable at its original location
- **AND** no migration step was required

### Requirement: Human browsing renders new categories without per-kind code

The generated source index SHALL present every populated category, including one produced by a kind the product does not ship, without requiring a code change for that kind. A category with no registered description SHALL receive a generic one rather than being omitted or breaking the index.

Counts SHALL include sources stored beneath a domain level.

#### Scenario: An unshipped kind appears in the index

- **WHEN** a source is captured with a kind the product does not ship as a built-in
- **THEN** the generated source index lists that category
- **AND** its description is either the registered one or a generic fallback

#### Scenario: Nested sources are counted under their kind

- **WHEN** sources are captured for the same kind with and without a domain
- **THEN** the index count for that kind includes both

### Requirement: Retrieval filters kind, domain, and project independently

Retrieval SHALL support filtering by source kind, by subject domain, and by project key independently of one another, and by any combination of them. These SHALL be addressable as first-class filter fields, and SHALL NOT require the caller to encode classification as tags.

#### Scenario: Each axis filters on its own

- **WHEN** a corpus contains sources spanning several kinds, domains, and projects, and a filter names exactly one axis
- **THEN** the results are exactly the sources matching that axis value
- **AND** no other axis restricts the result

#### Scenario: Axes combine

- **WHEN** a filter names a kind and a domain, or a kind and a project, or all three
- **THEN** the results are exactly the sources matching every named axis value

#### Scenario: Classification is filterable without tags

- **WHEN** sources are captured with kind, domain, and projects but no tags
- **THEN** each axis remains filterable

### Requirement: A captured source's classification is correctable

The system SHALL provide one operation that changes a captured source's source kind, its subject domain, or both, resolved through the same open-vocabulary rules that govern capture.

The operation SHALL require a stated reason for the correction, following the existing precedent that a reclassifying move names why the judgement changed.

Supplying neither axis SHALL be refused rather than treated as a no-op relocation, so the operation cannot be used as an unmotivated file move.

#### Scenario: A fallback capture is corrected to a real kind

- **WHEN** a source stored under the fallback kind is reclassified to a meaningful kind with a stated reason
- **THEN** the operation succeeds
- **AND** the source's recorded kind is the canonical form of the supplied value
- **AND** the source is no longer located under the fallback

#### Scenario: A domain is corrected without touching the kind

- **WHEN** only a domain is supplied for a source that already carries a meaningful kind
- **THEN** the recorded kind is unchanged
- **AND** the recorded domain is the canonical form of the supplied value

#### Scenario: A correction with no change is refused

- **WHEN** a reclassification supplies neither a kind nor a domain
- **THEN** the operation is refused
- **AND** the source is not moved

#### Scenario: A correction without a reason is refused

- **WHEN** a reclassification supplies a new classification but no reason
- **THEN** the operation is refused naming the missing reason
- **AND** the source is unchanged

### Requirement: Reclassification changes classification metadata and nothing else

The operation SHALL leave the source's body byte-identical. It SHALL change only the classification fields and the fields recording the correction itself.

It SHALL NOT alter the source's stable identity, its capture timestamp, its recorded origin, its tags, or the list of compiled notes that have ingested it.

Frontmatter mutation on an append-only source is already established: citing a source from a compiled note appends to that source's ingested-into list. Body immutability is the property being protected, and it SHALL remain protected here.

#### Scenario: The body survives a correction unchanged

- **WHEN** a source with body content is reclassified
- **THEN** the body after the correction is byte-identical to the body before it

#### Scenario: Identity and provenance fields survive a correction

- **WHEN** a source carrying a stable identity, a capture timestamp, an origin, tags, and ingested-into entries is reclassified
- **THEN** every one of those values is unchanged

### Requirement: Reclassification relocates the source to the projection its new classification implies

The operation SHALL move the source to the location the deterministic projection derives from its corrected kind and domain, and SHALL apply the same path-safety guarantees capture applies.

When the corrected classification projects to the location the source already occupies, the operation SHALL update the metadata and report that no relocation was required.

#### Scenario: A corrected source lands at its new projected location

- **WHEN** a source is reclassified to a kind and domain that project elsewhere
- **THEN** the source file exists at the projected location
- **AND** no file remains at the previous location

#### Scenario: A correction that does not change the projection moves nothing

- **WHEN** a source is reclassified such that the projection resolves to its current location
- **THEN** the metadata is corrected
- **AND** the operation reports that no relocation occurred

#### Scenario: An unsafe corrected value never reaches a path

- **WHEN** a reclassification supplies a value that cannot normalize into a safe canonical key
- **THEN** the operation is refused
- **AND** the source is neither moved nor modified

### Requirement: Reclassification preserves every reference to the source

The operation SHALL rewrite every inbound reference to the source's previous location so that no reference dangles, including references held in other pages' frontmatter provenance lists.

The operation SHALL record the previous path on the source so a caller holding the old location can still discover where the material went.

A reclassification SHALL be atomic: either the relocation, the metadata correction, and every reference rewrite all apply, or none of them do.

#### Scenario: Inbound references follow the source

- **WHEN** a source cited by a compiled note's provenance list and linked from another page is reclassified to a new location
- **THEN** the citing note's provenance entry names the new location
- **AND** the linking page's reference names the new location
- **AND** no reference to the previous location remains

#### Scenario: The previous path stays discoverable

- **WHEN** a source is reclassified to a new location
- **THEN** the source records the location it previously occupied

#### Scenario: A failure part-way leaves nothing half-applied

- **WHEN** a reclassification fails while applying its changes
- **THEN** the source remains at its original location with its original classification
- **AND** no inbound reference has been rewritten

### Requirement: Reclassification reports what it would do before doing it

The operation SHALL offer a read-only mode that reports the corrected classification, the location the source would move to, the number of references that would be rewritten, and the evidence supporting each proposed value, without writing anything.

The read-only mode SHALL accept a caller-supplied kind and domain and preview that correction, so a caller that has read the source and decided can show the destination and affected-reference count before anything is written. Supplied values SHALL be resolved through the same rules the correction applies, so a value the correction would refuse is refused during the preview rather than after approval.

When no values are supplied, evidence SHALL be limited to what is deterministically observable about the source: its current location, its recorded origin, its title, and its existing metadata. The operation SHALL NOT infer a classification through a model call, and SHALL report that it has no proposal rather than guessing when the observable evidence supports none.

#### Scenario: A preview writes nothing

- **WHEN** a reclassification is requested in read-only mode
- **THEN** the report names the destination and the number of affected references
- **AND** the source is unchanged at its original location
- **AND** no inbound reference has been rewritten

#### Scenario: Evidence accompanies a proposed value

- **WHEN** a proposal is requested for a source whose current location already carries a domain segment
- **THEN** the proposed domain is reported together with the observation that supports it

#### Scenario: A caller previews the correction it has decided on

- **WHEN** a preview is requested with a kind the caller has judged from reading the source
- **THEN** the report names the destination that kind projects to
- **AND** the report states that the value came from the caller rather than from observed evidence
- **AND** the source is unchanged at its original location

#### Scenario: A previewed value is canonicalized, not echoed

- **WHEN** a preview is requested with a kind or domain in non-canonical form
- **THEN** the reported value is its canonical form
- **AND** the reported destination is the one that canonical value projects to

#### Scenario: An undecidable source is reported, not guessed

- **WHEN** a proposal is requested for a source whose observable evidence supports no particular kind
- **THEN** the report states that no kind is proposed
- **AND** no fallback value is presented as a proposal

### Requirement: Reclassification is explicit and never automatic

The system SHALL NOT reclassify or relocate any source as a side effect of another operation, including capture, compilation, indexing, maintenance, registry edits, and advisory detection.

The classification advisory SHALL remain advisory: it reports that debt exists and SHALL NOT act on it.

#### Scenario: Ordinary operations move nothing

- **WHEN** captures, compilations, index updates, and maintenance run over a vault containing fallback-classified sources
- **THEN** no source is relocated or reclassified

#### Scenario: A registry edit does not migrate existing material

- **WHEN** a registry entry's path segment is changed after sources were filed under the previous segment
- **THEN** those sources stay where they are
- **AND** the operation reports no automatic migration

### Requirement: A structural suggestion resolves when the corpus gives its material a home

When the material a structural suggestion describes already has eligible compiled destinations declaring its vocabulary, the system SHALL NOT emit the suggestion, and it SHALL reach that outcome from corpus state alone.

The system SHALL NOT record, read, or rely on any acceptance, dismissal, snooze, cooldown, or per-page suggestion history to reach this outcome. Removing the destinations SHALL restore the suggestion, because nothing about the earlier emission was retained.

A destination SHALL contribute only when it is a compiled page eligible for the caller's own recall, is not the page just written, and its declared identity covers at least two of the cluster's recurring terms. Resolution SHALL be evaluated by removing the covered terms and re-applying the existing mass requirement, so a partially routed cluster still produces a suggestion for the part that has no home.

Resolution SHALL be expressed only as the absence of a suggestion. It SHALL NOT add a key, a reason code, a destination name, a path, a count of destinations, or any other fact about a page other than the one written.

#### Scenario: A suggestion acted on by creating destinations stops firing

- **WHEN** a compiled page carries a recurring off-scope cluster that would otherwise produce a suggestion
- **AND** the vault contains eligible compiled destinations whose declared identities together cover that cluster's recurring terms
- **AND** a further compiled write to the original page commits
- **THEN** the response contains no `structure_suggestion` key
- **AND** the original page's own durable units are unchanged, having been neither removed nor rewritten

#### Scenario: Resolution survives no dismissal record and reverses with the destinations

- **WHEN** the destinations that resolved a cluster are removed from the corpus
- **AND** a further compiled write to the original page commits
- **THEN** the suggestion is emitted again with its existing shape
- **AND** no stored acceptance or dismissal was consulted to reach either outcome

#### Scenario: An incidental single-term match cannot silence a cluster

- **WHEN** the only pages declaring any of a cluster's terms each cover exactly one of them
- **THEN** no destination contributes
- **AND** the suggestion is emitted unchanged

#### Scenario: A partially routed cluster still reports the unrouted remainder

- **WHEN** eligible destinations cover only part of a cluster's recurring terms
- **AND** the remaining terms still satisfy the existing mass requirement
- **THEN** a suggestion is emitted

### Requirement: Structural resolution reads only corpus state the write already holds and fails open

The system SHALL evaluate resolution using the corpus context the mutation already built. It SHALL NOT perform a corpus walk, index read, database query, embedding, or model call for this purpose, and it SHALL NOT build a second corpus context.

The destination set SHALL be drawn from the pages eligible for the caller's own recall, so a page the caller is not entitled to see SHALL NOT affect the caller's suggestion.

When no corpus context is available, or resolution cannot be evaluated for any reason, the system SHALL emit the suggestion exactly as it does without resolution. Suppression SHALL NEVER be the fallback behaviour, and a failure in this analysis SHALL NEVER affect the committed write, its terminal, its status, or its replay behaviour.

#### Scenario: Every compiled writer evaluates resolution

- **WHEN** a page whose cluster is fully routed is mutated through `remember`, `edit_memory`, `observe_memory`, or `replace_memory`
- **THEN** none of the four responses carries a `structure_suggestion`

#### Scenario: An unavailable corpus emits rather than suppresses

- **WHEN** resolution cannot be evaluated because no corpus context is available
- **THEN** the suggestion is emitted with its existing shape
- **AND** the committed write, its terminal, and its status are unchanged

#### Scenario: An ineligible destination does not resolve a cluster

- **WHEN** the only page declaring a cluster's vocabulary is not eligible for the caller's own recall
- **THEN** that page does not contribute
- **AND** the suggestion is emitted unchanged

### Requirement: Simple Front-Door Actions Route To Typed Operations

The command surface SHALL provide a simple front-door vocabulary for agents.
The first implementation MAY expose these as registry aliases, metadata, or
thin orchestration leaves, but the behavior SHALL be backed by the existing
typed operations rather than duplicating write logic. The front-door vocabulary
SHALL include save, adopt/import, ask, prove, review, update, and connect.

#### Scenario: Save routes without duplicate write logic
- **WHEN** the simple save action creates raw input, compiled knowledge, an
  entity, or proof
- **THEN** it delegates to the existing typed leaf (`add`, `note`, `link`, or
  `preserve`/evidence workflow) and preserves the same validation, logging,
  frontmatter, and write-scope rules

#### Scenario: Review fronts existing queues
- **WHEN** the simple review action is invoked
- **THEN** it can surface attention/audit/unprocessed-source findings through a
  product-level response
- **AND** the underlying audit/attention operations remain independently
  callable

### Requirement: Tool Descriptions Teach Intent

Generated MCP tool descriptions SHALL state when an agent should use the tool,
what kind of durable memory it creates or retrieves, and how it preserves
sources/provenance. Primary tool descriptions SHALL use simple product language.
Advanced tool descriptions MAY include internal page-type details.

#### Scenario: Agent sees proof intent
- **WHEN** the MCP schema/tool-description snapshot is inspected
- **THEN** the proof/evidence path tells the agent to use it for cases, claims,
  disputes, warranties, records, or other proof-bearing contexts
- **AND** it does not describe Evidence as the default destination for all raw
  input

### Requirement: Resource Mode CLI Is Scriptable

The system SHALL expose resource mode controls through the CLI without requiring
code edits. The CLI SHALL support showing the current mode and setting the mode
to `quiet`, `normal`, or `performance`. Low-resource aliases such as
`resource-saver` or `low-resource`, if accepted, MUST normalize to `quiet`.
Machine-readable output SHALL include the effective mode, source, config path,
and resolved resource policy fields.

#### Scenario: Show resource mode as JSON

- **WHEN** the user runs the mode command with JSON output enabled
- **THEN** the command emits stable JSON containing the effective mode, mode
  source, config path, and resource policy fields
- **AND** the command exits with status 0

#### Scenario: Low-resource alias maps to quiet

- **WHEN** the user sets the mode through an accepted low-resource alias
- **THEN** the persisted canonical mode is `quiet`
- **AND** subsequent mode status reports `quiet`

#### Scenario: Running server applies CLI mode change

- **WHEN** the CLI writes a new config-file mode
- **THEN** a running server observes the change through the existing mode-watch
  mechanism and applies the corresponding resource policy without a restart

### Requirement: Resource Status CLI Is No-Allocation

The system SHALL expose a scriptable resource status command or mode-status flag
that reports residency and deferred-work diagnostics without allocating heavy
resources. It MUST NOT load models, create sidecars, read vector matrices, or
initialize CUDA solely to answer status.

#### Scenario: Status is safe before gaming

- **WHEN** the user runs the resource status command before starting a foreground
  workload
- **THEN** the command reports mode policy, loaded models, large-cache residency,
  deferred work, and CUDA accounting when already initialized
- **AND** the command does not initialize CUDA or load any absent model/cache

#### Scenario: Unknown probes are represented explicitly

- **WHEN** a platform-specific resource metric cannot be read without allocation
  or without an unavailable dependency
- **THEN** the JSON status reports that metric as unknown or unavailable
- **AND** the command still exits successfully if the rest of status collection
  succeeds

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

### Requirement: Append-Only Tree Relocation

A move that stays within one append-only tree SHALL be permitted, carrying bytes verbatim. A move from `Sources/` into `Evidence/` SHALL be permitted as a promotion when the caller supplies a promotion reason, and the reason MUST be recorded in the activity log. Every other boundary crossing MUST be refused, including any move out of `Evidence/` and any move into an append-only tree from a non-append-only location. Promotion MUST NOT rewrite file content.

#### Scenario: Source becomes case-relevant

- **WHEN** a caller moves a page from `Sources/` to `Evidence/<scope>/` with a promotion reason
- **THEN** the file is relocated with its bytes unchanged
- **AND** the activity log records the promotion together with the supplied reason

#### Scenario: Promotion without a stated reason

- **WHEN** a caller moves a page from `Sources/` to `Evidence/<scope>/` without a promotion reason
- **THEN** the move is refused
- **AND** the refusal names the missing reason rather than the append-only rule

#### Scenario: Evidence is never demoted

- **WHEN** a caller moves a page out of `Evidence/` to any destination
- **THEN** the move is refused regardless of any reason supplied
- **AND** the refusal states that a case scope must remain complete

#### Scenario: Outside content still lands through the capture writers

- **WHEN** a caller moves a page from a non-append-only location into `Sources/` or `Evidence/`
- **THEN** the move is refused
- **AND** the refusal directs the caller to `add` or `preserve`

### Requirement: Referents is an additive shared envelope key
Eligible cue queries SHALL expose the same optional `referents` block through the existing find and ask-memory leaf on MCP, CLI, and REST without adding an MCP, CLI, or REST parameter.

#### Scenario: Compact and full detail
- **WHEN** the same cue query is requested with compact and full hit detail
- **THEN** the envelope-level referents block is identical

### Requirement: Family dispositions are set and read on the existing review surfaces

The triage command SHALL accept a family review reference of the form `exomem://review/family/<family>` with the actions `quiet`, `off`, and `normal`, recording or clearing the family's disposition with the reason token and why, and SHALL refuse those actions on any non-family reference and the item actions on a family reference. The review command SHALL offer a dispositions view listing every family with a non-default disposition, its reason, why, timestamp, and origin, together with per-family counts of manual dismissals, so the state is inspectable on every client. No tool input parameter SHALL be added or removed; the tool descriptions SHALL describe the family actions and the view, and the packaged tool-surface digest SHALL be regenerated and recorded as pending through the documented two-phase rollout.

#### Scenario: Quieting a family through triage

- **WHEN** `triage_memory` is called with a family reference, action `quiet`, and why `too_frequent: not useful in this vault`
- **THEN** the response reports the family, disposition `quiet`, reason `too_frequent`, and origin `manual`
- **AND** the dispositions view lists the family with the same values

#### Scenario: Item actions on a family reference are refused

- **WHEN** `triage_memory` is called with a family reference and action `dismiss`
- **THEN** the call is refused with an action-specific error and no disposition changes

#### Scenario: The regenerated surface is recorded as pending

- **WHEN** the packaged tool schemas are regenerated after this change
- **THEN** the packaged contract digest matches the live discovery surface
- **AND** the connector plugin contract records that digest as pending with a refresh required

### Requirement: Due-state emission is captured and batched

The due-state projection SHALL persist an emission ledger holding the number of governed writes applied to the projection and the number of due-state blocks emitted, readable from the projection file. A product command that commits more than one governed write in one invocation SHALL emit its due-state block at most once, at the end of the invocation, under the unchanged change-only rule; the per-write projection deltas SHALL still apply inside the batch. Separate invocations SHALL remain separate batches.

#### Scenario: A bulk command emits once

- **WHEN** one command commits twelve governed writes that each change the due-state counts
- **THEN** the command's response carries at most one due-state block
- **AND** the emission ledger's write count rose by twelve and its emission count by at most one

#### Scenario: The ledger is readable by a projector

- **WHEN** the projection file is read after a batch
- **THEN** it carries an emission section with the write count and the emission count

### Requirement: The f23 family runs against the real runtime

A journey driver SHALL execute the f23 scenario's operations against an installed envelope — seed, maintenance passes, a triage dismissal, an engine restart, prominence reconfiguration across the full level range, and one bulk ingest — and SHALL project the resulting review state and emission ledger into the snapshot pair the family's assertions evaluate. The vault projector SHALL declare `due_state_counters` available through the projection file. The driver SHALL refuse to run rather than fall back when no envelope is installed.

#### Scenario: f23 reports what this runtime can decide, and no more

- **WHEN** the f23 journey runs against the current runtime
- **THEN** `dismissal_respected_across_passes` passes for the dismissed subject
- **AND** `counter_emission_not_repeated_per_write` is evaluated on the emission delta between the two snapshots, so it is decided only for a batch that delivered at least one block, and otherwise reports `unsupported` rather than passing vacuously or inheriting an earlier batch's delivery
- **AND** on this runtime it reports `unsupported`, because no product leaf reaches the write carrier, so the bulk batch delivers no block and its emission delta is zero
- **AND** the batch-once requirement is proven where it is decidable: twelve write carriers inside one batch scope emit at most one block, plus the measured zero carrier trips at every product leaf

#### Scenario: Removing the batch scope turns the counter assertion red

- **WHEN** the batch scope is disabled and twelve write carriers run over one vault
- **THEN** `counter_emission_not_repeated_per_write` fails with twelve emissions for twelve writes

### Requirement: Every compiled writer shares one source-closure leaf

`remember_memory`, `replace_memory`, `edit_memory`, and governed Tier-2 compiled-note creation SHALL call one shared source-closure validator at the semantic precommit boundary. MCP, REST, CLI, OpenAPI, bootstrap guidance, and schema-fidelity fixtures SHALL derive the same behaviour and remediation from the canonical command registry; no facade SHALL implement its own resolver or warning-only exception.

#### Scenario: Equivalent unresolved write has surface parity

- **WHEN** the same compiled-note mutation with an unresolved explicit source is invoked through MCP, REST, CLI JSON, and the governed Tier-2 route
- **THEN** every surface refuses with the same stable application data and no surface commits the note

#### Scenario: Future writer cannot omit closure classification

- **WHEN** a new registry command is classified as a compiled semantic writer
- **THEN** registry or contract validation fails closed unless it enters the shared source-closure precommit path

### Requirement: Source-closure refusal uses one stable application envelope

The public error registry SHALL define `UNRESOLVED_SOURCE_CITATION` with a non-empty bounded message, capture-first remediation, unresolved total, deterministic capped caller-supplied values, and truncation state. MCP SHALL return the deliberate refusal as normal tool content; REST and CLI JSON SHALL return the identical shared envelope; human CLI SHALL render the same code, message, and remediation with the canonical operation-error exit status.

#### Scenario: Deliberate source refusal is not an internal error

- **WHEN** source closure rejects a non-empty unresolved citation
- **THEN** each generated facade presents the stable application refusal and does not relabel it as an unexpected execution failure

#### Scenario: Refusal guidance teaches capture then retry

- **WHEN** capability or bootstrap guidance describes a writer that accepts `sources`
- **THEN** it states that external material must first be captured and the derived write retried with the governed source reference

### Requirement: Capture remains independent from derived compilation

Source and Evidence capture commands SHALL remain valid without a pending derived note and SHALL preserve existing raw-material and provenance contracts. A compiled writer SHALL NOT call a connector, fetch a remote locator, or silently invoke capture while validating source closure.

#### Scenario: Compiled write does not fetch an external ID

- **WHEN** a source entry resembles a connector URL or object identifier
- **THEN** the writer refuses locally without network access or a hidden capture side effect

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

### Requirement: Generated Surfaces Inject Trusted Authorization Context

The single command registry SHALL declare which operation variants require a principal,
authorization session, and authorization-session lifecycle action. Generated MCP, REST,
Hosted, CLI, and OpenAPI surfaces SHALL expose the same public session capability
credential and lifecycle semantics while resolving canonical principal and trusted
issuer/surface family inside their adapters. The dispatcher SHALL inject one immutable
verified request context; governance leaves SHALL NOT accept a public `principal`,
`principal_scope`, issuer, or internal session-id parameter.

The registry SHALL classify every generated command, legacy leaf, finite selector
variant, retrieve/inject hook, and content-bearing writer result into the closed
credential matrix in `authorization-session-binding`: session open forbids a credential;
status/rotate/close plus session grant/revoke/declare require one; self-inspection and all
content/resolution routes accept it optionally with absent meaning standing-only and
present-invalid rejecting; owner-only/standing authoring does not derive authority from
it. No route may infer another behavior, and startup SHALL fail if a route has zero or
multiple classifications.

MCP SHALL expose one optional placeholder named exactly
`authorization_session_credential`. Bounded raw JSON-RPC/ASGI middleware SHALL extract
`params.arguments.authorization_session_credential`, remove/redact it from the envelope
and every logging/error copy, resolve trusted transport authentication, verify it, and
install immutable context before FastMCP request logging, `FunctionTool`, or Pydantic
validation. The middleware SHALL return the common credential refusal for a duplicate,
non-string, malformed, or invalid value even when ordinary arguments are malformed. It
SHALL pass a sanitized argument map to FastMCP; generated wrappers/leaves MUST NOT receive
the bearer parameter. FastMCP transport/session/request identity SHALL remain
non-authoritative.

REST and Hosted SHALL accept the credential only through the sensitive
`X-Exomem-Authorization-Session` header, separate from service/gateway `Authorization`;
body/query carriers SHALL be forbidden. Raw ASGI middleware SHALL remove/redact it before
access logging, exception copies, validation, and dispatch, then verify only after the
trusted access/gateway principal resolves. Hosted SHALL reject conflicting caller
principal headers. CLI SHALL expose only `--authorization-session-fd <fd|->`, read one
bounded bearer from a protected already-open descriptor or stdin, and clear it after
verification; literal argv and environment bearer carriers SHALL be forbidden. Generated
MCP schema, REST/Hosted OpenAPI, and CLI help SHALL advertise only their appropriate
placeholder/header/descriptor carrier.

Across all surfaces, raw extraction/redaction SHALL precede framework logging and
validation. Trusted principal resolution and capability verification SHALL then precede
ordinary coercion/validation, cache lookup/key creation, idempotency lookup, release
decision, receipt allocation, or leaf dispatch. Only an exact successful session
open/rotate response may carry the typed `issued_credential` through the non-disableable
terminal scrubber after response-schema and just-minted-value validation.

#### Scenario: Registry parity includes session lifecycle

- **WHEN** the registry and generated artifacts are inspected
- **THEN** authorization-session open, status, rotate, close, and protected resume
  semantics match across MCP, REST, Hosted, CLI, and OpenAPI

#### Scenario: Leaf cannot accept caller identity

- **WHEN** the live MCP schema, REST/OpenAPI schema, Hosted admission schema, and CLI help
  are generated
- **THEN** none exposes principal, principal scope, issuer, or internal session id as a
  caller-authoritative governance parameter

#### Scenario: Stateless MCP request id is not a session

- **WHEN** stateless MCP HTTP reconnects with a repeated or changed framework session or
  request id
- **THEN** session authority changes only when a valid server-issued capability is
  verified

#### Scenario: Unbound adapter fails closed

- **WHEN** a generated or in-process route reaches the dispatcher without the trusted
  principal/session context its registry variant requires
- **THEN** the invocation refuses before governance state or content is read and does not
  default to owner

#### Scenario: Bearer is absent from observability

- **WHEN** a request carrying a valid or invalid authorization capability is logged,
  traced, retried, rejected, or included in an idempotency calculation
- **THEN** the raw bearer is absent from all observability and persisted replay material

#### Scenario: Invalid credential wins before validation and cache

- **WHEN** a generated route receives both an invalid session credential and malformed or
  cacheable content arguments
- **THEN** it returns the common credential refusal before validation detail, cache or
  idempotency access, governance decision, receipt, or leaf effect

#### Scenario: MCP raw middleware precedes FastMCP validation

- **WHEN** an installed FastMCP stateless-HTTP request carries an invalid
  `params.arguments.authorization_session_credential` and a separately malformed tool
  argument
- **THEN** raw middleware scrubs the bearer, returns the common credential refusal, and
  FastMCP logging, `FunctionTool`, Pydantic validation, wrapper, and leaf receive no raw
  bearer or validation copy

#### Scenario: Surface carriers are distinct and protected

- **WHEN** clients inspect generated MCP, REST/Hosted, and CLI contracts
- **THEN** MCP exposes only the optional consumed placeholder, REST/Hosted expose only
  `X-Exomem-Authorization-Session` separate from `Authorization`, and CLI exposes only
  `--authorization-session-fd`; body/query/env/literal-argv alternatives refuse

#### Scenario: Actual-wire failures leave no observability copy

- **WHEN** valid, invalid, duplicate, non-string, or malformed bearers traverse installed
  MCP/FastMCP, REST, Hosted, and CLI adapters and trigger access logs, validation errors,
  exceptions, retries, traces, and debug serialization
- **THEN** an exact scan finds no bearer outside protected input/typed issuance and every
  wrapper/leaf invocation sees only trusted internal context

#### Scenario: Issuance is the only terminal scrubber exception

- **WHEN** a bearer-shaped value appears on any route or field other than the exact typed
  `issued_credential.bearer` of successful session open/rotate
- **THEN** the terminal scrubber removes it and malformed issuance refuses rather than
  weakening global bearer redaction

### Requirement: Governed Find Continuations Are Surface-Equivalent

The generated `ask_memory` and legacy `find` signatures SHALL expose the same optional
bounded string `continuation` across MCP, REST, Hosted, CLI, OpenAPI, and in-process
dispatch. The field SHALL be absent by default and SHALL NOT change never-governed
response bytes. A supplied continuation on a route without an active governed projected
runtime, or any malformed/unknown/expired/cross-binding continuation, SHALL return the
same `INVALID_CONTINUATION` application refusal. No adapter may decode the token into
caller-selectable offset, principal, session, purpose, policy, catalog, or runtime
authority.

Governed projected success SHALL use the existing envelope and MAY include exactly one
`continuation` string when another authorized page exists. Exhaustion SHALL omit the
field. The generated schemas SHALL declare that optional field without changing
ungoverned default payloads. MCP, REST, Hosted, CLI, and in-process calls with the same
trusted principal and request SHALL return the same page and continuation bytes, modulo
the already registered outer transport framing exclusions.

#### Scenario: Cross-surface continuation parity

- **WHEN** the same trusted principal requests consecutive governed pages through MCP,
  REST, Hosted, CLI, and in-process dispatch
- **THEN** each surface accepts the same bounded continuation contract and returns the
  same canonical page membership, order, exhaustion, and continuation bytes

#### Scenario: Caller cannot choose a page offset

- **WHEN** a caller edits, fabricates, replays after expiry, or moves a continuation
  across vault/principal/session/purpose/request bindings
- **THEN** the dispatcher returns `INVALID_CONTINUATION` and no decoded token field or
  registry detail reaches validation, logs, errors, or the leaf

### Requirement: Reserved Path Classification Is Registry-Total

The command registry SHALL identify every path/ref-bearing argument for every operation
and finite-selector variant, including whether it is a source, destination,
metadata-derived recovery destination, recursive root, dataset, media artifact, frame,
transfer target, or alias. Startup coverage SHALL fail when any registered public route
or selector can reach a path without a classification. The shared dispatcher SHALL apply
the canonical reserved administration-path classifier before existence checks, parsing,
counting, mutation planning, lease acquisition that exposes target state, or leaf
dispatch.

This is a boundary on Exomem commands and cooperating Exomem subsystems: untrusted
principals reach vault state only through that boundary. Direct filesystem or block-device
access as the OS vault owner is owner-equivalent and outside zero-effect and
universal-detection claims; it may disclose, corrupt, move, or delete state. The boundary
SHALL fail closed when drift is observable against retained logical, catalogue, registry,
or filesystem-identity anchors, but SHALL NOT claim to detect or reverse an unobservable
out-of-band owner action.

Classification SHALL cover one closed versioned internal-state registry. Its initial set
SHALL include `_Governance/**`, `_Consolidation/**`, the exact root-level
`Knowledge Base/` names `.governance.sqlite`, `.embeddings.sqlite`, `.clip.sqlite`,
`.lexical.sqlite`, `.graph.sqlite`, `.claims.sqlite`, `.references.sqlite`,
`.refs.sqlite`, `.freshness.sqlite`, `.deferred-index.sqlite`,
`.deferred_index.sqlite`, `.media-jobs.sqlite`, `.media_jobs.sqlite`,
`.idempotency.sqlite`, `.idempotency.json`, `.idempotency.jsonl`,
`.media-jobs.json`, `.deferred-index.json`, `.voice_profiles.json`,
`.media-worker.lock`, `.graph-sync.json`, `.graph-sync-floor.json`,
`.graph-commit-receipts/**`, and `.review-state.json`; current review-state temps matching
exactly `..review-state.json.[a-z0-9_]{8}.tmp`; lexical rebuild state matching exactly
`.lexical.sqlite.rebuild-[0-9a-f]{32}.tmp` plus that temp database's `-wal`, `-shm`, and
`-journal` siblings; lexical quarantine members matching exactly
`.lexical.sqlite.quarantine-[0-9a-f]{32}`,
`.lexical.sqlite-wal.quarantine-[0-9a-f]{32}`, and
`.lexical.sqlite-shm.quarantine-[0-9a-f]{32}`; plus
`Knowledge Base/.authorization-projections/**`. Every ordinary SQLite
entry SHALL include its exact `-wal`, `-shm`, and `-journal` family and the graph entry
its bounded registered rebuild-temp family. This closed set covers governance/session
authority, journals, raw lexical/vector/CLIP/reference/graph state, immutable projected
indexes, and active catalog descriptors. A new internal store/index/temp/lock cannot run
until it is registered and registry-total tests pass.
For both `/**` descriptors the root directory itself and every descendant SHALL be
reserved, including unknown/future child names; recognizing only today's receipt format
is insufficient for the ordinary-operation boundary.

The initial descriptor set SHALL be generated/audited against every current private-
state owning module and path factory, not copied from the hosted-portability list. The
audit SHALL enumerate primary files, directories, transactional siblings, owner-created
temps/quarantines/receipts, and runtime physical identities for governance, lexical,
vector, CLIP, graph/handoff, refs/claims, review, deferred/media/idempotency, voice, and
projection owners. Every owner-produced form SHALL map to exactly one descriptor; an
unmapped or multiply mapped private path SHALL fail startup and registry tests before
the owner may create/open it. Portability/export rules SHALL consume this security
registry or be checked against it, never define a smaller authority boundary.

Logical names are reserved before they exist. At protected acquisition, a stable
pre-existing symlink, reparse point, hard link, or physical alias to a reserved family
member SHALL be refused. Each owning subsystem SHALL retain and publish stable identities
of open primary/WAL/SHM/journal/temp/index files under the leaf coordination primitive.
SQLite primary/WAL/SHM identities SHALL be published before cooperative coordination is
released, not before filesystem reachability. Secure resolution SHALL compare retained
identities where `realpath` alone cannot expose an alias and SHALL reject multiply linked
or ambiguous internal-state files. It SHALL check both ends of move/copy/replace, trash
source and every possible restore destination, and every child of a recursive operation.
An observable anchor discrepancy or non-canonical resolution SHALL fail closed. A private
owning-subsystem authority MAY pass the dispatcher check; no serialized argument,
generic alias, surface, owner/L6 decision, non-Markdown classification, or Tier-2 flag
may do so.

Logical classification SHALL route/refuse early but SHALL NOT authorize a later pathname
reopen. Every generic filesystem leaf SHALL execute through a descriptor-bound,
handle-relative reserved-path transaction: open the vault root and parents without
following links, hold them through the leaf operation, classify stable volume/device and
file identities, and use relative create/read/write/rename/link/unlink primitives. POSIX
SHALL use `openat2` beneath/no-symlink/no-magic-link constraints or an equivalent
iterative dirfd/`openat`/`O_NOFOLLOW` implementation. Windows SHALL use `NtCreateFile`
with `RootDirectory` and a relative name plus `NtSetInformationFile` rename/disposition
semantics, reparse-aware handles, and final volume/file identity; the route SHALL remain
disabled unless a runtime actual-filesystem capability probe proves those exact relative
handle operations, no-follow/reparse behaviour, and final identity checks. Failure or
absence of that probe SHALL disable the route and return the registered refusal without a
fallback. A windows-latest required CI gate SHALL exercise NTFS junction, reparse,
hard-link, 8.3, rename/disposition, and fallback-disable fixtures, and SHALL be wired
into combined release verification. Same-device rename, trash, and recovery SHALL hold
both parent handles and run under cooperative coordination; cross-device move, trash,
and recovery SHALL refuse. A copy SHALL read the held source and publish atomically only
at the held destination; it SHALL NOT claim source-and-destination atomicity. Recursive
and multi-entry power-loss handling SHALL use a saga and recovery, not cross-file or
recursive atomicity. Parent swaps, rename/link races, hard links, reparse points, and
bind aliases SHALL be checked at the kernel read/mutation operation against retained
anchors, not by check-then-`realpath`. A platform without equivalent primitives SHALL
disable the affected generic route.

Enumeration and retrieval routes—including list/walk/browse/search/find/get/fetch,
dataset/Records/media/frame, download/export/transfer, graph/provenance, audit/repair,
trash/recovery, and recursive packaging—SHALL remove registered internal state before
existence, candidate, count, ordering, or manifest computation. Generic mutation routes
SHALL refuse a reserved target before touching that target. Multi-entry mutation and
recovery SHALL use an ordered, descriptor-bound preflight and saga: each entry is
revalidated immediately before its durable effect, each completed entry records durable
receipt/journal state, and later refusal or interruption is handled by recorded recovery,
not by treating all entries as one transaction. A private state file MUST NOT enter the
governance membership evaluator as an ordinary non-Markdown artifact at L6.

#### Scenario: Case Unicode and separator variants remain reserved

- **WHEN** a route spells a reserved component with case variants, NFKC-equivalent
  Unicode, backslashes, mixed separators, or a knowledge-base-prefix variant
- **THEN** every generated surface classifies it as the same reserved root

#### Scenario: Stable aliases and symlinks cannot bypass the root

- **WHEN** protected acquisition finds a canonical ref, managed alias, short-name alias,
  or stable pre-existing symlink resolving into a reserved tree without spelling its name
  in the public input
- **THEN** the dispatcher and secure leaf resolution both refuse or hide the target within
  the command boundary

#### Scenario: Filesystem identity alias cannot bypass the root

- **WHEN** protected acquisition finds a stable pre-existing hard link, bind-style alias,
  or multiply linked file outside the reserved spelling that refers to
  administration-tree state
- **THEN** retained filesystem-identity checks refuse it as reserved or fail closed on an
  observable anchor discrepancy

#### Scenario: Move checks source and destination

- **WHEN** either end of a move, copy, replace, or transfer resolves inside a reserved
  tree
- **THEN** the entire operation refuses before any source or destination mutation

#### Scenario: Recovery checks explicit implicit and recursive destinations

- **WHEN** a recover operation uses a trash path, explicit restore path, original path
  from metadata, alias, or recursive child that resolves to a reserved tree
- **THEN** the ordered preflight refuses before that entry is restored; entries already
  durably completed by the recovery saga are reconciled from their receipt/journal state,
  and recovery remains per-entry rather than transactional across its full set

#### Scenario: Dataset and media selectors are covered

- **WHEN** query/dataset, Records, process/read media, video-frame, upload/download, and
  multiplexed management variants are enumerated
- **THEN** every path/ref selector is registry-classified and none can reach a reserved
  tree through an unclassified branch

#### Scenario: Private activation family is never ordinary L6

- **WHEN** `.governance.sqlite`, any exact WAL/SHM/journal sibling, or a retained/
  published physical alias is targeted through list/walk/search/get/download/dataset/
  export/transfer/recovery at owner/L6
- **THEN** it is structurally absent before membership/projection and no byte, row,
  count, name, hash, timing signal, or existence bit is returned

#### Scenario: Raw and projected index families are equally reserved

- **WHEN** a generic route targets `.embeddings.sqlite`, `.clip.sqlite`, `.lexical.sqlite`,
  `.graph.sqlite`, `.refs.sqlite`, `.authorization-projections/**`, a registered legacy
  spelling, journal sibling, rebuild temp, or retained/published physical alias
- **THEN** reads/enumeration hide it and generic mutation refuses independently of
  whether the file currently exists

#### Scenario: Graph handoff and review-state families are reserved

- **WHEN** a route targets `.graph-sync.json`, `.graph-sync-floor.json`, the
  `.graph-commit-receipts/` root or any descendant, `.review-state.json`, or an exact current
  `..review-state.json.<8-char-token>.tmp`, before or after owner creation
- **THEN** list/search/get/download/export/dataset/recovery treats it as absent and every
  generic create/move/delete/recover refuses without revealing stable existence

#### Scenario: Lexical rebuild and quarantine generations are reserved

- **WHEN** a route targets an exact `.lexical.sqlite.rebuild-<32-lowerhex>.tmp` family or
  the main/WAL/SHM `.quarantine-<same-32-lowerhex>` group during publish/rollback
- **THEN** dispatcher and held-leaf identity checks hide/refuse every member, including a
  pre-create spelling and an alias raced between quarantine and restore

#### Scenario: Every private-state owner is inventoried

- **WHEN** current owner path factories and write/temp/quarantine/receipt paths are
  enumerated independently of hosted portability
- **THEN** each maps to exactly one internal-state descriptor and a missing/duplicate
  mapping fails before startup or owner creation

#### Scenario: WAL creation and checkpoint race stay reserved

- **WHEN** a cooperating internal subsystem creates, checkpoints, renames, or removes a
  WAL/SHM/journal/staged-index file while a generic read, move, link, delete, recovery,
  or export observes the same retained logical or physical identity
- **THEN** shared held-leaf coordination classifies the identity as internal or fails the
  generic operation closed; SQLite identities are published before coordination release

#### Scenario: Internal-state registry is closed

- **WHEN** code introduces an internal database, journal, lock, temp pattern, raw lane,
  projected lane, graph, or catalog file without a descriptor
- **THEN** startup/schema coverage fails before the owning subsystem or generic command
  can open it

#### Scenario: New path-bearing command fails until classified

- **WHEN** a command, action, operation, mode, or alias with a new path/ref field is added
  to the registry
- **THEN** registry/startup coverage fails until its role and reserved-path behavior are
  declared

#### Scenario: Parent swap cannot cross the reserved boundary

- **WHEN** a stable pre-existing or anchor-observable parent swap resolves to a symlink,
  junction, reparse point, or bind mount into a reserved tree before the leaf
- **THEN** the held-handle operation refuses before the protected acquisition or mutation

#### Scenario: Rename and hard-link races cannot bypass identity

- **WHEN** a source/destination rename, hard link, or alias exchange produces an
  observable retained-anchor mismatch after logical classification
- **THEN** the leaf refuses; no portable final-component filesystem guarantee is assumed
  beyond the platform primitives named above

#### Scenario: Unsupported platform primitives fail closed

- **WHEN** a platform cannot provide no-follow handle-relative traversal and mutation
  through the leaf for a possibly reserved target
- **THEN** the generic route returns the content-free reserved-path refusal and MUST NOT
  fall back to check-then-path-use

#### Scenario: Cross-device and multi-entry operations have bounded semantics

- **WHEN** a generic move, trash, or recovery crosses devices, or recursive/multi-entry
  work is interrupted by power loss
- **THEN** the cross-device move/trash/recovery refuses, while copy publishes only its
  destination atomically and recursive/multi-entry work uses ordered preflight, per-entry
  durable effects, and recorded saga recovery rather than claiming all-or-none atomicity

### Requirement: Reserved Path Outcomes Are Surface-Consistent

MCP, REST, Hosted, and CLI SHALL produce the shared content-free outcome for the same
reserved-path request. Ordinary read/enumeration operations SHALL use the same missing
contract as structural absence. Generic mutations SHALL use one stable reserved-path
code and remediation naming only the owning command, without probing or reporting
whether the requested reserved target exists. The caller-supplied spelling MAY be echoed
only where the existing caller-input error contract permits it; no resolved alias,
canonical administration path, child count, or metadata-derived destination may be
returned.

#### Scenario: Read parity hides existence

- **WHEN** the exact same ordinary reserved-path read is issued through MCP, REST,
  Hosted, and CLI, first with the target present and then absent
- **THEN** all surface envelopes match their missing-path contract and reveal no
  existence difference

#### Scenario: Mutation parity names only the owning command

- **WHEN** the exact same generic reserved-path mutation is issued on each surface
- **THEN** each returns the shared stable code/remediation, performs no write, and emits
  no resolved path or tree metadata

