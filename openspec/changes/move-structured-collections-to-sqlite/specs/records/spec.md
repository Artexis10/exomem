## ADDED Requirements

### Requirement: Records is a built-in collection type and record_memory is its typed facade
`records` SHALL be a built-in collection type of kind `observed`, placement `Records`, extensible by each collection's own item schema, with default audience `policy`. `record_memory` SHALL be a typed facade: its actions SHALL map onto the generic collection operations through the built-in declaration's wire map (`append` to `add`, and every other action one to one). Its argument sets, `record_id` naming, `_record_receipt` receipts, error codes and `describe` contract SHALL be unchanged for Records collections. `record_memory` SHALL also serve collections of declared types that no facade owns, without a new tool. For those it SHALL use generic item naming, a `_collection_receipt` marker and generic error codes. The added surface SHALL be exactly two things: an `action` value `transition`, which takes the existing `item_key`, `expected_item_version`, `why` and `changes` (naming the target state), and an optional `collection_type` argument on `describe`, which teaches a named type from its declaration. The generated `record_memory` schema SHALL grow by no more than 400 bytes. It SHALL refuse Planning collections.

#### Scenario: Records wire is unchanged by the facade
- **WHEN** the same append, update and query are issued against a Records collection before and after the facade is introduced
- **THEN** arguments, receipts, error codes and query envelopes are identical in shape

#### Scenario: A declared type's item is added through record_memory
- **WHEN** an agent appends to a collection of a declared `recipes` type and later transitions it to `current`
- **THEN** both commit through the generic operations with `_collection_receipt` receipts, and the transition is checked against the declared state machine

#### Scenario: The added surface stays within its byte budget
- **WHEN** the local `record_memory` tool schema is generated before and after declared-type support
- **THEN** the only differences are the `transition` action value and the optional `collection_type` argument, and the schema grows by at most 400 bytes

#### Scenario: Frozen hosted candidates do not see declared-type actions
- **WHEN** the released hosted candidate schemas are compared with their pinned digests
- **THEN** they are byte-identical, and only the local surface and the v5 candidate list `transition` and the `collection_type` describe argument

## MODIFIED Requirements

### Requirement: Refused Record writes are held, inspectable and resumable
When a Records append or update refuses for a candidate-content reason (representability, undeclared field, schema type or enum failure), the writer SHALL by default preserve the complete candidate (item values, body, changes and rationale) as one held candidate in the collection store, distinct from items, with the machine-readable diagnostics attached. It SHALL render the held candidate as a read-only view under the collection's `Held/` directory, and SHALL return the held reference beside the unchanged refusal. Held candidates SHALL NOT be counted as items, returned by queries, opened by recall, or recorded as item transitions, and holding SHALL NOT advance the collection generation or audit head. The caller SHALL be able to resume a held candidate by reference through the same action with optional field overrides; a successful resume commits exactly one item and removes the held candidate in the same store transaction, and a repeated refusal updates the held diagnostics in place under the same reference. The caller SHALL be able to discard a held candidate by reference with a rationale. A caller MAY decline holding for one call. If the hold itself cannot be written, the original refusal SHALL be returned unchanged with a warning and no second error. Held candidates, including held view corrections, SHALL be subject to the same release filtering as items before they are disclosed or counted.

#### Scenario: Refusal holds the candidate
- **WHEN** an append refuses for an undeclared field
- **THEN** the response carries the refusal, the field-addressed diagnostics and a held reference, a held candidate exists in the store with a read-only view outside the item source, and the collection's item count, query results, recall candidates and audit head are unchanged

#### Scenario: Resume by reference commits once
- **WHEN** the caller resumes the held reference with an override that removes the undeclared field
- **THEN** exactly one item is committed, one transition is recorded, the held candidate and its view are removed, and inspection reports zero held candidates

#### Scenario: Repeated refusal re-holds in place
- **WHEN** a resumed candidate refuses again
- **THEN** the same held reference remains with updated diagnostics and no second held candidate is created

#### Scenario: Declined hold refuses plainly
- **WHEN** the caller declines holding and the append refuses
- **THEN** no held candidate is written and the refusal is returned with its field-addressed details

#### Scenario: Discard removes the candidate
- **WHEN** the caller discards a held reference with a rationale
- **THEN** the held candidate and its view are removed, the audit head is unchanged, and inspection no longer lists it

#### Scenario: Hold failure does not mask the refusal
- **WHEN** the held candidate cannot be written
- **THEN** the original refusal is returned with a warning and without a held reference

#### Scenario: Withheld candidate is invisible
- **WHEN** release rules would withhold the candidate's values from the requesting audience
- **THEN** inspection and inventory report neither its reference nor its count to that audience

#### Scenario: Editing a held view changes nothing
- **WHEN** a user edits a held candidate's view
- **THEN** the held candidate is unchanged and the view is re-rendered
