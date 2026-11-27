## MODIFIED Requirements

### Requirement: Review Intent Is Non-Circular During Validation

A validation-only existing edit carrying `relation_disposition="reviewed_none"` and a non-empty `relation_review_reason` SHALL validate all other transition and governance constraints without requiring or comparing `relation_review_hash`. Where a link review applies to the page, the corresponding commit MUST require that the supplied review hash exactly equals the validated transition fingerprint, and a commit that omits the hash SHALL be refused with `INVALID_RELATION_REVIEW`, `missing=["relation_review_hash"]` and a pointer to `semantic.relation_review_hash`; only a present, wrong hash yields `LIFECYCLE_TRANSITION_REVIEW_MISMATCH`. Where no link review applies, such as an entity page, the review fields SHALL be ignored and no refusal follows.

#### Scenario: Validation omits the unknown review hash
- **WHEN** a client validates an otherwise valid edit with reviewed-none intent and no `relation_review_hash`
- **THEN** validation succeeds and returns the required `relation_review_hash`

#### Scenario: Commit supplies the wrong review hash
- **WHEN** a client commits reviewed-none intent to a page that takes a link review, with a hash other than the validated `relation_review_hash`
- **THEN** the write is refused with `LIFECYCLE_TRANSITION_REVIEW_MISMATCH`

#### Scenario: Commit omits the review hash
- **WHEN** a client commits reviewed-none intent with a reason and no `relation_review_hash` to a page that takes a link review
- **THEN** the write is refused with `INVALID_RELATION_REVIEW` and `missing=["relation_review_hash"]`
- **AND** the message names `semantic.relation_review_hash` as the source of the value

#### Scenario: Commit to a page that takes no review omits the review hash
- **WHEN** a client commits reviewed-none intent with a reason and no `relation_review_hash` to an entity page
- **THEN** the write commits, records no review decision, and returns one warning that the review fields were ignored
