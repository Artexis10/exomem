# Spec Delta

## Purpose

Release reviewed source with reusable test evidence and independently qualified artifacts, while exposing delivery timing and incomplete states.

## ADDED Requirements

### Requirement: Reuse complete evidence for the actual tested tree

Release acceptance SHALL reuse successful full CI when its actual tested tree equals the selected source tree. The evidence SHALL identify the repository, workflow, run, attempt, tested commit, tree, and full scope. Partial or unavailable evidence SHALL select a full run. Release metadata validation and a short integration check SHALL still run.

#### Scenario: Full source proof survives a squash merge
- **WHEN** a source pull request has successful full CI and its merged tree equals the tested tree
- **THEN** the release reuses that evidence without running the corpus again

#### Scenario: Partial PR checks do not become full proof
- **WHEN** a successful PR run omits part of the full suite
- **THEN** release acceptance does not describe or accept that run as full evidence

#### Scenario: Changed tree needs fresh evidence
- **WHEN** the merged tree differs from the tested tree, or complete proof cannot be established
- **THEN** the release obtains a full run for the selected tree

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

A release receipt SHALL record observed source readiness, artifact qualification, and consumer verification separately. It SHALL name each timestamp's source. It SHALL distinguish Python qualification from complete image qualification. Missing consumer proof SHALL remain unverified and SHALL NOT be replaced with publication time.

#### Scenario: Package exists before consumer verification
- **WHEN** official artifacts qualify but no consumer verification exists
- **THEN** the receipt reports artifact latency and leaves ready-to-production latency unverified
