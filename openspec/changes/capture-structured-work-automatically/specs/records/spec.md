## MODIFIED Requirements

### Requirement: Agents infer Records participation from observed state

The agent-facing Records contract SHALL route durable observed events and state, and recurring structured work, to `record_memory` from semantic fit, without requiring the user to say “save”, “log”, “record” or “Records”. Recognising the work SHALL be the agent's judgement of meaning, never a fixed repetition count or a word list. The contract SHALL keep the boundary: future intent is Planning, raw material is Sources, proof-bearing artifacts are Evidence, and conclusions are Notes.

#### Scenario: Covered observations are routed
- **WHEN** a conversation carries a measurement, session, symptom, transaction, maintenance event, inventory change, status history or other attributable fact
- **THEN** the contract routes it to `record_memory`

#### Scenario: Recurring structured work is covered
- **WHEN** a conversation shows stable identities that recur, repeated observations or evaluations of the same items, comparisons, changing state, or a need to query exactly
- **THEN** the contract treats it as Records work

#### Scenario: One compatible collection is written and reported
- **WHEN** exactly one existing collection is compatible, judged by meaning and schema, and the observation is sufficiently identified for it
- **THEN** an agent under a proactive engagement policy may append or update it, a repeated item updates the row its natural key names, and the agent reports the mutation

#### Scenario: Missing identity or unclear scope gets one question
- **WHEN** required identity, date or provenance is missing, ownership is uncertain, or the work's identity, type or scope is unclear in a way that changes the action
- **THEN** the agent asks one focused question

#### Scenario: A clear identity with no collection gets a minimal one
- **WHEN** no compatible collection exists and the work's identity, type and scope are clear
- **THEN** the agent creates a minimal collection under the served `proactive_capture` disposition and the automatic-creation bounds, backfills the items already established in the conversation with their dates and attribution, reports the creation and the rows in its reply, and creates no unreported schema

#### Scenario: Narrative stays in Notes and attribution stays separate
- **WHEN** recurring structured work carries narrative, the user's feedback, the assistant's proposals or outside findings
- **THEN** the narrative and synthesis stay in Notes, and feedback from the user, proposals from the assistant and findings from outside sources are kept as separately attributed values

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

`records.create` SHALL accept `automatic: true` for an agent-initiated creation, and without it SHALL behave as before. With it, the server SHALL refuse `AUTOMATIC_CREATION_REFUSED`, naming the failed bound in the details and creating nothing, unless every bound in the scenarios holds. The bounds SHALL be defined once, beside the check, and SHALL hold on a vault with a collection store and on a vault with none.

#### Scenario: Every bound is checked
- **WHEN** an agent creates a collection with `automatic: true`
- **THEN** the server creates it only if the effective `proactive_capture` is `silent`, the manifest uses the Records profile and declares a natural key, the item schema has at most 12 fields counting the properties of an object field and of an array's items, the manifest has no key beyond those of the minimal manifest (`type`, `exomem_id`, `title`, `semantic_profile`, `collection_version`, `schema_version`, `lifecycle`, `storage` and `item_schema`, whose own keys are `natural_key` and `fields`) and declares no reference, and the vault created fewer than 3 collections automatically in the last 24 hours

#### Scenario: A refusal leaves the confirmed create available
- **WHEN** an automatic creation is refused
- **THEN** the refusal says to propose the collection and create it without the flag after the user confirms, except under `off`, where it says to create it without the flag only if the user asked for it, and in every case the confirmed create stays available

#### Scenario: The receipt and the inventory report the flag
- **WHEN** an automatic creation commits
- **THEN** its receipt records `automatic: true` and the collection inventory reports for each collection whether it was created automatically

#### Scenario: The 24-hour count reads each storage mode's own record
- **WHEN** the server counts the automatic creations in the last 24 hours
- **THEN** a store vault reads the collections' create transactions, a file-only vault reads the marker that an automatic create adds to its activity-log entry, a vault with both counts both, and no count scans all history

#### Scenario: A minimal automatic creation commits under silent capture
- **WHEN** `proactive_capture` is `silent` and an agent creates a Records collection with a natural key and 6 fields with `automatic: true`
- **THEN** the collection exists, the receipt records `automatic: true`, and the inventory reports the collection as created automatically

#### Scenario: A minimal automatic creation commits on a file-only vault
- **WHEN** the vault has enrolled no collection store, `proactive_capture` is `silent`, and an agent creates a Records collection with a natural key and 6 fields with `automatic: true`
- **THEN** the collection exists, the receipt records `automatic: true`, and the inventory reports the collection as created automatically

#### Scenario: Advisory capture refuses an automatic creation
- **WHEN** `proactive_capture` is `advisory` and an agent creates a collection with `automatic: true`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED` naming the disposition, and no collection exists

#### Scenario: Advisory and off capture word the way forward differently
- **WHEN** an agent creates a collection with `automatic: true` under `advisory`, and again under `off`
- **THEN** the first refusal says to propose the collection and create it after the user confirms, and the second says to create it only if the user asked for it

#### Scenario: A schema over the bound is refused
- **WHEN** an agent creates a collection with 13 fields, or with no natural key, with `automatic: true`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED` naming that bound, and the same manifest without the flag still creates normally

#### Scenario: Nested properties count toward the field bound
- **WHEN** an agent creates a collection with 3 fields, one of them an object that declares 11 properties, with `automatic: true`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED` naming the field bound with 14 fields, and a manifest that totals 12 commits

#### Scenario: A key beyond the minimal manifest is refused
- **WHEN** an agent creates a collection with `automatic: true` whose manifest declares `governance`, `templates`, saved views or a key of `item_schema` beyond `natural_key` and `fields`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED` naming the authored features, and the same manifest without the flag still creates

#### Scenario: The daily bound holds
- **WHEN** the vault created 3 collections automatically in the last 24 hours and an agent creates a fourth with `automatic: true`
- **THEN** the server refuses `AUTOMATIC_CREATION_REFUSED` naming the daily bound

### Requirement: `add_record` creates a minimal collection for its first row

`add_record` SHALL accept `new_collection`, an object with `natural_key`, a non-empty list of field names that the row's `item` carries. When `collection` is a title that names no collection, the server SHALL draft a minimal Records manifest from the row, create it through the automatic create so that every bound of `records.create` with `automatic: true` holds, and append the row, as one governed call with one receipt.

#### Scenario: The manifest is drafted from the row
- **WHEN** the server drafts the collection from the row
- **THEN** the title is `collection`, the declared natural key is required, each key of `item` is a field typed from its value's JSON shape (string, number for every JSON number, boolean, or date for an ISO calendar-date string), the manifest declares no saved views, references, rollups or derived rows, and storage is the vault's default

#### Scenario: The receipt names the creation and the row
- **WHEN** the call commits
- **THEN** the receipt names the created collection and the row and records `automatic: true`

#### Scenario: A title with a control character is refused
- **WHEN** `collection` holds a control character, a line break included
- **THEN** the call is refused with `INVALID_COLLECTION_REFERENCE` and creates nothing

#### Scenario: A row that the drafted collection would refuse creates nothing
- **WHEN** the row lacks a natural-key field or holds a null key, or fails the drafted schema
- **THEN** the call is refused with the append's own validation code before anything is created

#### Scenario: A refused bound returns the confirmed create
- **WHEN** a bound refuses the automatic creation
- **THEN** nothing is created and the refusal carries the confirmed create call with the manifest the server would have created, so the agent can propose exactly that collection

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

`add_record` and `edit_record` SHALL accept `new_fields`, a list of distinct field names that the call's `item` (append) or `changes` (update) carries and that the collection may not declare yet. Under silent `proactive_capture` the server SHALL add each undeclared field to a Records collection's manifest through the manifest revision and its audit event, typed from the value's JSON shape (every JSON number is `number`), then write the row in one call that names them in `added_fields`.

#### Scenario: The reply records the addition and leaves earlier data untouched
- **WHEN** fields are added
- **THEN** the reply records `automatic: true`, existing fields and rows stay untouched, and an existing row holds no value for a new field

#### Scenario: The create record decides the field-count bound
- **WHEN** an addition would pass 12 fields
- **THEN** a collection that the owner made, or that a confirmed create made, takes the fields with no field-count bound; the server decides that from the collection's own create record, a store collection's create transaction or a file collection's create line in the activity log, and treats a collection whose create record it cannot read in full as not created automatically

#### Scenario: A refusal changes nothing and carries the proposal
- **WHEN** the effective `proactive_capture` is not `silent`, or a collection created automatically would declare more than 12 item fields
- **THEN** the server refuses `AUTOMATIC_FIELDS_REFUSED` naming the failed bound and changes nothing; the refusal carries the proposal and the confirmed `revise`, keeps the proposal and names why in `retry_unavailable` when that revise cannot be drafted, and under `off` says to make the change only if the user asked for it

#### Scenario: The revise edits only the fields
- **WHEN** the confirmed `revise` is drafted
- **THEN** it adds each field at the end of the fields mapping, leaves every other byte, and refuses, writing nothing, an edit that would change anything but the fields

#### Scenario: An undeclared field without new_fields keeps its refusal
- **WHEN** a write names an undeclared field without `new_fields`
- **THEN** it keeps refusing `SCHEMA_UNKNOWN_FIELD`; when every issue is an undeclared field, the refusal carries the call that adds them under silent capture and otherwise the proposal and the confirmed `revise`, and renaming, retyping or removing a field stays the confirmed `revise`

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

#### Scenario: A declared field adds nothing and its value is judged by the row
- **WHEN** `new_fields` names a field that the collection declares as a string, with a value that is a date-shaped string or a number
- **THEN** the first call writes only the row and reports no added field, and the second is refused by the row's own type validation

#### Scenario: A whole number then a fraction
- **WHEN** a first row adds a field with `new_fields` and the value `4`, and a later row carries `4.5` for it
- **THEN** the field is declared as a number and both rows commit

#### Scenario: A field that cannot be typed from one row is refused
- **WHEN** `new_fields` names an undeclared field whose value is null, a list or an object
- **THEN** the call is refused naming the field type and the collection is unchanged

#### Scenario: A field is added after a last field written as a block
- **WHEN** a collection's last field declares a block-style enum or a block scalar and a key follows its fields, and an agent adds a field
- **THEN** the field lands at the end of the fields mapping, the manifest stays valid and every other key and value is unchanged

#### Scenario: A refused field keeps its proposal when the revise cannot be drafted
- **WHEN** a collection's fields are written in flow style and an agent writes an undeclared field under `advisory`
- **THEN** the refusal carries the proposal and names why no revise is ready in `retry_unavailable`

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
