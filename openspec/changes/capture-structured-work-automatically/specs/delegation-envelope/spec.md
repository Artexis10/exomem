## REMOVED Requirements

### Requirement: Collection creation is restructure execution
**Reason**: The product now creates a minimal Records collection under the owner's capture setting and within fixed bounds; holding every creation at confirm-required contradicted that intent.
**Migration**: Replaced by "Collection creation is bounded capture or confirmed restructuring". A creation outside the bounds, a Planning collection, and every schema change other than adding fields, merge, deletion and migration keep the confirm-required treatment.

## ADDED Requirements

### Requirement: Collection creation is bounded capture or confirmed restructuring

Creating one Records collection within the automatic-creation bounds of the `records` capability SHALL be a `proactive_capture` action and follow its served disposition: `silent` creates it and reports it in the same reply, `advisory` proposes it and creates it after one inline confirmation, and `off` initiates nothing while a creation the user asks for is still served. Every other collection creation and schema change SHALL remain `restructure_execution`, and bootstrap SHALL serve this split.

#### Scenario: The refusal words its way forward by disposition
- **WHEN** an automatic creation is refused under `advisory` or under `off`
- **THEN** the refusal under `advisory` says to propose and create after the confirmation, and the refusal under `off` says to create only if the user asked for it

#### Scenario: The remaining actions keep their class
- **WHEN** an agent creates a collection outside the bounds, creates a Planning collection, or makes a schema change other than adding fields, a merge, a deletion or a migration
- **THEN** the action is `restructure_execution`: confirm-required at every prominence level, satisfiable by one inline confirmation of the named proposal, never by a standing delegation, while appending to an existing collection remains `proactive_capture`

#### Scenario: The routing advisory and the candidate finding stay advisory
- **WHEN** the runtime reports a `records_routing` advisory or a `collection_candidate` finding
- **THEN** both are advisory material under `structural_suggestions` and never create, append, move or delete anything at any prominence, and a `disposition` on the advisory derives from the effective capture level and directs only the agent's own `proactive_capture` append through `record_memory`

#### Scenario: A candidate never creates a collection on its own
- **WHEN** a strong `collection_candidate` is served at prominence `maximal`
- **THEN** no collection exists until the agent creates it under the served `proactive_capture` disposition or the user confirms a proposal in the conversation; the runtime creates none

#### Scenario: Silent capture creates a bounded collection and reports it
- **WHEN** `proactive_capture` is `silent` and the agent creates a collection that meets the automatic-creation bounds
- **THEN** the creation needs no question, and the agent's reply names the created collection and the rows it filed

#### Scenario: Advisory capture proposes before creating
- **WHEN** `proactive_capture` is `advisory` and recurring structured work has no compatible collection
- **THEN** the agent proposes the named collection and creates it only after one inline confirmation

#### Scenario: A creation outside the bounds stays confirm-required
- **WHEN** a collection the agent would create exceeds an automatic-creation bound, or is a Planning collection
- **THEN** the creation is `restructure_execution` and needs one inline confirmation at every prominence

#### Scenario: Standing delegation for restructuring is refused by name
- **WHEN** a user asks that schema changes, merges or deletions of collections be done automatically from now on
- **THEN** the request is refused as a standing delegation above the v1 ceiling, and the one-action confirmation remains available

#### Scenario: The routing advisory never appends
- **WHEN** a `records_routing` advisory is returned on a committed write
- **THEN** no record has been appended by the runtime; the agent's subsequent append follows the served `proactive_capture` disposition

#### Scenario: A backfill is a question even at maximal
- **WHEN** a grouped backfill item is served at prominence `maximal`
- **THEN** the agent asks the user once before filing any of the named notes

#### Scenario: A file disposition is the agent's append
- **WHEN** a `records_routing` advisory carries `disposition: file` at prominence `maximal`
- **THEN** no record exists until the agent performs the returned payload, which is governed like any other append

### Requirement: An additive field change is bounded capture

Adding one or more fields to an existing Records collection, within the bounds that the `records` capability defines for `new_fields`, SHALL be an action of the `proactive_capture` class and follow its served disposition: `silent` adds the fields and reports them in the same reply, `advisory` proposes them and adds them after one inline confirmation, and `off` initiates no change while a change the user asks for is still served.

#### Scenario: The refusal under off words its way forward by request
- **WHEN** an addition is refused under `off`
- **THEN** the refusal says to make the change only if the user asked for it

#### Scenario: The field-count bound counts only an automatic collection
- **WHEN** an agent adds fields under silent capture
- **THEN** the field count is bounded only in a collection that was itself created automatically

#### Scenario: The restructuring actions keep their class
- **WHEN** an agent renames, retypes or removes a field, re-keys, merges, deletes or migrates a collection, or changes the schema of a Planning collection
- **THEN** the action is `restructure_execution`, with its confirmation and its refusal of a standing delegation

#### Scenario: Silent capture adds a field and reports it
- **WHEN** `proactive_capture` is `silent` and the agent adds a field to a Records collection within the bounds
- **THEN** the change needs no question, and the agent's reply names the added fields and the row it filed

#### Scenario: A collection the owner made takes added fields without a field-count bound
- **WHEN** `proactive_capture` is `silent` and the agent adds a field to a collection that the user made or a confirmed create made, which already declares 12 fields
- **THEN** the change needs no question, and the agent's reply names the added field and the row it filed

#### Scenario: Advisory capture proposes before adding
- **WHEN** `proactive_capture` is `advisory` and a later fact has no field in its collection
- **THEN** the agent proposes the named field and adds it only after one inline confirmation

#### Scenario: Any other schema change stays confirm-required
- **WHEN** the agent would rename, retype or remove a field of a collection
- **THEN** the change is `restructure_execution` and needs one inline confirmation at every prominence

## MODIFIED Requirements

### Requirement: Handing over an artifact does not bypass write authority

Handing a generated file to the user, or a definite report that an offered artifact was sent or published, SHALL be the adoption trigger but SHALL NOT count as explicit confirmation to write Exomem. Agent-initiated exact-byte Source or Evidence adoption and any later compatible delivery Record SHALL be `proactive_capture` and SHALL obey its active off, advisory or silent disposition.

#### Scenario: A requested save creates no standing authority
- **WHEN** the user explicitly asks to save or preserve an artifact
- **THEN** that is an ordinary requested action and creates no standing authority for later artifacts

#### Scenario: Structural work never blocks adoption
- **WHEN** no compatible Records collection exists for a delivery Record
- **THEN** a collection is created only under the bounded collection-creation rule, any schema mutation other than the bounded addition of fields is separate confirmed `restructure_execution`, and adoption success never depends on that structural work

#### Scenario: Hand-over under silent capture preserves exact bytes
- **WHEN** the agent hands the user one generated file and `proactive_capture` is `silent`
- **THEN** the active agent adopts exactly that file through the semantically correct Source or Evidence lane
- **AND** the hand-over grants no standing authority for files the agent never hands over or for future files

#### Scenario: Advisory capture surfaces before writing
- **WHEN** the agent hands the user one generated file and `proactive_capture` is `advisory`
- **THEN** the agent surfaces the proposed exact-byte adoption before invoking the write
- **AND** it does not describe the hand-over alone as write confirmation

#### Scenario: Off capture leaves a handed-over file unwritten
- **WHEN** the agent hands the user one generated file but `proactive_capture` is `off` and the user has not explicitly requested preservation
- **THEN** the agent performs no Source, Evidence, adoption-receipt, or delivery-Record write

#### Scenario: Explicit save remains requested action
- **WHEN** the user explicitly asks to save a generated file in Exomem
- **THEN** the agent may invoke the correct existing preservation lane as a requested action
- **AND** that request creates no standing delegation for another file

#### Scenario: Delivery observation obeys the same capture posture
- **WHEN** a local adoption receipt exists and a definite delivery event is eligible for an existing compatible Records collection
- **THEN** the agent-initiated Record follows `proactive_capture`
- **AND** absence of a compatible collection permits only a creation under the bounded collection-creation rule, never an unreported schema
