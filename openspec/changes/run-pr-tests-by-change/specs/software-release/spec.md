## MODIFIED Requirements

### Requirement: Pull requests run the partial tier and Full evidence comes from main

CI SHALL run, for each pull request, draft or ready, the partial-tier jobs other than the core and harness shards, and the test modules that the pull request adds or changes: core modules on the supported floor Python (`requires-python`) and the newest supported runtime, and harness modules on the floor. A pull request SHALL run no core or harness shard. Every push to main SHALL run every core and harness shard on the floor Python. A failure that only the shards on main or the Full tier catch SHALL be fixed forward on main. Full coverage, with both supported Python runtimes and the specialized Full jobs, SHALL run on schedule and manual dispatch (main, or a source branch on request) and SHALL supply release evidence. Docs-only narrowing SHALL apply to every non-release pull request. The release PR SHALL retain targeted metadata and integration checks backed by source evidence. Advisory measurements SHALL run outside the workflow whose completion release evidence requires.

#### Scenario: A pull request is opened or updated
- **WHEN** a source PR, draft or ready, that is not docs-only, is opened, reopened or updated
- **THEN** its partial-tier jobs other than the core and harness shards run, and no Full-only job runs
- **AND** the core test modules that it adds or changes run on the supported floor Python and the newest supported runtime, and its harness test modules run on the floor
- **AND** no core or harness job runs when it changes no test module
- **AND** its run issues no Full receipt

#### Scenario: A push to main runs every partial shard
- **WHEN** a commit is pushed to main
- **THEN** every core and harness shard runs on the supported floor Python
- **AND** a failure that only these shards or the Full tier catch is fixed forward on main

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
