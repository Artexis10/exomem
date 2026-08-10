## ADDED Requirements

### Requirement: Existing Edit Validation Binds Exact Committed Bytes

Every `edit_memory` kind SHALL render server-owned normalization, including the `updated:` stamp, before hashing the proposed page. A commit using the returned transition token SHALL reuse the reviewed render context and SHALL write bytes whose content hash equals the validation `after_hash`.

#### Scenario: Updated frontmatter varies across pages and edit kinds
- **WHEN** each advertised edit kind is validated and committed against pages whose `updated:` field is stale, current, or absent
- **THEN** each commit succeeds with the returned transition token
- **AND** the validation `after_hash` equals the hash of the committed Markdown bytes

#### Scenario: No-op frontmatter patch crosses a clock tick
- **WHEN** a client validates setting a frontmatter field to its current value and commits after the server clock advances
- **THEN** the commit reuses the validated stamp and passes the exact-state transition check

### Requirement: Existing Validation Returns The Review Input Explicitly

Existing-page validation SHALL return the exact transition fingerprint under `relation_review_hash`. It SHALL also return direct `before_hash` and `after_hash` values. The legacy `transition_hash` alias MAY remain for compatibility but MUST equal `relation_review_hash`.

#### Scenario: Client inspects validation response
- **WHEN** `edit_memory` returns a successful validation response
- **THEN** `relation_review_hash` is present and names the value accepted by the commit field of the same name
- **AND** `before_hash` and `after_hash` identify the evaluated Markdown states

### Requirement: Review Intent Is Non-Circular During Validation

A validation-only existing edit carrying `relation_disposition="reviewed_none"` and a non-empty `relation_review_reason` SHALL validate all other transition and governance constraints without requiring or comparing `relation_review_hash`. The corresponding commit MUST require that the supplied review hash exactly equals the validated transition fingerprint.

#### Scenario: Validation omits the unknown review hash
- **WHEN** a client validates an otherwise valid edit with reviewed-none intent and no `relation_review_hash`
- **THEN** validation succeeds and returns the required `relation_review_hash`

#### Scenario: Commit supplies the wrong review hash
- **WHEN** a client commits reviewed-none intent with a hash other than the validated `relation_review_hash`
- **THEN** the write is refused with `LIFECYCLE_TRANSITION_REVIEW_MISMATCH`

### Requirement: Every Edit Kind Can Recover From A Stale Disposition

All advertised edit kinds, including fill-row, SHALL support validation and the transition/relation-review fields required to recover from `RELATION_DISPOSITION_STALE` without changing to another operation.

#### Scenario: Fill-row page has stale disposition
- **WHEN** a client validates a fill-row edit on a page with a stale relation disposition and then commits with the returned transition and review values
- **THEN** the row edit commits through the same governed existing-page transition contract
