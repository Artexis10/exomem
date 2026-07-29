## ADDED Requirements

### Requirement: Self-Describing Reviewed-None Validation

Creation validation responses SHALL expose an additive `relation_review_hash`. When a
reviewed-none decision is required it MUST equal the exact `draft_hash` covered by the
commit; otherwise it SHALL be null. Existing hash fields and their semantics MUST remain
available across MCP, REST, CLI, and direct leaves. Replacement previews MUST rewrite both
public fields to the same predecessor-bound hash required by replacement commit.

#### Scenario: Zero-candidate validation returns the commit hash

- **WHEN** a new compiled note has no qualifying relation and validation requires an
  explicit reviewed-none decision
- **THEN** the response contains `relation_review_hash == draft_hash`
- **AND** the response remains non-mutating

#### Scenario: Reviewed-none is not required

- **WHEN** creation validation is satisfied by a qualifying relation
- **THEN** `relation_review_hash` is null
- **AND** existing validation fields remain unchanged

### Requirement: Reviewed-None Compatibility Alias

The public semantic-write boundary SHALL accept both `reviewed_none` and the previously
advertised `reviewed-none` as input, canonicalize both to `reviewed_none` before hash,
reason, writer-lease idempotency digesting, replay, and persistence checks, and name the
accepted spellings when rejecting any other value. The alias MUST NOT relax applicability,
unchanged-draft, hash-match, reason, or replay requirements.

#### Scenario: Advertised hyphen spelling reaches one canonical receipt

- **WHEN** a caller commits an unchanged reviewed-none draft using `reviewed-none`, the
  returned relation review hash, and a valid explicit reason
- **THEN** the commit succeeds under the same checks as `reviewed_none`
- **AND** durable review state stores only canonical `reviewed_none`

#### Scenario: Alias does not bypass review integrity

- **WHEN** either accepted spelling is supplied with a wrong hash, changed draft, missing
  reason, or inapplicable disposition
- **THEN** the existing semantic-contract error is returned
- **AND** no Markdown or auxiliary review state is written

#### Scenario: Alias and canonical spelling share one explicit replay identity

- **WHEN** the same valid mutation and explicit idempotency key are first sent with
  `reviewed-none` and retried with `reviewed_none`
- **THEN** writer-lease digesting treats both requests as the same canonical mutation
- **AND** the stored receipt remains underscore-only
