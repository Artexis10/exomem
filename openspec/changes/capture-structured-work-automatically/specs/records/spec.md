## MODIFIED Requirements

### Requirement: Agents infer Records participation from observed state

The agent-facing Records contract SHALL route durable observed events and state to `record_memory` from semantic fit rather than requiring the user to say “save”, “log”, “record”, or “Records”. Covered observations SHALL include measurements, sessions, symptoms, transactions, maintenance events, inventory changes, status history, and other attributable facts. Recurring structured work SHALL also be covered: stable identities that recur, repeated observations or evaluations of the same items, comparisons, changing state, or a need to query exactly. Recognising it SHALL be the agent's judgement of meaning, never a fixed repetition count or a word list. The routing contract SHALL preserve the existing boundary: future intent belongs to Planning, received raw material to Sources, proof-bearing artifacts to Evidence, and conclusions to Notes.

When exactly one existing collection is compatible and the observation is sufficiently identified for that schema, an agent operating under a proactive engagement policy MAY append or update it and SHALL report the mutation. Ambiguous collections, missing required identity/date/provenance, or uncertain ownership SHALL produce one focused clarification. Compatibility SHALL be judged by meaning and schema, and a repeated item SHALL update the row its natural key names. When no compatible collection exists and the work's identity, type and scope are clear, the agent SHALL create a minimal collection under the served `proactive_capture` disposition and the automatic-creation bounds, SHALL backfill the items already established in the conversation with their dates and attribution, and SHALL report the creation and the rows in its reply. It SHALL NOT create an unreported schema. A passing mention SHALL get no collection. An identity, type or scope that is unclear in a way that changes the action SHALL produce one focused question. Narrative and synthesis SHALL stay in Notes. Feedback from the user, proposals from the assistant and findings from outside sources SHALL be kept as separately attributed values, and an identity the user has not resolved SHALL stay its own row with its ambiguity recorded, never merged.

#### Scenario: Measurement is inferred without a magic verb
- **WHEN** a user states a new dated measurement in context without asking to save, log, record, or use Records and exactly one existing collection accepts it
- **THEN** the agent routes the observation through `record_memory`, preserves the observed value without interpretation, and reports the committed mutation

#### Scenario: No collection produces a proposal
- **WHEN** a durable observed event fits Records, no compatible collection exists, and `proactive_capture` is `advisory`
- **THEN** the agent uses Records discovery and authoring guidance to propose a collection and does not write the event into a Note, Source, Evidence artifact, or silently invented collection

#### Scenario: No collection produces a bounded creation under silent capture
- **WHEN** recurring structured work fits Records but no compatible collection exists
- **AND** `proactive_capture` is `silent`
- **THEN** the agent creates a minimal collection within the automatic-creation bounds, files the established items and reports both, and never writes the items into a Note, Source, Evidence artifact, or unreported collection

#### Scenario: A passing mention gets no collection
- **WHEN** a conversation mentions one item that could recur but shows no recurrence, comparison, changing state or need to query
- **THEN** the agent creates no collection and files no row

#### Scenario: An unresolved identity stays its own row
- **WHEN** two items in recurring structured work may name the same thing and the user has not said whether they do
- **THEN** the agent keeps them as separate rows, records the possible identity, and merges nothing

#### Scenario: Competing collections require one clarification
- **WHEN** two releasable Records collections are equally compatible with the observed event
- **THEN** the agent asks one focused collection-selection question and performs no guessed mutation

#### Scenario: Interpretation remains outside Records
- **WHEN** a Records query supplies values that support a possible conclusion
- **THEN** the Records response remains neutral observed state and any durable conclusion is compiled explicitly into a linked Note

## ADDED Requirements

### Requirement: An automatic collection creation is bounded by the server
`records.create` SHALL accept `automatic: true` for an agent-initiated creation; without it a creation SHALL behave as before. With it, the server SHALL refuse with `AUTOMATIC_CREATION_REFUSED`, naming the failed bound in the details and creating nothing, unless every bound holds:

- the effective `proactive_capture` disposition is `silent`;
- the manifest uses the Records profile and declares a natural key;
- the item schema has at most 12 fields;
- the manifest declares no saved view, reference, rollup or derived rows;
- the vault created fewer than 3 collections automatically in the last 24 hours.

The refusal SHALL say to propose the collection and create it without the flag after the user confirms. The create's receipt SHALL record `automatic: true`, and the collection inventory SHALL report for each collection whether it was created automatically. The bounds SHALL be defined once, beside the check, and SHALL hold on a vault that has a collection store and on a vault that has none. The 24-hour count SHALL read each storage mode's own create record and never scan all history: a store vault reads the collections' create transactions, a file-only vault reads the marker that an automatic create adds to its activity-log entry, and a vault with both counts both.

#### Scenario: A minimal automatic creation commits under silent capture
- **WHEN** `proactive_capture` is `silent` and an agent creates a Records collection with a natural key and 6 fields with `automatic: true`
- **THEN** the collection exists, the receipt records `automatic: true`, and the inventory reports the collection as created automatically

#### Scenario: A minimal automatic creation commits on a file-only vault
- **WHEN** the vault has enrolled no collection store, `proactive_capture` is `silent`, and an agent creates a Records collection with a natural key and 6 fields with `automatic: true`
- **THEN** the collection exists, the receipt records `automatic: true`, and the inventory reports the collection as created automatically

#### Scenario: Advisory capture refuses an automatic creation
- **WHEN** `proactive_capture` is `advisory` and an agent creates a collection with `automatic: true`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED` naming the disposition, and no collection exists

#### Scenario: A schema over the bound is refused
- **WHEN** an agent creates a collection with 13 fields, or with no natural key, with `automatic: true`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED` naming that bound, and the same manifest without the flag still creates normally

#### Scenario: The daily bound holds
- **WHEN** the vault created 3 collections automatically in the last 24 hours and an agent creates a fourth with `automatic: true`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED` naming the daily bound

### Requirement: `add_record` creates a minimal collection for its first row
`add_record` SHALL accept `new_collection`, an object with `natural_key`, a non-empty list of field names that the row's `item` carries. When `collection` is a title that names no collection and `new_collection` is given, the server SHALL draft a minimal Records manifest from the row: the title is `collection`, the declared natural key is required, and each key of `item` is a field typed from its value's JSON shape (string, integer, number, boolean, or date for an ISO calendar-date string), with no saved views, references, rollups or derived rows and the vault's default storage. It SHALL create that collection through the automatic create, so every bound of `records.create` with `automatic: true` holds unchanged, and SHALL append the row, as one governed call with one receipt that names the created collection, records `automatic: true` and names the row. A row that the drafted collection would refuse, including one that lacks a natural-key field or holds a null key, SHALL be refused with the append's own validation code before anything is created. A refused bound SHALL create nothing and SHALL return the confirmed create call, carrying the manifest the server would have created, so that the agent can propose exactly that collection. When `collection` names an existing collection, `new_collection` SHALL be ignored and the call SHALL be a plain append. A repeated call SHALL return the recorded result and SHALL NOT create a second collection.

#### Scenario: One call creates the collection and adds the row on a file-only vault
- **WHEN** the vault has no collection store, `proactive_capture` is `silent`, and an agent calls `add_record` with a title that names no collection, a row and `new_collection`
- **THEN** the collection exists with the row, the receipt records `automatic: true` and names the created collection, and the inventory reports the collection as created automatically

#### Scenario: One call creates the collection and adds the row on a store vault
- **WHEN** the vault has a collection store that a running service serves, `proactive_capture` is `silent`, and an agent makes the same call to that service
- **THEN** the service creates the collection in the store and appends the row on its store thread, the receipt records `automatic: true`, and the inventory reports the collection as created automatically

#### Scenario: Advisory capture refuses and returns the collection it would have created
- **WHEN** `proactive_capture` is `advisory` and an agent calls `add_record` with `new_collection`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED`, no collection or row exists, and the refusal carries the create call with the drafted manifest, which creates the collection when the user confirms

#### Scenario: A row without its natural key creates nothing
- **WHEN** an agent calls `add_record` with `new_collection` and a row that lacks a natural-key field
- **THEN** the server refuses with the append's validation code naming the field, and no collection exists

#### Scenario: An existing title appends without creating
- **WHEN** `collection` names an existing collection and the call carries `new_collection`
- **THEN** the call is a plain append, no collection is created, and the receipt does not say that it created one

#### Scenario: A repeated call creates once
- **WHEN** an agent repeats the same `add_record` call that created a collection, under the same idempotency key or a new one
- **THEN** the call returns the recorded result or a replay of the row, and the vault holds one collection

### Requirement: `new_fields` adds the fields of a row to its collection
`add_record` and `edit_record` SHALL accept `new_fields`, a list of distinct field names that the call's `item` (append) or `changes` (update) carries and that the collection may not declare yet. For a Records collection under silent `proactive_capture`, the server SHALL add each undeclared field to the collection's manifest through the manifest revision and its audit event, typed from the value's JSON shape as `new_collection` types it, and SHALL then write the row, as one call whose reply records `automatic: true` and names the added fields in `added_fields`. Existing fields and rows SHALL stay untouched, and an existing row SHALL hold no value for a new field. A collection that the owner made, or that a confirmed create made, SHALL take added fields with no field-count bound; the server SHALL decide that from the collection's own create record, a store collection's create transaction or a file collection's create line in the activity log, and SHALL treat a collection whose create record it cannot read in full as not created automatically. A name that the collection already declares with the type its value gives SHALL add nothing, so a repeated call writes only the row and adds no field twice. The server SHALL refuse with `AUTOMATIC_FIELDS_REFUSED`, naming the failed bound in the details and changing nothing, when the effective `proactive_capture` is not `silent` or when a collection that was itself created automatically would then declare more than 12 item fields; the refusal SHALL carry the proposal and the confirmed `revise` that makes the change. It SHALL refuse a name that the collection declares with another type and a value that is null, a list or an object, and SHALL refuse a Planning collection. A write that names an undeclared field without `new_fields` SHALL keep refusing `SCHEMA_UNKNOWN_FIELD`; when every issue is an undeclared field, the refusal SHALL carry the call that adds them under silent capture and otherwise the proposal and the confirmed `revise`. Renaming, retyping or removing a field SHALL stay the confirmed `revise`.

#### Scenario: Silent capture adds the fields and writes the row on a file-only vault
- **WHEN** the vault has no collection store, `proactive_capture` is `silent`, and an agent calls `add_record` with a row that carries two fields the collection does not declare, named in `new_fields`
- **THEN** the collection declares both fields, typed from the values, the row is written, the earlier rows hold no value for them, and the reply records `automatic: true` and names them in `added_fields`

#### Scenario: Silent capture adds the field and writes the row on a store vault
- **WHEN** the vault has a collection store that a running service serves, `proactive_capture` is `silent`, and an agent makes the same call for a store collection to that service
- **THEN** the service revises the store collection's manifest and appends the row on its store thread, and the reply names the added field

#### Scenario: A file collection of a served store vault takes the field in process
- **WHEN** the vault is served by a running service and the call names a file collection
- **THEN** the field is added to that collection and the row is written, as on a file-only vault

#### Scenario: A capture that is not silent refuses and returns the proposal
- **WHEN** `proactive_capture` is `advisory` or `off` and an agent calls `add_record` with `new_fields`
- **THEN** the server refuses `AUTOMATIC_FIELDS_REFUSED` naming the disposition, nothing is added or written, and the refusal carries the proposed fields and the confirmed `revise`, after which the row fits the collection

#### Scenario: An undeclared field is refused with the call that adds it
- **WHEN** `proactive_capture` is `silent` and an agent writes a row with an undeclared field and no `new_fields`
- **THEN** the server refuses `SCHEMA_UNKNOWN_FIELD`, holds the candidate, and the refusal carries the call with `new_fields` that resumes it

#### Scenario: An undeclared field under advisory capture is refused with the proposal
- **WHEN** `proactive_capture` is `advisory` and an agent writes a row with an undeclared field and no `new_fields`
- **THEN** the refusal says to propose the field to the user and carries the confirmed `revise`, and carries no call with `new_fields`

#### Scenario: Another refusal beside an undeclared field carries no retry with `new_fields`
- **WHEN** a row has an undeclared field and a value that fails another field's type
- **THEN** the refusal carries no call with `new_fields`

#### Scenario: An automatic collection at the field bound refuses one more field
- **WHEN** a collection on a file-only vault that was created automatically declares 11 fields, and an agent adds one field and then a second
- **THEN** the first call adds the twelfth field and the second is refused `AUTOMATIC_FIELDS_REFUSED` naming the field bound, with no field added and no row written

#### Scenario: An automatic store collection at the field bound refuses one more field
- **WHEN** a store collection that was created automatically declares 12 fields and an agent adds a field
- **THEN** the call is refused `AUTOMATIC_FIELDS_REFUSED` naming the field bound, with no field added and no row written

#### Scenario: A collection the owner made takes a field past 12 on a file-only vault
- **WHEN** `proactive_capture` is `silent` and a collection that was created without `automatic` declares 12 fields, and an agent adds a field
- **THEN** the call adds the thirteenth field and writes the row, and the reply names the added field

#### Scenario: A collection the owner made takes a field past 12 on a store vault
- **WHEN** `proactive_capture` is `silent` and a store collection that was created without `automatic` declares 12 fields, and an agent adds a field
- **THEN** the call adds the thirteenth field and writes the row, and the reply names the added field

#### Scenario: A create record that cannot be read does not refuse the write
- **WHEN** the activity log cannot be read in full and a collection with 12 fields has no create line that the read found
- **THEN** the server treats the collection as not created automatically, adds the field and writes the row

#### Scenario: A field declared with another type is refused and the same type adds nothing
- **WHEN** `new_fields` names a field that the collection declares as a string while its value is a number, or a field that it declares with the type the value gives
- **THEN** the first call is refused naming the field type, and the second writes only the row and reports no added field

#### Scenario: A Planning collection is refused
- **WHEN** `new_fields` names a field for a Planning collection
- **THEN** the server refuses with `RECORDS_PROFILE_REQUIRED` and the collection is unchanged

#### Scenario: A repeated call adds each field once
- **WHEN** an agent repeats a call that added a field, under the same idempotency key or a new one
- **THEN** the call returns the recorded result or a replay of the row, and the collection's manifest holds the field once

#### Scenario: Update adds the field its changes name
- **WHEN** an agent updates a row with `changes` that carry an undeclared field and name it in `new_fields`
- **THEN** the field is added and the row is updated, and the reply names the added field

#### Scenario: A call names only fields it carries
- **WHEN** `new_fields` names a field that the call's `item` does not carry
- **THEN** the call is refused as invalid and the collection is unchanged
