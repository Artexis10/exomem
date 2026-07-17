# Delta: adoption-studio × semantic units

## ADDED Requirements

### Requirement: Work items include semantic-unit context packs

`adoption_studio(action="work-item")` SHALL include, for each bound source it returns, the source's semantic unit context pack (the same pack surface `context-packs` defines), bounded by the work item's existing caps. Pack inclusion SHALL be read-only and deterministic; when a source has no units, the row SHALL say so explicitly rather than omitting the field.

#### Scenario: Bound sources carry their unit packs

- **WHEN** `work-item` is called on a run with applied sources that have semantic units
- **THEN** each source row includes a `semantic_units` pack (units, kinds, bounded excerpts) alongside the raw excerpt
- **AND** the response stays within the work item's server-clamped size bounds

#### Scenario: Sources without units are explicit

- **WHEN** a bound source has no semantic units
- **THEN** the source row carries an explicit empty pack marker, never a silently missing field

### Requirement: Proposals are contract-validated at submission

`adoption_studio(action="propose")` SHALL run the semantic write contract's creation validation for `compilation` and `supersession` payloads at submission time and record the outcome on the proposal as `contract_findings`. Blocking findings SHALL NOT invalidate the proposal by themselves (relation-review findings are resolvable at apply via the reviewed-none flow); non-review blockers SHALL mark the proposal `invalid` with the findings attached.

#### Scenario: Reviewable findings ride the proposal record

- **WHEN** an agent submits a compilation whose content passes validation except a missing relation disposition
- **THEN** the proposal is recorded `proposed` with `contract_findings` naming the relation-review requirement
- **AND** the review queue item carries those findings

#### Scenario: Non-review blockers invalidate at submission

- **WHEN** a submitted compilation has a blocking contract finding that no reviewed disposition can clear
- **THEN** the proposal is recorded `invalid` with the findings attached and can never be applied

### Requirement: Reviewed-none apply flow is the governed path

`adoption_studio(action="apply-proposal")` for compilation, supersession, and reconciliation-replace SHALL commit through the two-phase governed creation: validate to bind a draft, then commit with `relation_disposition="reviewed_none"`, `relation_review_hash` bound to the validated draft, and the approver's `why` as the relation review reason — whenever the contract requires an explicit relation review. Pages committed this way SHALL resurface through the relation-debt queue.

#### Scenario: Approval rationale rides the governed write

- **WHEN** a reviewer approves a compilation proposal whose content carries no typed relation
- **THEN** the created page records a reviewed-none disposition whose review reason is the approver's `why`
- **AND** the page appears in the relation-debt queue on its normal schedule

### Requirement: Studio surfaces contract findings before approval

The Studio proposal detail SHALL render the proposal's `contract_findings` and, when approval would record a reviewed-none disposition, state that consequence in plain language before the approve control. The Studio SHALL NOT enable approval while the proposal is `invalid`.

#### Scenario: Reviewer sees findings and the reviewed-none consequence

- **WHEN** a reviewer opens a proposal whose findings require a relation review
- **THEN** the detail shows the findings and states that approving records "reviewed, no typed relation yet" and the page will resurface for relation review
- **AND** the approve control is enabled

#### Scenario: Invalid proposals cannot be approved from the Studio

- **WHEN** a reviewer opens an `invalid` proposal
- **THEN** the findings are shown and the approve control is absent or disabled
