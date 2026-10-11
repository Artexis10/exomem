## ADDED Requirements

### Requirement: Activation associates one relevant due candidate with current material
Resolved activation SHALL carry at most one optional `relevant_due_refs` candidate from the maintained due projection, associated with released current material. Each candidate SHALL contain `ref`, `fingerprint`, `category`, `due_since`, and `anchor`. Empty advice SHALL be omitted. The association SHALL use resolved anchors, served unit parents, or current-state sources. Recent context, upkeep, ambiguity, and partial-only anchors SHALL NOT trigger advice.

#### Scenario: A relevant sixth item reaches activation
- **WHEN** five older unrelated due items precede a due item on material that activation serves
- **THEN** activation returns the relevant item's review ref and existing review fingerprint
- **AND** the response contains no global counts, scores, or generated explanation

#### Scenario: Recent or partial material gives no advice
- **WHEN** a due item's page appears only in recent context or as a partial anchor
- **THEN** activation omits `relevant_due_refs`

#### Scenario: A current-state source has no released canonical reference
- **WHEN** a current-state source releases only its path
- **THEN** `anchor` carries that exact source path, while anchors and units retain their released parent references
- **AND** the association never uses the current-state row's different resolved anchor

### Requirement: Relevant advice uses the existing served due rules
Relevant advice SHALL apply the existing due dates, family dispositions, review decisions, audience recomposition, and shared fingerprint rules after selecting stored page buckets. Missing or unreadable projections SHALL omit advice without starting recovery, reconciliation, or an audit. Subset reads SHALL remain isolated from full served caches. Activation SHALL NOT carry `due_state` or read or advance its emission ledger.

#### Scenario: A dismissed item stays quiet
- **WHEN** the existing review state dismisses the candidate's exact fingerprint
- **THEN** activation omits that candidate until its finding changes

#### Scenario: Missing advice does not consume recall emission
- **WHEN** activation runs with a missing due projection and recall then runs
- **THEN** activation starts no due recovery
- **AND** recall retains its existing recovery and emission behavior

### Requirement: Final filtering retains only associated advice
Every public activation door SHALL keep a due candidate only while matching current material survives final filtering. Hidden rows SHALL NOT affect its selection, fingerprint, or order. L0 material SHALL behave as absent material; existing L1 notices SHALL remain without due-only markers. The bounded hook renderer SHALL retain complete candidate identities or omit the entire candidate.

#### Scenario: Final filtering removes the matching material
- **WHEN** final filtering removes the material that carried the candidate's association
- **THEN** it also removes the candidate without adding a due-only notice

#### Scenario: The hook ceiling cannot fit a candidate
- **WHEN** the remaining render budget cannot fit the complete candidate and its handling instruction
- **THEN** the hook omits the whole candidate and never cuts its ref or fingerprint
