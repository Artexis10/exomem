## ADDED Requirements

### Requirement: Representability is a property of the storage strategy
Records item values SHALL be judged representable according to the collection's storage strategy. For Markdown-item storage, string values MAY contain line breaks and other control characters; before commit the writer SHALL serialise the complete candidate frontmatter, parse it back with the same reader the collection uses, normalise both sides through the item schema, and refuse with `UNREPRESENTABLE_RECORD_VALUE` naming the field path only when a parsed value differs from its candidate value. For Markdown-log storage, heading values, notes and delimited child-row values SHALL continue to refuse line breaks and the row delimiter, naming the field path. A committed Markdown-item value SHALL read back equal to the candidate value.

#### Scenario: Multi-line text commits into a Markdown item
- **WHEN** a Markdown-item append supplies a required string field whose value contains two consecutive line breaks
- **THEN** the append commits, the item's canonical frontmatter parses back to the identical string, and a digest computed over the read-back value equals the digest computed over the candidate

#### Scenario: Markdown-log still refuses a line break
- **WHEN** a Markdown-log append supplies a heading, note or child-row value containing a line break
- **THEN** it refuses with `UNREPRESENTABLE_RECORD_VALUE` whose details name the field path and the reason, and nothing is written

#### Scenario: Round-trip mismatch refuses before commit
- **WHEN** serialising a candidate value would parse back to a different value
- **THEN** the writer refuses naming the field path and stages nothing

### Requirement: Refused Record writes are held, inspectable and resumable
When a Records append or update refuses for a candidate-content reason (representability, undeclared field, schema type or enum failure), the writer SHALL by default preserve the complete candidate — item values, body, changes and rationale — as one human-owned held file under the collection, distinct from items, with the machine-readable diagnostics attached, and SHALL return the held reference beside the unchanged refusal. Held files SHALL NOT be counted as items, returned by queries, opened by recall, or included in the collection's audit chain, and holding SHALL NOT advance the audit head. The caller SHALL be able to resume a held candidate by reference through the same action with optional field overrides; a successful resume commits exactly one item and removes the held file within the same guarded mutation, and a repeated refusal updates the held diagnostics in place under the same reference. The caller SHALL be able to discard a held candidate by reference with a rationale. A caller MAY decline holding for one call. If the hold itself cannot be written, the original refusal SHALL be returned unchanged with a warning and no second error. Held candidates SHALL be subject to the same release filtering as items before they are disclosed or counted.

#### Scenario: Refusal holds the candidate
- **WHEN** an append refuses for an undeclared field
- **THEN** the response carries the refusal, the field-addressed diagnostics and a held reference, a held file exists under the collection outside the item source, and the collection's item count, query results, recall candidates and audit head are unchanged

#### Scenario: Resume by reference commits once
- **WHEN** the caller resumes the held reference with an override that removes the undeclared field
- **THEN** exactly one item is committed, the audit head advances exactly once, the held file is removed, and inspection reports zero held candidates

#### Scenario: Repeated refusal re-holds in place
- **WHEN** a resumed candidate refuses again
- **THEN** the same held reference remains with updated diagnostics and no second held file is created

#### Scenario: Declined hold refuses plainly
- **WHEN** the caller declines holding and the append refuses
- **THEN** no held file is written and the refusal is returned with its field-addressed details

#### Scenario: Discard removes the candidate
- **WHEN** the caller discards a held reference with a rationale
- **THEN** the held file is removed, the audit head is unchanged, and inspection no longer lists it

#### Scenario: Hold failure does not mask the refusal
- **WHEN** the held file cannot be written
- **THEN** the original refusal is returned with a warning and without a held reference

#### Scenario: Withheld candidate is invisible
- **WHEN** release rules would withhold the candidate's values from the requesting audience
- **THEN** inspection and inventory report neither its reference nor its count to that audience

## MODIFIED Requirements

### Requirement: Records authoring is self-describing and safely preflightable
The Records product command SHALL expose content-free `describe` and `validate` actions in addition to collection inspection and mutation. `describe` SHALL return the complete supported manifest contract, all closed enum values, exact open constraints, and generic minimal and nested-measurement examples. `validate` SHALL run the binding parser, Records-profile rule, safe-path rules, create-only checks, and scaffold checks without requiring a mutation reason, acquiring writer authority, writing an audit event, or changing the vault. Item validation refusals SHALL name every failing field path with its reason and received value class in one response. `describe` SHALL state that `item_key` is the internal UUID identity and that item identity derives from the declared natural key when `item_key` is omitted.

#### Scenario: Generic client creates the first collection without guessing
- **GIVEN** an empty sample vault and a client with no repository, skill, or fixture-manifest access
- **WHEN** the client calls `describe`, authors a manifest only from that response, and calls `validate`
- **THEN** validation succeeds without any guessed field name or enum value
- **AND** the same manifest can be created, inspected, and appended through the public command

#### Scenario: Laboratory example teaches nested observed measurements
- **WHEN** a client requests the Records authoring contract
- **THEN** the complete example shows a panel date, provenance link, and child analytes with values, inequalities, units, ranges, cancellation, and specimen qualifiers
- **AND** it contains no diagnosis, interpretation, private identity, or domain-specific storage engine

#### Scenario: Validation performs no mutation
- **WHEN** a valid or invalid manifest is submitted to `validate`
- **THEN** no manifest, source, directory scaffold, activity event, governance receipt, or writer-lease mutation is produced

#### Scenario: Refusal names every failing field
- **WHEN** an append supplies two schema-invalid values and one undeclared field
- **THEN** one refusal lists all three field paths, each with its reason and received value class

#### Scenario: Natural-key value supplied as item key
- **WHEN** `item_key` carries a value equal to one of the candidate's natural-key field values, or any non-UUID while the candidate's natural key is complete
- **THEN** the refusal keeps `INVALID_RECORD_ID`, and its details name the declared natural key, state that `item_key` is the internal UUID, and say to omit it

### Requirement: Records inventory is available before a selector is known
Calling `record_memory(action="inspect")` without a collection SHALL return a bounded, governance-filtered inventory of releasable first-class Records manifests and exact Records-layer legacy trackers. Supplying a collection SHALL preserve targeted inspection behavior. Inventory SHALL NOT parse legacy item grammar or return item contents. Inventory SHALL report per-collection coverage counts of committed items and held candidates, and targeted inspection SHALL report coverage with bounded held references, both governance-filtered.

#### Scenario: Empty vault inventory is useful and empty
- **WHEN** a generic client inspects an empty Records layer without a collection selector
- **THEN** it receives empty first-class and legacy inventories plus the route to `describe`

#### Scenario: Denied inventory candidate stays absent
- **WHEN** governance withholds a first-class manifest or legacy tracker
- **THEN** inventory does not reveal its path, title, identity, type, or existence

#### Scenario: Inventory shows a blocked ledger
- **WHEN** one collection holds a refused candidate
- **THEN** inventory reports that collection's committed and held counts without returning candidate contents, and targeted inspection lists the held reference, its timestamp, attempted action and a diagnostics summary
