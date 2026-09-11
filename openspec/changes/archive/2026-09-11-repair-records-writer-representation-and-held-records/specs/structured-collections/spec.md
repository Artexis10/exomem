## ADDED Requirements

### Requirement: Item validation refusals are field-addressed and complete
Item validation for structured-collection mutations SHALL preserve stable error codes and add machine-readable details naming each failing field path — dotted for nested objects and indexed for arrays — together with the reason and the received value class, and SHALL report every failing field in one response rather than only the first encountered.

#### Scenario: Every undeclared field is named
- **WHEN** a candidate item carries two fields the schema does not declare
- **THEN** the refusal keeps `SCHEMA_UNKNOWN_FIELD` and its details list both field names

#### Scenario: Nested failure is addressed by path
- **WHEN** the third element of a declared array-of-object field carries a value of the wrong type
- **THEN** the refusal details name that element's path with its index and the failing sub-field, the reason, and the received value class

## MODIFIED Requirements

### Requirement: Collection-scoped item identity and exact source versioning
Every safely mutable item SHALL expose an identity tuple `(collection_uuid, canonical_item_key)` and an item version derived from its exact current source bytes. New agent-authored Markdown items SHALL receive an explicit UUID item key through the active semantic profile's declared ID property and marker contract. Query-only datasets MAY expose a bounded manifest-declared string key, but arbitrary dataset keys SHALL NOT be treated as global Exomem IDs. Standalone Records references SHALL retain `exomem://record/<collection-uuid>/<percent-encoded-key>`; standalone Planning references SHALL use `exomem://plan/<collection-uuid>/<percent-encoded-key>`. A reference parser SHALL require the namespace to match the selected profile. An explicit item key that is not a UUID SHALL refuse with `INVALID_RECORD_ID`; when the supplied value equals a natural-key field value of the candidate, or the candidate's declared natural key is complete, the refusal SHALL name the declared natural key and explain that identity derives from it when the key is omitted.

#### Scenario: Explicit identity survives a non-semantic edit
- **WHEN** a user changes an item field, title, body, or path without changing its explicit item identifier
- **THEN** the item identifier and profile-specific reference remain stable and its item version changes

#### Scenario: Legacy deterministic identity remains queryable
- **WHEN** a legacy Markdown Record block has no explicit item identifier but its declared natural key is unique
- **THEN** the adapter serializes schema version plus natural-key fields in declared order using Unicode-NFC strings, normalized ISO dates/datetimes, explicit JSON nulls, and typed JSON scalars, then returns a deterministic collection-scoped compatibility key marked as inferred

#### Scenario: Corrected inferred natural key can change compatibility identity
- **WHEN** a user corrects a natural-key field on an unmarked legacy Record item
- **THEN** the inferred compatibility key may change and Exomem does not claim it is a durable substitute for an explicit item key

#### Scenario: Duplicate legacy natural key refuses update
- **WHEN** two authorized items in one collection resolve to the same canonical item key
- **THEN** both remain inspectable but targeted update or triage refuses with an ambiguity error and names no arbitrary winner

#### Scenario: Namespace mismatch refuses
- **WHEN** a Planning operation receives an `exomem://record/...` item reference or a Records operation receives an `exomem://plan/...` item reference
- **THEN** resolution refuses the profile mismatch without searching by the encoded key alone

#### Scenario: Natural-key value supplied as an item key refuses with remediation
- **WHEN** an append supplies the candidate's natural-key value as `item_key`
- **THEN** the refusal keeps `INVALID_RECORD_ID`, names the declared natural-key fields and the received value, and states that identity derives from the natural key when `item_key` is omitted

### Requirement: Manual-edit visibility and report-only inspection
Queries SHALL read current canonical files so ordinary editor and Obsidian changes become visible without AI mediation. Collection inspection SHALL detect out-of-band source changes, duplicate or missing identities, schema violations, missing templates, audit-history gaps, and stale saved-view provenance without rewriting canonical files. Collection inspection SHALL report held candidates and coverage counts without adopting, rewriting, or counting them as items. Generic derived-index repair SHALL remain owned by `maintain_memory(mode="reconcile", dry_run=false)`.

#### Scenario: Direct edit appears on next query
- **WHEN** a user adds, changes, or removes a valid item directly in an ordinary editor
- **THEN** the next fresh query reflects that canonical state and reports a changed source snapshot

#### Scenario: Inspect reports but does not repair canonical ambiguity
- **WHEN** manual edits create duplicate item identities or ambiguous legacy keys
- **THEN** `record_memory(action="inspect")` reports the exact issue and leaves canonical files untouched, while `maintain_memory` may repair only derived indexes

#### Scenario: Inspect reports an undeclared manual field
- **WHEN** a human adds a property that is not declared by the collection schema to an otherwise readable Record item
- **THEN** inspection reports a schema violation without dropping, rewriting, or silently adopting the property

#### Scenario: Inspection reports a held candidate without adopting it
- **WHEN** a collection directory contains a held candidate beside its items
- **THEN** inspection reports it under coverage with its reference and diagnostics summary, the item count excludes it, and no canonical file is rewritten
