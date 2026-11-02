## MODIFIED Requirements

### Requirement: Review Intent Is Non-Circular During Validation

A validation-only existing edit carrying `relation_disposition="reviewed_none"` and a non-empty `relation_review_reason` SHALL validate all other transition and governance constraints without requiring or comparing `relation_review_hash`. The corresponding commit MUST require that the supplied review hash exactly equals the validated transition fingerprint. A commit that omits the hash SHALL be refused with `INVALID_RELATION_REVIEW`, `missing=["relation_review_hash"]` and a pointer to `semantic.relation_review_hash`; only a present, wrong hash yields `LIFECYCLE_TRANSITION_REVIEW_MISMATCH`.

#### Scenario: Validation omits the unknown review hash
- **WHEN** a client validates an otherwise valid edit with reviewed-none intent and no `relation_review_hash`
- **THEN** validation succeeds and returns the required `relation_review_hash`

#### Scenario: Commit supplies the wrong review hash
- **WHEN** a client commits reviewed-none intent with a hash other than the validated `relation_review_hash`
- **THEN** the write is refused with `LIFECYCLE_TRANSITION_REVIEW_MISMATCH`

#### Scenario: Commit omits the review hash
- **WHEN** a client commits reviewed-none intent with a reason and no `relation_review_hash`
- **THEN** the write is refused with `INVALID_RELATION_REVIEW` and `missing=["relation_review_hash"]`
- **AND** the message names `semantic.relation_review_hash` as the source of the value
