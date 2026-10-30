# Spec Delta

## MODIFIED Requirements

### Requirement: Tiered model and media gates
Lean product E2E SHALL run without optional models for every pull request that changes a path outside the docs-only set, except the release-please release pull request, and for every main, scheduled, or manual run. The docs-only set is `docs/`, `openspec/`, and Markdown files at the repository root other than `README.md`; none of these paths is a wheel or sdist input.
CI SHALL omit this journey for a pull request only when its complete merge-tree comparison proves that every change is inside the docs-only set, or when the pull request is the release-please release pull request whose release evidence requires successful full evidence for its source tree and refuses any change that release-please did not make.
CI SHALL report omission explicitly, as a job summary or a skipped job, and SHALL retain the journey when that comparison or decision is unavailable.
Real embeddings/reranking SHALL run in the model job.
Real OCR, PDF, ASR, CLIP, and video fixtures SHALL run scheduled or opt-in with explicit soft-fail reporting when their configured dependencies are unavailable.

#### Scenario: Lean CI remains deterministic
- **WHEN** optional model and media extras are absent in a pull-request job that runs the journey
- **THEN** the lean E2E proves the complete text/governance lifecycle and reports optional lanes as unavailable rather than failing implicitly

#### Scenario: OpenSpec-only changes report omitted execution
- **WHEN** every change relative to the pull request merge parent is inside `openspec/`
- **THEN** CI reports that the installed journey did not run and makes no runtime acceptance claim
- **AND** the remaining CI checks still run under their existing rules

#### Scenario: Docs-only changes report omitted execution
- **WHEN** every change relative to the pull request merge parent is inside the docs-only set
- **THEN** CI reports that the installed journey did not run and makes no runtime acceptance claim
- **AND** every test marked `reads_repository_docs` still runs, the push run on main runs every test after the merge, and the remaining CI checks still run under their existing rules

#### Scenario: Mixed changes retain installed proof
- **WHEN** a pull request changes any path outside the docs-only set, including `README.md`, a deletion, or either side of a rename
- **THEN** CI runs the installed journey even when the final source commit changes only docs-only paths

#### Scenario: Unavailable comparison retains installed proof
- **WHEN** CI cannot read the merge ancestry or does not obtain a scope decision
- **THEN** it does not authorize omission of the installed journey

#### Scenario: Release pull request relies on full-run evidence
- **WHEN** the pull request is the release-please release pull request
- **THEN** the installed journey does not run on it
- **AND** release evidence refuses the pull request unless successful full evidence exists for its source tree and every change is release-please's own work

#### Scenario: Main and full runs retain installed proof
- **WHEN** CI runs for main, a schedule, or a manual dispatch
- **THEN** it runs the installed journey regardless of changed paths
- **AND** release acceptance requires successful full evidence for the actual source tree, reusable across identical tested and merged trees
