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
