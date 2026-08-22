# openspec-record-discipline Specification

## Purpose
Define the repository contract for strict OpenSpec validation, evidence-based
archive eligibility, canonical-superset preservation, and same-delivery closure
of completed change records.

## Requirements

### Requirement: The complete OpenSpec surface is required validation
The repository's required continuous-integration contract SHALL run strict validation over both active changes and canonical specs.

#### Scenario: An active delta is invalid
- **WHEN** an active change fails strict OpenSpec validation while canonical specs remain valid
- **THEN** the required OpenSpec job fails

#### Scenario: Canonical specs are invalid
- **WHEN** a canonical spec fails strict OpenSpec validation while active changes remain valid
- **THEN** the required OpenSpec job fails

### Requirement: Fully checked active changes cannot accumulate silently
The repository SHALL provide a deterministic read-only audit that lists every active change with a non-empty task list whose tasks are all checked and SHALL return failure while any such change remains active.

#### Scenario: A complete task list remains active
- **WHEN** an active change has at least one task and every task is checked
- **THEN** the audit fails and names the change for archive or task-state correction

#### Scenario: A change has genuine remaining tasks
- **WHEN** an active change has one or more unchecked tasks
- **THEN** the archive-debt audit does not classify it as task-complete

### Requirement: Archive eligibility is evidence-based
An OpenSpec change SHALL be archived only after shipped code, tests, and delivery evidence establish that its implemented scope is complete; task checkboxes alone MUST NOT be treated as proof of shipment.

#### Scenario: Checkboxes and implementation disagree
- **WHEN** task state conflicts with code, test, or merge evidence
- **THEN** archive eligibility follows the implementation evidence and the inaccurate task state is recorded or corrected

### Requirement: Archive sync preserves the canonical superset
Archiving SHALL merge the change delta into canonical specs, preserve the complete change artifact history under a dated archive path, and MUST NOT remove scenarios or requirements introduced by later implemented changes.

#### Scenario: A modified delta is stale
- **WHEN** a `MODIFIED` block omits scenarios already present in the canonical requirement
- **THEN** archive sync refuses until the delta is refreshed from the full current canonical requirement

#### Scenario: A requirement is already synchronized
- **WHEN** an active delta adds a requirement that is already canonical with equivalent semantics
- **THEN** the archive may skip that duplicate spec operation only after recording the equivalence and still preserves the change artifacts

### Requirement: Batch archive ordering is deterministic
Multi-change archive repair SHALL process implemented prerequisites before dependants, otherwise in ascending `created:` order, and SHALL re-run strict validation after each bounded tranche.

#### Scenario: A modified requirement depends on an unarchived base
- **WHEN** a selected change modifies a requirement introduced by another implemented active change
- **THEN** the base change is synchronized before the modifying change

### Requirement: Completed changes close with their delivery
Repository delivery instructions SHALL require an implemented OpenSpec change to be synchronized and archived in the delivery that completes it unless named work remains genuinely incomplete.

#### Scenario: Implementation is complete
- **WHEN** all accepted change scope is implemented and verified
- **THEN** the delivery includes canonical spec sync and a dated archive of the change

#### Scenario: Named work remains
- **WHEN** a change retains explicit unimplemented or external acceptance work
- **THEN** it remains active and the remaining work stays visible rather than being declared complete
