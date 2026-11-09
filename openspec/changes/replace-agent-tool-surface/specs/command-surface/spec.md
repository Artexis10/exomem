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
