## MODIFIED Requirements

### Requirement: Existing Corpus Is Grandfathered Without Silent Worsening
Older pages lacking the relation/category contract SHALL stay readable and editable with migration warnings and enter activation/review queues, not bulk rewritten. Each error finding SHALL have stable key `(code, governed_element_identity, resolved_rule)`. An in-process edit MAY keep prior error debt only when its after-error keys are a subset of its before-error keys and it invalidates no current accepted disposition. New error keys SHALL block. Replacements SHALL be new compiled conclusions.


#### Scenario: Legacy debt does not block unrelated safe edit
- **WHEN** an existing grandfathered page lacks typed relations and receives an unrelated edit that preserves its prior semantic state
- **THEN** the write succeeds with migration/review warnings

#### Scenario: Edit cannot introduce malformed observation
- **WHEN** an edit to a grandfathered page introduces malformed observation syntax
- **THEN** the in-process edit is rejected before mutation

#### Scenario: Mechanical worsening is set-based
- **WHEN** a grandfathered edit retains all old error keys but adds one new schema error key
- **THEN** the edit is rejected even if the total error count is unchanged

#### Scenario: Repair may reduce legacy debt incrementally
- **WHEN** a grandfathered edit removes one old error and adds none
- **THEN** the edit may proceed with warnings for the remaining before-existing errors

#### Scenario: Existing-vault activation is portable and nonrewriting
- **WHEN** the capability first opens a non-empty vault
- **THEN** it records existing-page baselines in one governed activation manifest, does not edit the pages, and consistently distinguishes later creations after rebuild/transfer

#### Scenario: Empty vault has no grandfathered page
- **WHEN** activation occurs with no governed compiled pages
- **THEN** the manifest records an empty baseline and the first later page uses the narrow bootstrap disposition

#### Scenario: Replacement follows current contract
- **WHEN** a legacy page is superseded by a newly written compiled conclusion
- **THEN** the successor must satisfy the current semantic and relation contract

## ADDED Requirements

### Requirement: The Activation Manifest Records The Pre-Existing Corpus
On first activation, the system SHALL create one portable vault-managed manifest of the contract version and the identities and source hashes of pre-existing governed compiled pages, without rewriting them. A page with a stable memory ID SHALL match by that ID; a legacy page by path and source hash, or through governed moves in its lineage if it alone left that path. Pages first seen after the boundary, including out-of-band creations, SHALL be new pages with current-contract posthoc findings.

#### Scenario: A legacy page keeps its baseline through governed moves
- **WHEN** a pre-existing page without a stable memory ID is moved twice by governed moves and then edited without worsening
- **THEN** the edit and a posthoc audit treat it as grandfathered
- **AND** after that edit commits, a further edit is judged against the full current contract

#### Scenario: A copy inherits no baseline
- **WHEN** a page created after activation names a legacy page's path in its lineage, and the legacy page still stands at that path or another page also claims it
- **THEN** the edit and a posthoc audit judge the copy as a new page

### Requirement: Only A Real Commit Installs The Activation Manifest
Validate-only and preliminary blockers SHALL NOT create the activation manifest. When the manifest is missing, a real first commit SHALL atomically install or observe the immutable activation-boundary winner after preliminary validation and SHALL then repeat evaluation before any other mutation. If the installed race winner changes membership and repeated evaluation blocks, that immutable manifest SHALL be the sole side effect.

#### Scenario: Validation leaves no manifest
- **WHEN** a validate-only write runs on a vault that has no activation manifest
- **THEN** no activation manifest exists after it

### Requirement: A Relocation Refuses Only The Debt It Adds
A move and its undo SHALL judge each page they reach, the moved page and each page whose links or link standing they change, by the set-based non-worsening rule, whether or not the page is grandfathered. They SHALL refuse only an error key that the move adds, or the invalidation of a satisfied relation disposition. A relation error key holds the link as written and, on a page without a stable memory ID, the page's path; a move that changes either gives a new key, which SHALL still block.

#### Scenario: A legacy page is renamed
- **WHEN** a relocation renames a pre-existing page that has no stable memory ID and no semantic units
- **THEN** the move commits and reports the missing unit as retained debt

#### Scenario: A dependent's old debt does not block
- **WHEN** a page that links the moved page already lacks semantic units and is not grandfathered
- **THEN** the relocation commits, and its undo is judged by the same rule

#### Scenario: A new error key still blocks
- **WHEN** a relocation gives one page an error key that its before state did not have
- **THEN** the relocation refuses that page with `SEMANTIC_CONTRACT_BLOCKED` and relocates the others
