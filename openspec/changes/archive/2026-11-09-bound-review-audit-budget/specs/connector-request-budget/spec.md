## ADDED Requirements

### Requirement: Audits Consume Their Request Budget

Budgeted audits SHALL check their remaining allowance before expensive preparation and checks, and between bounded units of work. They SHALL reserve time for final release and delivery. Deadline exhaustion SHALL stop further audit work without an abandoned background scan. Existing local and reconcile exemptions SHALL remain unchanged.

#### Scenario: Corpus preparation exhausts its allowance
- **WHEN** the request allowance expires during corpus preparation
- **THEN** preparation stops at its next bounded checkpoint
- **AND** no incomplete shared corpus or cache is published
- **AND** the report marks corpus-dependent coverage incomplete

#### Scenario: A category exceeds its allowance
- **WHEN** a category exhausts the audit allowance
- **THEN** the audit discards that category's incomplete findings
- **AND** the budget names the interrupted stage and the stages that never started
- **AND** earlier completed checks remain eligible for release

#### Scenario: A local audit has no request budget
- **WHEN** the same audit runs through an existing unbudgeted local or reconcile path
- **THEN** its complete findings, counts and ordering retain their existing meaning

#### Scenario: Interruption crosses a dependency boundary
- **WHEN** an audit expires during a resolver proof or accepted store read batch
- **THEN** the dependency stops cooperatively under the same allowance
- **AND** interruption does not become a cache miss, fallback scan or empty successful check

#### Scenario: A caller expires while sharing a corpus build
- **WHEN** an audit waiter exhausts its allowance during another caller's corpus build
- **THEN** the waiter stops without cancelling the owner's build
- **AND** an expired owner's abandoned build does not pass its request-specific failure to other callers

### Requirement: Audited Result Release Preserves Fresh Authorization

A budgeted audit SHALL obtain fresh authorization for its final report and check its allowance throughout release and accepted read batches. If release cannot complete, it SHALL discard the report and return a fixed `AUDIT_BUDGET_EXHAUSTED` terminal in the existing error shape. This terminal SHALL finalize without corpus enumeration, contain no findings, paths or counts, and recommend narrower categories.

#### Scenario: Final filtering cannot finish
- **WHEN** the delivery allowance expires while the final report is being authorized
- **THEN** no part of that report is released
- **AND** the caller receives the content-free audit budget error

#### Scenario: Authorization changes during an audit
- **WHEN** a page becomes withheld after audit preparation
- **THEN** final release uses the current authorization decision
- **AND** an earlier proof never permits the withheld content to escape

#### Scenario: Other read operations use their existing filter
- **WHEN** a request performs ordinary attention or another read operation
- **THEN** this audit-specific release deadline does not change its existing behavior

#### Scenario: Error delivery has no warm artifact census
- **WHEN** audit release expires while governance is active and the artifact census is cold
- **THEN** the fixed budget terminal finishes without walking the vault or constructing that census
- **AND** a caller-supplied message cannot select this internal terminal path
