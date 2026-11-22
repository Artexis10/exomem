## MODIFIED Requirements

### Requirement: Pull requests run the partial tier and Full evidence comes from main

CI SHALL run the partial tier for each pull request, draft or ready, on the supported floor Python (`requires-python`). Full coverage, with both supported Python runtimes and the specialized Full jobs, SHALL run on schedule and manual dispatch (main, or a source branch on request) and SHALL supply release evidence. Docs-only narrowing SHALL apply to every non-release pull request. The release PR SHALL retain targeted metadata and integration checks backed by source evidence. A reconciliation PR, which the cut controller opens from the same repository against `main`, SHALL run no test job only while merging it onto its base gives exactly the controller's projection of the accepted release origin for its source; code from the base SHALL check this, and any other content SHALL fail the required gate. Advisory measurements SHALL run outside the workflow whose completion release evidence requires.

#### Scenario: A pull request is opened or updated
- **WHEN** a source PR, draft or ready, is opened, reopened or updated
- **THEN** its partial jobs run on the supported floor Python and no Full-only job runs
- **AND** its run issues no Full receipt

#### Scenario: Full runs only on schedule and dispatch
- **WHEN** CI runs for a schedule or a manual dispatch
- **THEN** the Full tier runs both supported Python runtimes and every specialized Full job against the checked-out commit
- **AND** its receipt claims Full only after every required Full dependency succeeds

#### Scenario: Long jobs keep their cases
- **WHEN** CI divides long-running Full work using measured case durations
- **THEN** every collected case runs exactly once and the receipt retains observed timing, including queue time

#### Scenario: Paired sampling spans runners
- **WHEN** CI partitions paired recall cases across runners
- **THEN** each case retains both arms and every repeat on one runner with its original arm order
- **AND** aggregation verifies complete case coverage and common source, runtime, encoder, and corpus provenance before comparing the whole series once
- **AND** raw fragments carry no statistical verdict and no shard confidence intervals are averaged

#### Scenario: A package download uses its allowance
- **WHEN** package acquisition approaches its bounded allowance during CI setup
- **THEN** local installation retains a separate allowance and does not download packages
- **AND** a failed acquisition prevents installation while a failed installation stops without automatic replay

#### Scenario: Advisory measurements run outside release evidence
- **WHEN** an advisory measurement, such as the MCP read latency comparison, is scheduled or dispatched
- **THEN** it runs in its own workflow, nightly and on request
- **AND** the Full run completes, and its receipt is issued, without waiting for it

#### Scenario: A reconciliation PR carries only the controller's projection
- **WHEN** a reconciliation PR opens against `main`, including a repair that adds no origin or a branch updated from `main`
- **THEN** merging its head onto the base gives the tree the controller projects for that source's accepted origin
- **AND** its test jobs do not run, while metadata, package and integration checks do

#### Scenario: A reconciliation-named PR carries other content
- **WHEN** a PR on a reconciliation branch changes anything the controller's projection does not produce
- **THEN** the release evidence check fails the required gate and the change must land through an ordinary PR
