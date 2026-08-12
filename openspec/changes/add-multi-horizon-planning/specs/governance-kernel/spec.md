## ADDED Requirements

### Requirement: Planning authorization precedes identity and reduction
Every Planning discovery, inspection, read, structured query, lifecycle total, horizon grouping, hierarchy assembly, rendered view, export-shaped response, template return, history projection, and mutation receipt SHALL pass the existing governance and release boundary at the correct manifest or item granularity. Structured Planning SHALL require `LEVEL_FULL` (L6) for every artifact whose complete values, hashes, identity, contents, or relationships contribute to a response. Authorization SHALL occur before public count, cap, ordering, parsing, schema finding, identity ambiguity, snapshot, continuation, grouping, hierarchy, latest selection, or rendering.

#### Scenario: Withheld Planning collection is indistinguishable from absent
- **WHEN** a Planning manifest is below L6 for the caller
- **THEN** discovery, inspection, query, and mutation use the same public missing shape as a nonexistent collection

#### Scenario: Hidden item cannot shape a horizon view
- **WHEN** a Planning collection contains both released and withheld items
- **THEN** only authorized items contribute to counts, horizon groups, ordering, pagination, derived renderings, and truncation metadata

#### Scenario: Hidden parent cannot reveal hierarchy
- **WHEN** a released work item names a parent or area that is withheld
- **THEN** the target is authorized before identity, title, or kind is parsed and the public result/refusal is indistinguishable from a missing target

#### Scenario: Excerpt permission cannot authorize full Planning values
- **WHEN** a caller has L5 but not L6 for a Planning manifest, item, template, or linked governed value
- **THEN** `plan_memory` returns none of its full values, hashes, identity, hierarchy, counts, or existence while ordinary recall may still apply its independent excerpt rules to an eligible manifest

### Requirement: Planning item granularity and mutation require complete authorized state
The Markdown-item adapter SHALL receive an immediate path-authorization callback and SHALL authorize each candidate at L6 before it can affect public file/byte caps, ordering, parsing, diagnostics, identity ambiguity, relationship validation, source versions, continuation snapshots, or reductions. Internal raw-walk bounds SHALL remain fail-closed and reveal no count. Authorized-only snapshots SHALL mean a hidden-only edit does not invalidate another caller's continuation. Planning mutation SHALL refuse when the caller cannot receive the complete canonical collection snapshot required for safe hierarchy and container-CAS validation.

#### Scenario: Hidden malformed item is never parsed
- **WHEN** a below-L6 Planning item is malformed, duplicates an ID, or would exceed a public candidate cap
- **THEN** the structured result is identical to that item being absent and the file cannot cause a public parse error, ambiguity, count, or cap exhaustion

#### Scenario: Hidden-only edit preserves released continuation
- **WHEN** only a withheld Planning item changes after a released-only first page
- **THEN** the authorized continuation identity remains stable and reveals no hidden change

#### Scenario: Partial-view mutation refuses
- **WHEN** a caller can read only a subset of a Planning collection and requests add, update, or triage
- **THEN** mutation refuses before publication because an authorized subset cannot substitute for the complete canonical snapshot and relationship graph

### Requirement: Planning egress and precommit receipts are default-deny
Planning responses SHALL use shape-specific typed default-deny projectors. Ordinary schema values require L6; Planning references, parent/area edges, exact Records saved-view pointers, external execution pointers, templates, paths, identities, hashes, audit/history, conflicts, continuations, counts, groupings, and derived provenance SHALL be recursively shape-validated and projected before egress. Opaque Records collection references and execution references SHALL remain opaque and SHALL NOT trigger local stable-ID, wikilink, path, or remote-system resolution. Their disclosure authority is exactly the containing L6 Planning item; target existence or authorization SHALL NOT change their shape.

Mutation authorization and disclosure SHALL run through a precommit hook inside the guarded Planning mutation after final canonical re-read and relationship resolution but before guarded batch publication. Failure SHALL leave canonical files and activity history untouched. Governance receipts, Planning audit events, operational journals, and terminal mutation receipts SHALL remain distinct and SHALL not claim publication that did not commit.

#### Scenario: Stale refusal leaks no current Planning content
- **WHEN** an unauthorized or stale Planning update is refused
- **THEN** the response provides only bounded remediation and metadata allowed at the caller's release level, without current title, body, relationships, evidence, or execution references

#### Scenario: Evidence descriptor cannot leak a governed link
- **WHEN** an authorized Planning item contains a syntactically valid opaque Records collection and saved-view pointer whose target is hidden or absent
- **THEN** the exact pointer round-trips because Planning neither resolves nor target-authorizes it, while withholding the containing item suppresses the entire pointer

#### Scenario: Opaque execution reference is not resolved
- **WHEN** an execution pointer resembles a private vault path, memory reference, or external URL
- **THEN** Planning validates only its bounded opaque syntax and does not use target existence or authorization to change the public shape

#### Scenario: Disclosure failure precedes publication
- **WHEN** the precommit governance or receipt hook refuses or fails
- **THEN** no Planning item, `plan_audit` head, activity event, or committed terminal is published

#### Scenario: Publication failure does not forge governance commit evidence
- **WHEN** precommit disclosure succeeds but guarded publication later rolls back
- **THEN** the governance receipt records only the authorization attempt and does not claim that the Planning mutation committed
