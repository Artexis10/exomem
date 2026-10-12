# Spec Delta

## MODIFIED Requirements

### Requirement: Tiered model and media gates
Lean product E2E SHALL run without optional models for every pull request with changes outside `openspec/` and every main, scheduled, or manual run.
CI SHALL omit this journey only for a pull request whose complete merge-tree comparison proves that every change is inside `openspec/`.
CI SHALL report omission explicitly and SHALL retain the journey when that comparison or decision is unavailable.
Real embeddings/reranking SHALL run in the model job.
Real OCR, PDF, ASR, CLIP, and video fixtures SHALL run scheduled or opt-in with explicit soft-fail reporting when their configured dependencies are unavailable.

#### Scenario: Lean CI remains deterministic
- **WHEN** optional model and media extras are absent in a pull-request job that runs the journey
- **THEN** the lean E2E proves the complete text/governance lifecycle and reports optional lanes as unavailable rather than failing implicitly

#### Scenario: OpenSpec-only changes report omitted execution
- **WHEN** every change relative to the pull request merge parent is inside `openspec/`
- **THEN** CI reports that the installed journey did not run and makes no runtime acceptance claim
- **AND** the remaining CI checks still run under their existing rules

#### Scenario: Mixed changes retain installed proof
- **WHEN** a pull request changes any path outside `openspec/`, including a deletion or either side of a rename
- **THEN** CI runs the installed journey even when the final source commit changes only OpenSpec

#### Scenario: Unavailable comparison retains installed proof
- **WHEN** CI cannot read the merge ancestry or does not obtain a scope decision
- **THEN** it does not authorize omission of the installed journey

#### Scenario: Main and full runs retain installed proof
- **WHEN** CI runs for main, a schedule, or a manual dispatch
- **THEN** it runs the installed journey regardless of changed paths
- **AND** release acceptance still requires green full CI at the recorded main revision
