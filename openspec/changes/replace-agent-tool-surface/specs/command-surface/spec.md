## ADDED Requirements

### Requirement: The Agent Surface Lists Twenty-Three Typed Tools And Two Doors

The MCP surface SHALL list exactly these tools: recall_context, search_memory, read_page, read_instructions, create_note, add_observation, edit_note, supersede_note, save_source, save_evidence, save_episode, upload_files, download_files, query_records, add_record, edit_record, query_plan_items, add_plan_item, edit_plan_item, list_review_items, decide_review_item, inspect_memory and manage_memory. A Cloud surface SHALL also list observe_transfer and transfer_memory until the gateway routes door calls. Each typed tool SHALL have one job and one effect class, flat typed fields and no nested operation union; edit_note SHALL be the only typed tool with an `action` selector. A tool that adds without changing existing content SHALL NOT carry a destructive annotation.

#### Scenario: The listing is the closed set
- **WHEN** a client lists tools on a local surface
- **THEN** it receives exactly the 23 names
- **AND** a Cloud surface receives those names and the two Cloud transfer tools

#### Scenario: An additive tool is not destructive
- **WHEN** a client reads the annotations of add_observation
- **THEN** the tool is not read-only and is not destructive

### Requirement: Two Doors Carry The Long Tail By Effect

inspect_memory SHALL carry every read operation that has no typed tool, and manage_memory SHALL carry every write operation that has no typed tool. A door call SHALL be `{"operation": "<area>.<action>", "arguments": {...}}`. `operation` SHALL be a closed enum generated from the operation entries, and `arguments` MAY arrive as a JSON string that the server decodes once. inspect_memory SHALL be read-only, manage_memory SHALL carry the write and destructive annotations, and manage_memory SHALL accept no `preview`: the read twin of an operation on inspect_memory SHALL be its dry run. A door operation SHALL keep the leaf field names of its grouped command. Every operation entry SHALL have exactly one public home, and a startup check SHALL prove it.

#### Scenario: A door call reaches its grouped command
- **WHEN** a client calls manage_memory with `schema.save` and the arguments of a schema save
- **THEN** the call authorizes, leases and records as the schema write command

#### Scenario: A read twin previews a write
- **WHEN** a client calls inspect_memory with `files.create`
- **THEN** the call validates the creation and writes nothing

#### Scenario: A write named on the read door is refused
- **WHEN** a client calls inspect_memory with an operation that only manage_memory carries
- **THEN** the refusal names manage_memory and returns a retry

### Requirement: Help Comes On Demand From The Operation Entries

`read_instructions(topic)` SHALL return the arguments and one example for a tool name, the operations of a door for a door name, and the schema of one operation, from either door, for an operation name. The schema SHALL be generated from the operation entries and SHALL carry the credential-presence text of governance operations. A wrong door call SHALL return a `retry` rendered for the active surface and up to five `candidates`. There SHALL be no `help` operation on either door.

#### Scenario: A topic names an operation
- **WHEN** a client calls read_instructions with topic `schema.save`
- **THEN** the reply lists the arguments of the operation and one example call to manage_memory

#### Scenario: A wrong operation returns candidates
- **WHEN** a client calls manage_memory with an unknown operation
- **THEN** the refusal lists up to five near operations and a retry call

### Requirement: An Internal Command Name Is Not Callable

A call to the name of a grouped command, a retired tool name or a legacy alias SHALL return the refusal for an unknown tool, before authorization runs. The server SHALL expose no compatibility alias. The grouped command names SHALL remain the identity of receipts, the writer lease, egress and the ledger, and REST and CLI SHALL keep their names.

#### Scenario: A retired tool name is refused
- **WHEN** a client calls `record_memory` or `change_observations` over MCP
- **THEN** the call is refused as an unknown tool and no write occurs

### Requirement: A Reply Spells Its Calls For The Surface That Serves It

A reply that names a call SHALL spell it as the public tool that the serving surface lists. A structured route `{"tool", "args"}` in any result SHALL be rewritten to the public tool and arguments that carry it, and a refusal that names the other side of an operation SHALL name the public tool that serves that side. A route that no public tool carries SHALL stay as authored, so that a finished call never fails for a hint. A grouped surface (REST, CLI and a direct call) SHALL keep the grouped spelling.

#### Scenario: A route in a reply is callable
- **WHEN** a client saves a source and asks for compile guidance
- **THEN** the `preparation_route` of the reply names inspect_memory with the operation `sources.compile`
- **AND** sending that call returns the preparation draft

#### Scenario: A refusal names the serving door
- **WHEN** a client sends a read-only operation to manage_memory
- **THEN** the refusal names inspect_memory and not a grouped command

#### Scenario: A route no public tool carries stays as authored
- **WHEN** a result holds a route for an operation that no public tool carries
- **THEN** the result is returned with that route unchanged

### Requirement: The Note Tools Carry The Vocabulary Decision

create_note and supersede_note SHALL accept an optional `vocabulary_decision` with `evidence_fingerprint`, `outcome` (one of reuse, create or defer) and an optional `canonical`. The call SHALL pass the decision to the write unchanged, so that a caller can answer the `vocabulary_preparation` that a preview returned.

#### Scenario: A preview asks and the next call answers
- **WHEN** a preview of create_note returns a vocabulary preparation with an evidence fingerprint
- **THEN** a call to create_note with `vocabulary_decision` that carries that fingerprint and an outcome commits the note

### Requirement: A Typed Tool Carries Every Field Its Job Needs

Each typed tool SHALL accept the fields that its grouped command needs for the job the tool names, as flat typed fields or as one `options` object for rare knobs. add_record SHALL accept `options` with `delivery` and `item_key`. edit_record SHALL accept `refresh_presentation`, with `changes` allowed to be empty when it is true. create_note, supersede_note and save_source SHALL accept `projects`. edit_note SHALL accept each entry of `edits` as an object or as a JSON string that holds one. decide_review_item SHALL accept an optional `relation` and the recorded vocabulary decision that names a relation. list_review_items SHALL list the `procedure_approvals` queue. edit_plan_item SHALL move an item to a status with `status`.

#### Scenario: A connector sends encoded edits
- **WHEN** a connector sends each entry of `edits` as a JSON string
- **THEN** edit_note applies the entries as if they were objects

#### Scenario: A presentation refresh needs no change
- **WHEN** a client calls edit_record with `refresh_presentation` and no changes
- **THEN** the managed Markdown view of the row is rebuilt and no column changes

#### Scenario: A status move spells as edit_plan_item
- **WHEN** guidance spells a plan transition that moves only the status
- **THEN** the spelled call is edit_plan_item with `status`

## MODIFIED Requirements

### Requirement: Bootstrap Is Exposed On Every Generated Surface
The system SHALL expose `bootstrap` through the single command registry on REST, CLI, and OpenAPI, and SHALL expose its read on MCP as `read_instructions`. The MCP tool SHALL be marked read-only and non-destructive in MCP annotations.

#### Scenario: Bootstrap appears in generated surfaces
- **WHEN** the server is built
- **THEN** `read_instructions` appears in the MCP tool list
- **AND** `/api/bootstrap` appears in the REST facade and OpenAPI document
- **AND** the CLI exposes a `bootstrap` subcommand

#### Scenario: Bootstrap is accounted for by schema fidelity tests
- **WHEN** the MCP schema fidelity test runs
- **THEN** the live tool set includes `read_instructions`
- **AND** `read_instructions` is generated from the public tool table rather than hand-registered as an exception

### Requirement: Edit Memory Advertises One Discriminated Operation

The public `edit_note` tool SHALL remain one tool and SHALL advertise a required `action` selector. Supported actions MUST preserve current behavior for `replace_body`, `set_tags`, `set_fields`, `replace_text` (single and batch), `replace_section`, `fill_row`, `edit_observation` and `remove_observation`, except that `replace_body` SHALL require the page id that a read returned and `replace_section` SHALL accept an optional `create_missing` boolean, false by default. When `create_missing` is true and the heading is absent, `replace_section` SHALL append a new level-2 section that holds `new_text`, under the same version guard, and SHALL leave the rest of the body unchanged apart from its trailing newlines. A `heading` that is empty or spans more than one line SHALL refuse with `INVALID_EDIT` before mutation. Otherwise a missing heading SHALL refuse with `HEADING_NOT_FOUND` and a remediation that names the flag. Each action SHALL refuse a field that belongs to another action, and SHALL expose only guards that the underlying operation enforces. The grouped `edit_memory` command SHALL keep its nested `operation` form on REST and CLI.

#### Scenario: Agent selects replace string
- **WHEN** the client inspects the `replace_text` action
- **THEN** it sees the exact old and new text, replace-all, the batch list and the preview field
- **AND** it does not see frontmatter, row, section or whole-body fields

#### Scenario: Invalid variant fields are submitted
- **WHEN** a caller adds a field belonging to another action or omits a required action field
- **THEN** validation fails before mutation with the selected action and precise field guidance

#### Scenario: A whole-body edit without a hash is refused
- **WHEN** a caller sends `edit_note` with `replace_body` and no page id, or sends the grouped `edit_memory` command on REST or CLI with a whole-body edit and no `expected_hash`
- **THEN** the call fails with `INVALID_EDIT` before mutation, saying to send the page id or `content_hash` of a whole-page read
- **AND** the page is unchanged, including for a preview

#### Scenario: The legacy edit alias refuses the same way
- **WHEN** a REST or CLI caller sends the opt-in legacy `edit` alias or a direct runtime call with `new_body` and no `expected_hash`
- **THEN** the call fails with `INVALID_EDIT` naming `expected_hash`
- **AND** the same call with the page's current `content_hash` succeeds

#### Scenario: A missing section is created on request
- **WHEN** a caller sends `edit_note` with `replace_section` whose heading is absent from the page and `create_missing` set to true
- **THEN** the page gains one new level-2 section with that heading and the new text at the end of the body
- **AND** the rest of the body is unchanged apart from its trailing newlines, and the `updated` stamp moves as for any edit

#### Scenario: A missing section refuses without the flag
- **WHEN** a caller sends `replace_section` whose heading is absent and omits `create_missing`
- **THEN** the call fails with `HEADING_NOT_FOUND` before mutation
- **AND** the remediation names `create_missing` as the retry

#### Scenario: A section is not created under a heading that is not one line
- **WHEN** a caller sends `replace_section` with `create_missing` set to true and a `heading` that is empty or spans more than one line
- **THEN** the call fails with `INVALID_EDIT` before mutation
- **AND** the page is unchanged

### Requirement: Artifact preservation appears in capability guidance

The product command surface SHALL describe `save_evidence` as the canonical binary-evidence capability and `upload_files` as its transport helper for clients without file handles. Full bootstrap guidance SHALL provide copyable direct-file and fallback upload calls and SHALL distinguish obtaining an upload capability from successfully storing bytes.

#### Scenario: Full bootstrap teaches both transport paths

- **WHEN** a client requests the full bootstrap profile
- **THEN** the response shows a `save_evidence(files=[...], scope=..., category=...)` call for clients with file handles
- **AND** it shows `upload_files` plus `/upload` delivery for clients without file handles
- **AND** neither example embeds binary base64 or a long-lived upload secret

### Requirement: Preferred Public Names Preserve Stable Command Identity

The product registry SHALL define preferred public names independently of stable execution identities.
The preferred names SHALL be `recall_context`, `search_memory`, `create_note`, `edit_note`, `supersede_note` and `read_page` for the existing activation, search, creation, edit, supersession and page-read commands respectively.
MCP SHALL accept only the preferred names. REST and CLI SHALL resolve both the previous and the preferred spelling directly to the same command.
The system SHALL preserve parameters, defaults, response shapes, authorization, disclosure, writer leases, mutation replay, and operation boundaries.
Compatibility spellings on REST and CLI SHALL have no automatic expiry.
Existing requirements that name an execution identity SHALL retain their operation contract under both spellings.
Discovery and documentation for that operation SHALL use its preferred name.

#### Scenario: Discovery recommends one spelling per operation

- **WHEN** a client reads MCP discovery, bootstrap recommendations, OpenAPI, or CLI help
- **THEN** it sees each preferred name instead of the previous name
- **AND** previous spellings remain callable on REST and CLI without duplicate operation listings

#### Scenario: Retry crosses spellings without another mutation

- **WHEN** a mutation submitted through one REST or CLI spelling is retried with the same request identity and arguments through the other spelling
- **THEN** the existing mutation replay returns its original outcome
- **AND** no second write occurs

#### Scenario: Compatibility cannot restore a withheld capability

- **WHEN** surface, tier, or Cloud policy excludes a command
- **THEN** neither spelling bypasses that exclusion

#### Scenario: Existing clients keep their argument and result contracts

- **WHEN** a REST or CLI client calls either spelling with previously valid arguments
- **THEN** the command applies the same validation, guards, and result contract
- **AND** activation inputs, task refresh, map/profile references, continuation, and note summary/topics retain their behavior

### Requirement: Shared public tools preserve their complete jobs

A public tool MAY combine related operations from existing execution commands. Each typed call SHALL select exactly one execution command before authorization. The combination SHALL preserve response capabilities, guards, retry identity and error envelopes. Existing grouped calls SHALL remain callable on REST and CLI.

#### Scenario: A graph view retains its coverage contract

- **WHEN** `inspect_memory` runs `graph.map`
- **THEN** it retains drawing limits, complete or partial coverage state, counts and the original graph response
- **AND** it does not substitute a context catalogue map

#### Scenario: File preservation retains its transport identity

- **WHEN** a client preserves evidence files
- **THEN** `save_evidence` retains its operation identity, top-level file handles and transport metadata
- **AND** exact retries preserve the original command's mutation identity

#### Scenario: Shared reads retain dataset admission

- **WHEN** tier 2 is disabled
- **THEN** the `inspect_memory` operation enum and its invocation resolver exclude `dataset.query`
- **AND** enabling tier 2 retains the dataset reader's permissions, provenance and query result contract

#### Scenario: A registry preview checks the live overlay

- **WHEN** a caller runs `schema.preview` through `inspect_memory` with a keyed registry delta
- **THEN** the server invokes the grouped proposal operation against the current overlay
- **AND** the preview preserves existing custom entries and checks collisions and the registry cap without saving
- **AND** discovery distinguishes this operation from complete-document validation

#### Scenario: An existing import client retains its run

- **WHEN** a caller starts an import with `imports.start` through `manage_memory` and later reads it with `imports.status` through `inspect_memory`
- **THEN** both calls retain the same execution identity and address the same run

### Requirement: Converted tools keep the grouped command as their execution identity

A public tool call SHALL normalize into the grouped command's selector value and arguments before authorization, payload hashing, idempotency, the writer lease, the call ledger, the governance projector and the egress postfilter. Validation, guards, refusals, receipts and response shapes SHALL be those of the grouped command for the same arguments. Any component that routes by tool name SHALL resolve a public name to its command and normalized arguments, and an unresolvable name SHALL fail closed.

#### Scenario: A retry crosses spellings
- **WHEN** a client submits an `append` through `add_record` and retries the same idempotency key and arguments through the REST route of `record_memory`
- **THEN** the retry returns the committed result and no second write occurs

#### Scenario: Authorization sees the grouped command
- **WHEN** a governed caller invokes `add_plan_item`
- **THEN** credential routing, the authorization decision and the disclosure receipt are the same as for `plan_memory` with `action="add"`

#### Scenario: Store-routed collections are unchanged
- **WHEN** `query_records` queries a collection that the collection store serves
- **THEN** the same store routing, result and receipts apply as for `record_memory` with `action="query"`

### Requirement: Records are exposed as one read tool and one write tool

The product surface SHALL expose Records through `query_records`, `add_record` and `edit_record`, and through the `inspect_memory` and `manage_memory` operations of the `records` area, rather than storage-specific tools. `query_records` SHALL cover the bounded list, filter, sort and continuation of rows, and the listing of collections when it names none. `add_record` SHALL append one row. `edit_record` SHALL update one row that a row id names. The `records.describe`, `records.validate` and `records.history` operations SHALL read on `inspect_memory`, and `records.create`, `records.bulk_upsert`, `records.discard`, `records.revise`, `records.rebaseline` and `records.import` SHALL write on `manage_memory`. Query SHALL cover bounded list, history, render and export-shaped responses through explicit arguments; generic derived-index repair SHALL remain a maintenance operation.

#### Scenario: Agent discovers before creating
- **WHEN** an agent needs to create a first Record collection without prior manifest knowledge
- **THEN** it uses `records.describe` and `records.validate` through `inspect_memory` before `records.create` through `manage_memory`

#### Scenario: Natural log intent finds the Records tools
- **WHEN** an agent receives “Log this training session”, “Record today’s symptoms”, “Update the car mileage”, or “Show the last three months”
- **THEN** bootstrap routes the reads through `query_records` and the writes through `add_record` and `edit_record` rather than advertising storage-specific tools

#### Scenario: Existing manifest uses the Records tools
- **WHEN** an agent needs to validate, revise, or explicitly rebaseline an existing collection manifest
- **THEN** it validates through `records.validate` on `inspect_memory` and revises or rebaselines through `manage_memory` rather than a generic file editor or storage-specific tool

#### Scenario: Storage strategy is not a tool choice
- **WHEN** the selected collection uses a Markdown log, Markdown items, or a dataset
- **THEN** the same tools resolve the manifest and adapter without making the agent select a storage-specific public command

#### Scenario: Dataset mutation refuses through the write tool
- **WHEN** `add_record` or `edit_record` resolves a dataset-backed collection
- **THEN** the command refuses that action as unsupported in this delivery and does not accept a caller-supplied replacement file

### Requirement: Planning is exposed as one read tool and one write tool

The product surface SHALL expose Planning through `query_plan_items`, `add_plan_item` and `edit_plan_item`, and through the `inspect_memory` and `manage_memory` operations of the `plans` area, rather than separate capture, horizon, hierarchy, manifest-lifecycle, or storage-specific tools. `query_plan_items` SHALL return the Planning inventory when it names no collection and query the collection otherwise. `add_plan_item` SHALL add one item, and `edit_plan_item` SHALL update or triage one item that a plan item id names. The `plans.validate` and `plans.history` operations SHALL read on `inspect_memory`, and `plans.create`, `plans.revise` and `plans.rebaseline` SHALL write on `manage_memory`. Query SHALL cover bounded horizon, date, history, hierarchy, render and export-shaped responses through explicit arguments; generic derived-index repair and previewed structured-file migration SHALL remain maintenance operations.

#### Scenario: Natural planning intent finds the Planning tools
- **WHEN** an agent receives “save this feature idea”, “file this bug for later”, “make this a quarterly initiative”, “what matters this week”, “show my multi-year outcomes”, or “revise this Planning collection”
- **THEN** bootstrap routes reads through `query_plan_items` and writes through `add_plan_item`, `edit_plan_item` or `plans.revise` instead of advertising a family of narrow tools

#### Scenario: Planning storage is not a tool choice
- **WHEN** an agent captures, queries, validates, or revises Planning intent
- **THEN** the same tools resolve the Planning collection and Markdown-item adapter without asking the user to select an internal storage operation

#### Scenario: Existing manifest uses the Planning tools
- **WHEN** an agent needs to validate, revise, or explicitly rebaseline an existing Planning collection manifest
- **THEN** it validates through `plans.validate` on `inspect_memory` and revises or rebaselines through `manage_memory` rather than a generic file editor or storage-specific tool

#### Scenario: Inventory before a collection is known
- **WHEN** an agent calls `query_plan_items` with no collection
- **THEN** the response is the bounded Planning inventory and nothing is created or resolved

#### Scenario: Review does not hide inside query
- **WHEN** a caller queries a plan that carries Records evidence descriptors
- **THEN** `query_plan_items` returns authored Planning state and descriptors without evaluating planned-versus-recorded progress or silently invoking epistemic review

### Requirement: Semantic Authoring Guidance Is Projected Consistently And Bounded

Every generated surface that teaches or performs semantic writes SHALL project the same semantic authoring contract identity, short role-first selection rule, one compact example, and route to full bootstrap guidance. The MCP tool definitions SHALL be the exception: they carry the observation line syntax in the `content` description of create_note, and the contract identity SHALL reach an MCP client through bootstrap and `read_instructions`, because the schema byte budget leaves no room for it in each tool definition. Full bootstrap, the public reference, generic scaffold, and workflow skills SHALL project the complete core vocabulary. Parity tests MUST cover contract identity and the appropriate bounded/full projection. Intentional description changes MUST be regenerated and reviewed as a bounded fixture change.

#### Scenario: Generated schemas stay small and cannot drift

- **WHEN** surface projection tests run
- **THEN** REST/OpenAPI and CLI write surfaces identify the same contract version and short selection rule without duplicating the full sixteen-label table
- **AND** the MCP tool definitions carry no contract identity text and the create_note `content` description names the observation line syntax
- **AND** bootstrap/reference/skill projections expose the exact complete vocabulary under the same contract identity

## REMOVED Requirements

### Requirement: Grouped names stay callable as hidden compatibility spellings
**Reason**: The agent surface now lists 23 typed tools and two doors, and an internal command name is not callable over MCP. "An Internal Command Name Is Not Callable" replaces the hidden compatibility spellings.
**Migration**: An MCP client calls the listed tool for the operation. REST and CLI keep the grouped spelling.

### Requirement: Product tools separate reads from writes and expose each operation as a typed variant
**Reason**: Typed flat tools and the two doors replace the nested `operation` union with a `kind` discriminator. "The Agent Surface Lists Twenty-Three Typed Tools And Two Doors" and "Two Doors Carry The Long Tail By Effect" define the new shapes, and each typed tool declares exactly the fields of its one job.
**Migration**: An MCP client sends the flat fields of `query_records`, `add_record`, `edit_record`, `query_plan_items`, `add_plan_item` or `edit_plan_item`, or a door `operation` with its `arguments`.
