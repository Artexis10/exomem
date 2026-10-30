# Spec Delta

## Purpose

Release reviewed source with reusable test evidence and independently qualified artifacts, while exposing delivery timing and incomplete states.

## ADDED Requirements

### Requirement: Pull requests run the partial tier and Full evidence comes from main

CI SHALL run the partial tier for each pull request, draft or ready, on the supported floor Python (`requires-python`). Full coverage, with both supported Python runtimes and the specialized Full jobs, SHALL run on schedule and manual dispatch (main, or a source branch on request) and SHALL supply release evidence. Docs-only narrowing SHALL apply to every non-release pull request. The release PR SHALL retain targeted metadata and integration checks backed by source evidence. Advisory measurements SHALL run outside the workflow whose completion release evidence requires.

#### Scenario: A pull request becomes ready
- **WHEN** a source PR is opened, updated, or marked ready for review
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

### Requirement: Reuse complete evidence for the actual tested tree

Release acceptance SHALL reuse successful full CI when its actual tested tree equals the selected source tree. The evidence SHALL identify the repository, workflow, run, attempt, tested commit, tree, and full scope. Partial or unavailable evidence SHALL select a full run. Release metadata validation and a short integration check SHALL still run.

#### Scenario: Full source proof survives a squash merge
- **WHEN** a Full run dispatched on the source branch succeeded and its tested tree equals the merged tree
- **THEN** the release reuses that evidence without running the corpus again

#### Scenario: Partial PR checks do not become full proof
- **WHEN** a successful PR run omits part of the full suite
- **THEN** release acceptance does not describe or accept that run as full evidence

#### Scenario: Changed tree needs fresh evidence
- **WHEN** the merged tree differs from the tested tree, or complete proof cannot be established
- **THEN** the release obtains a full run for the selected tree

#### Scenario: A retry retains an earlier successful gate
- **WHEN** the latest successful workflow attempt inherits an earlier successful Full gate and receipt
- **THEN** acceptance retains the receipt's issuing attempt and names the successful retry separately

### Requirement: Failed merge preflight stops the release command

The supported release merge command SHALL stop before invoking merge if current main, the release head, required checks, or selected source evidence fails its preflight. It SHALL pin the reviewed head in the merge request. It SHALL report any remaining base race rather than claiming an atomic base guarantee.

#### Scenario: Current main has moved
- **WHEN** the selected source differs from current main
- **THEN** the release command returns failure and never invokes merge

### Requirement: Qualify Python artifacts independently

Python qualification SHALL bind the exact tag, source commit, publisher run and attempt, and wheel and sdist hashes. It SHALL verify official GitHub and PyPI bytes without waiting for unrelated images. A missing image SHALL affect that image's qualification only. Qualification SHALL grant no installation or rollout authority.

#### Scenario: Images continue after Python publication
- **WHEN** the exact publisher has produced matching official Python distributions while images remain in progress
- **THEN** the Python handback passes and names its producer without claiming image qualification

#### Scenario: A retry inherits successful distribution bytes
- **WHEN** a publisher retry retains an artifact from an earlier successful job
- **THEN** the receipt keeps the original artifact producer attempt and records the later verification attempt separately

### Requirement: Report delivery boundaries separately

A release receipt SHALL record the ready PR/head event, all-green CI, artifact qualification, and consumer verification separately. It SHALL name each timestamp's source. It SHALL report ready-to-green and ready-to-qualified intervals with queue time and retries included. Python qualification SHALL remain distinct from complete image qualification. Missing consumer proof SHALL remain unverified.

#### Scenario: Package exists before consumer verification
- **WHEN** official artifacts qualify but no consumer verification exists
- **THEN** the receipt reports artifact latency and leaves ready-to-production latency unverified
