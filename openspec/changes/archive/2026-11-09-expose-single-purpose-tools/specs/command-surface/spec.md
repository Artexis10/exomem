## ADDED Requirements

### Requirement: Product tools separate reads from writes and expose each operation as a typed variant

A converted product object SHALL be exposed on MCP as one read tool and one write tool. Each tool SHALL take a required `operation` object discriminated by `kind`, and each variant SHALL declare exactly the fields its operation accepts and forbid every other field. A read tool SHALL contain only variants that the read-only invocation classifier treats as reads for every accepted input, and SHALL be annotated `readOnlyHint=true`. A write tool SHALL be annotated `readOnlyHint=false`, and its `destructiveHint` SHALL be true when any of its variants can overwrite or remove content. Every selector value of a converted command, paired with its import mode where it has one, SHALL map to exactly one variant of exactly one tool.

#### Scenario: A variant shows only its own fields
- **WHEN** a client inspects the `query` variant of `read_records`
- **THEN** it sees the query fields, with `collection` required
- **AND** it does not see manifest, mutation or history-only fields

#### Scenario: A field from another operation is refused
- **WHEN** a caller sends `change_records` an `append` operation that carries `acknowledged_gap_codes`
- **THEN** validation refuses before any lease or write with the selected kind and the unexpected field

#### Scenario: A read tool cannot write
- **WHEN** any variant of `read_records` or `read_plans` runs with any accepted input
- **THEN** invocation classification treats it as read-only and it does not contact the writer coordinator

#### Scenario: Coverage is complete
- **WHEN** the server starts
- **THEN** every Records and Planning selector value and import mode maps to exactly one variant, every read variant classifies as read-only, and a gap or duplicate fails startup

#### Scenario: A write sent to a read tool is refused
- **WHEN** a caller sends `read_records` an `append` operation, or an `import` operation whose mode is `start`, missing or malformed
- **THEN** the call is refused before any lease with the grouped command's refusal envelope
- **AND** the refusal names `change_records` when the kind belongs to it

### Requirement: Converted tools keep the grouped command as their execution identity

A converted tool SHALL normalize its operation into the grouped command's selector value and arguments before authorization, payload hashing, idempotency, the writer lease, the call ledger, the governance projector and the egress postfilter. Validation, guards, refusals, receipts and response shapes SHALL be those of the grouped command for the same arguments. Any component that routes by tool name SHALL resolve a converted spelling to its command and normalized arguments, and an unresolvable name SHALL fail closed.

#### Scenario: A retry crosses spellings
- **WHEN** a client submits an `append` through `record_memory` and retries the same idempotency key and arguments through `change_records`
- **THEN** the retry returns the committed result and no second write occurs

#### Scenario: Authorization sees the grouped command
- **WHEN** a governed caller invokes `change_plans` with an `add` operation
- **THEN** credential routing, the authorization decision and the disclosure receipt are the same as for `plan_memory` with `action="add"`

#### Scenario: Store-routed collections are unchanged
- **WHEN** `read_records` queries a collection that the collection store serves
- **THEN** the same store routing, result and receipts apply as for `record_memory` with `action="query"`

### Requirement: Grouped names stay callable as hidden compatibility spellings

After a command converts, its grouped tool name SHALL remain callable on MCP with its previous schema, defaults and annotation, and SHALL NOT appear in MCP discovery. Compatibility spellings SHALL have no automatic expiry. Surface, tier and Cloud exclusions SHALL apply to every spelling. On MCP, discovery, bootstrap contracts and routes, the action catalog, the surface descriptor, response payloads that propose a next call, remediation text, hook reminders and the shipped skill SHALL name the converted tools, because a host may let a model call only listed tools. REST and CLI SHALL keep the grouped spelling.

#### Scenario: An existing client keeps working
- **WHEN** a client calls `record_memory` or `plan_memory` with previously valid arguments
- **THEN** the command applies the same validation, guards and result contract as before conversion

#### Scenario: No MCP text names a hidden spelling
- **WHEN** an MCP client receives bootstrap guidance, a `create_note` result that proposes a Records call, or a remediation for a Planning refusal
- **THEN** the proposed call names `change_records`, `read_records`, `read_plans` or `change_plans` with an `operation` object
- **AND** the same guidance on REST and CLI names `record_memory` or `plan_memory`

#### Scenario: Discovery lists one spelling per operation
- **WHEN** a client lists MCP tools
- **THEN** it sees `read_records`, `change_records`, `read_plans` and `change_plans`
- **AND** it does not see `record_memory` or `plan_memory`

### Requirement: Records are exposed as one read tool and one write tool

The product surface SHALL expose Records through `read_records` and `change_records` rather than storage-specific tools. `read_records` SHALL hold `describe`, `inspect`, `validate`, `query`, `history`, and `import` restricted to the `preview` and `status` modes. `change_records` SHALL hold `create`, `append`, `update`, `bulk_upsert`, `discard`, `revise`, `rebaseline`, and `import` restricted to the `start` and `cancel` modes, where continuing an import is `start` with its continuation. Query SHALL cover bounded list, history, render and export-shaped responses through explicit arguments; generic derived-index repair SHALL remain a maintenance operation.

#### Scenario: Agent discovers before creating
- **WHEN** an agent needs to create a first Record collection without prior manifest knowledge
- **THEN** it uses `describe` and `validate` through `read_records` before `create` through `change_records`

#### Scenario: Natural log intent finds the Records tools
- **WHEN** an agent receives “Log this training session”, “Record today’s symptoms”, “Update the car mileage”, or “Show the last three months”
- **THEN** bootstrap routes the reads through `read_records` and the writes through `change_records` rather than advertising storage-specific tools

#### Scenario: Existing manifest uses the Records tools
- **WHEN** an agent needs to validate, revise, or explicitly rebaseline an existing collection manifest
- **THEN** it validates through `read_records` and revises or rebaselines through `change_records` rather than a generic file editor or storage-specific tool

#### Scenario: Storage strategy is not a tool choice
- **WHEN** the selected collection uses a Markdown log, Markdown items, or a dataset
- **THEN** the same tools resolve the manifest and adapter without making the agent select a storage-specific public command

#### Scenario: Dataset mutation refuses through the write tool
- **WHEN** `append` or `update` resolves a dataset-backed collection
- **THEN** the command refuses that action as unsupported in this delivery and does not accept a caller-supplied replacement file

### Requirement: Planning is exposed as one read tool and one write tool

The product surface SHALL expose Planning through `read_plans` and `change_plans` rather than separate capture, horizon, hierarchy, manifest-lifecycle, or storage-specific tools. `read_plans` SHALL hold `inspect`, `validate`, `query` and `history`. `change_plans` SHALL hold `create`, `add`, `update`, `triage`, `revise` and `rebaseline`. `inspect` without a collection SHALL return the Planning inventory; with a collection it SHALL inspect that collection. Query SHALL cover bounded horizon, date, history, hierarchy, render and export-shaped responses through explicit arguments; generic derived-index repair and previewed structured-file migration SHALL remain maintenance operations.

#### Scenario: Natural planning intent finds the Planning tools
- **WHEN** an agent receives “save this feature idea”, “file this bug for later”, “make this a quarterly initiative”, “what matters this week”, “show my multi-year outcomes”, or “revise this Planning collection”
- **THEN** bootstrap routes reads through `read_plans` and writes through `change_plans` instead of advertising a family of narrow tools

#### Scenario: Planning storage is not a tool choice
- **WHEN** an agent captures, queries, validates, or revises Planning intent
- **THEN** the same tools resolve the Planning collection and Markdown-item adapter without asking the user to select an internal storage operation

#### Scenario: Existing manifest uses the Planning tools
- **WHEN** an agent needs to validate, revise, or explicitly rebaseline an existing Planning collection manifest
- **THEN** it validates through `read_plans` and revises or rebaselines through `change_plans` rather than a generic file editor or storage-specific tool

#### Scenario: Inventory before a collection is known
- **WHEN** an agent calls `read_plans` with an `inspect` operation and no collection
- **THEN** the response is the bounded Planning inventory and nothing is created or resolved

#### Scenario: Review does not hide inside query
- **WHEN** a caller queries a plan that carries Records evidence descriptors
- **THEN** `read_plans` returns authored Planning state and descriptors without evaluating planned-versus-recorded progress or silently invoking epistemic review

## MODIFIED Requirements

### Requirement: Records command parity and selector safety

`record_memory` SHALL be generated from the canonical command registry across MCP, REST, and CLI, and `read_records` and `change_records` SHALL be renderings of it on MCP. `describe`, `validate`, `inspect`, `query`, `history`, and `import` in the `preview` and `status` modes SHALL be read-only at invocation classification; `create`, `append`, `update`, `bulk_upsert`, `discard`, `revise`, `rebaseline`, and `import` in the `start` and `cancel` modes SHALL enter writer authority, idempotency, terminal-response, governance-projector, and retry coverage. Unknown or unclassified actions SHALL fail closed at startup or invocation.

#### Scenario: Discovery and validation do not acquire writer authority
- **WHEN** Records runs `describe`, either `validate` form, collection-less or targeted `inspect`, `query`, `history`, or an import preview or status
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Every mutation enters writer authority
- **WHEN** Records runs `create`, `append`, `update`, `bulk_upsert`, `discard`, `revise`, `rebaseline`, or an import start or cancel
- **THEN** it uses the existing same-vault writer lease, idempotency, committed terminal envelope, and content-safe projector

#### Scenario: Mutations enter writer authority
- **WHEN** `record_memory` runs `create`, `append`, or `update`
- **THEN** it uses the existing same-vault writer lease, idempotency, and committed terminal envelope

#### Scenario: Unknown selector cannot bypass coverage
- **WHEN** an unregistered Records action or operation kind reaches the command boundary
- **THEN** it is refused and cannot default to a read or mutation path without projector and receipt coverage

#### Scenario: Mixed command is advertised conservatively
- **WHEN** MCP exposes annotations for the hidden `record_memory` spelling
- **THEN** the command-level annotation remains write-capable even though selector dispatch keeps its read actions lease-free

#### Scenario: Read and write tools are advertised honestly
- **WHEN** MCP exposes annotations for the Records tools
- **THEN** `read_records` is read-only and `change_records` is write-capable and destructive

### Requirement: Planning command parity and selector safety

`plan_memory` SHALL have one Python leaf/signature and SHALL be registered consistently in the repository's canonical command and product metadata registries so MCP, REST, CLI, OpenAPI, capability documentation, and schema-fidelity fixtures are generated from that implementation; `read_plans` and `change_plans` SHALL be renderings of it on MCP. `inspect`, `validate`, `query` and `history` SHALL remain read-only at invocation classification; `create`, `add`, `update`, `triage`, `revise`, and `rebaseline` SHALL enter writer authority, idempotency, terminal-response, governance-projector, and retry coverage. Unknown or unclassified actions SHALL fail closed at startup or invocation.

#### Scenario: Planning reads do not acquire writer authority
- **WHEN** Planning runs `inspect`, either `validate` form, `query` or `history`
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Query does not acquire writer authority
- **WHEN** `plan_memory` runs `query` or `inspect`
- **THEN** invocation classification treats it as read-only and does not contact the writer coordinator

#### Scenario: Planning mutation enters writer authority
- **WHEN** Planning runs `create`, `add`, `update`, `triage`, `revise`, or `rebaseline`
- **THEN** it uses the existing same-vault writer lease, idempotency, committed terminal envelope, governance projector, and retry identity

#### Scenario: Unknown selector cannot bypass coverage
- **WHEN** an unregistered Planning action or operation kind reaches the command boundary
- **THEN** it is refused and cannot default to a read or mutation path without projector and receipt coverage

#### Scenario: Mixed command is advertised conservatively
- **WHEN** MCP exposes annotations for the hidden `plan_memory` spelling
- **THEN** the command-level annotation remains write-capable even though selector dispatch keeps `inspect`, `validate`, `query` and `history` lease-free

#### Scenario: Read and write tools are advertised honestly
- **WHEN** MCP exposes annotations for the Planning tools
- **THEN** `read_plans` is read-only and `change_plans` is write-capable and destructive

#### Scenario: Planning application errors stay on the shared facade contract
- **WHEN** the Planning leaf raises a deliberate public `OpError`
- **THEN** MCP returns a normal `{success: false, error: {code, message, remediation}}` tool result and REST plus CLI `--json` return the identical shared envelope
- **AND** unexpected exceptions retain the existing native or internal-error path instead of being projected as Planning refusals

#### Scenario: Generated surfaces stay identical
- **WHEN** the Planning command schema is inspected through MCP, REST, CLI, OpenAPI, and generated capability artifacts
- **THEN** all surfaces expose the same selector and parameter semantics from one leaf/signature with no hand-maintained duplicate command implementation
- **AND** the MCP renderings `read_plans` and `change_plans` are generated from the same operation entries

## REMOVED Requirements

### Requirement: One multiplexed Records product command

**Reason**: Records are exposed as one read tool and one write tool with discriminated operations; see "Records are exposed as one read tool and one write tool".

**Migration**: `record_memory` stays callable as a hidden compatibility spelling with its previous schema; new guidance names `read_records` and `change_records`.

### Requirement: One multiplexed Planning product command

**Reason**: Planning is exposed as one read tool and one write tool with discriminated operations; see "Planning is exposed as one read tool and one write tool".

**Migration**: `plan_memory` stays callable as a hidden compatibility spelling with its previous schema; new guidance names `read_plans` and `change_plans`.
