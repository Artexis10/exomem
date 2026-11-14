# software-release Specification

## Purpose
Release reviewed source with reusable test evidence and independently qualified artifacts, while exposing delivery timing and incomplete states.

## Requirements

### Requirement: Pull requests run the partial tier and Full evidence comes from main

CI SHALL run the partial tier for each pull request, draft or ready, on the supported floor Python (`requires-python`). Full coverage, with both supported Python runtimes and the specialized Full jobs, SHALL run on schedule and manual dispatch (main, or a source branch on request) and SHALL supply release evidence. Docs-only narrowing SHALL apply to every non-release pull request. The release PR SHALL retain targeted metadata and integration checks backed by source evidence. Advisory measurements SHALL run outside the workflow whose completion release evidence requires.

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

The supported release command SHALL validate the selected cut source, reviewed head, configured required checks and complete source evidence before merging.
It SHALL pin the reviewed head and verify the actual squash parent and tree before tagging.
A main advancement alone MUST NOT invalidate an isolated cut.
An unexpected cut-branch or head change SHALL stop that cut without blocking unrelated delivery.

#### Scenario: Current main has moved
- **WHEN** an unrelated reviewed source merges after a cut selects its source
- **THEN** qualification continues for the selected source and excludes the later change from that release
- **AND** unrelated main merges and PR pushes remain available

#### Scenario: The cut branch changes unexpectedly
- **WHEN** the cut branch or reviewed release head differs from the selected identity
- **THEN** the command stops before publication and names the changed identity

#### Scenario: The squash outcome differs
- **WHEN** the merge parent or tree differs from the selected source and checked release tree
- **THEN** automation creates no release tag for that outcome

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

### Requirement: An intended release lands when its checks pass

Release automation SHALL act only on an explicit intent naming the cut, source and reviewed head.
It SHALL apply main's configured required-check policy to that head and require complete source evidence and checked metadata.
It SHALL merge into the selected isolated branch and create a tag for the verified squash outcome.
Main pushes SHALL neither create competing release proposals nor publish the moving main tree.
The existing landing switch SHALL stop automatic landing.

#### Scenario: A nightly Full run passes while a release PR is open
- **WHEN** a scheduled Full run completes without an intent for a selected cut and head
- **THEN** automation may retain its evidence but merges and tags no release

#### Scenario: The release head changes after the intent
- **WHEN** an intent names an earlier release head
- **THEN** automation does not merge or tag the newer head

#### Scenario: Required checks are not all reported yet
- **WHEN** the head's CI is incomplete or a configured required check is missing
- **THEN** automation keeps the cut pending and does not treat a partial or empty list as success

#### Scenario: A required check fails
- **WHEN** a configured required check fails on the selected head
- **THEN** automation stops landing and names the failed check

#### Scenario: The intent named this head and every check passes
- **WHEN** complete evidence and checked metadata support the intended source and head
- **THEN** automation merges that head into its isolated branch and tags only the verified outcome

#### Scenario: Landing is switched off
- **WHEN** the landing switch is off as automation starts
- **THEN** automation merges and tags no new cut

### Requirement: Preserve release history across advancing main

Metadata reconciliation SHALL preserve intervening product changes and dependency selections on main.
It SHALL record the released source boundary rather than the later reconciliation boundary for the next release.
A subsequent cut SHALL contain the preceding reconciliation and matching published version state.
Only cuts and their reconciliation SHALL serialize; unrelated delivery MUST remain available.

#### Scenario: A change arrives during qualification
- **WHEN** a feature and dependency change merge after source selection but before metadata reconciliation
- **THEN** the current release excludes them and reconciliation preserves them on main
- **AND** the following release includes their behavior and changelog entry exactly once

#### Scenario: An older source is selected after reconciliation
- **WHEN** the selected source lacks the preceding reconciliation or has stale published version state
- **THEN** automation rejects that selection without starting a new Full run or changing an existing tag

#### Scenario: Generator inputs change before reconciliation merges
- **WHEN** current main changes an input to generated packages
- **THEN** reconciliation refreshes its generated outputs while retaining those source changes
- **AND** it does not restart qualification for the already selected source

### Requirement: Retain checked origins for isolated release tags

An isolated tag SHALL bind to a write-once record accepted on trusted main before artifact publication.
The record SHALL identify the selected source, reviewed tree, release outcome and complete evidence producer.
Publication SHALL verify that binding and source ancestry rather than require the release commit itself in main ancestry.
Legacy releases SHALL retain their existing origin path.

#### Scenario: The release commit is outside main
- **WHEN** a tag matches an accepted cut record and its source, parent, tree and evidence checks pass
- **THEN** its artifacts may qualify while current main contains later source changes

#### Scenario: A declared metadata path carries other content
- **WHEN** release metadata changes an undeclared JSON value or generated content differs from the deterministic output
- **THEN** automation rejects that candidate before tagging despite its allowed file path

#### Scenario: A record or immutable tag conflicts
- **WHEN** an existing record or tag identifies different content
- **THEN** automation reports the conflict and rewrites neither identity

### Requirement: Resume a cut without changing its source

A retry SHALL recover the same cut, source, version and release outcome from retained GitHub identities.
It SHALL reuse authentic evidence or an existing matching Full run before dispatching a fallback.
Recovery SHALL preserve published distributions, original image evidence and checked origin records.
Temporarily unavailable state SHALL remain visibly pending during bounded observation.

#### Scenario: Execution stops after tagging
- **WHEN** automation resumes after creating a tag but before completing reconciliation or publication
- **THEN** it resumes that version and source without selecting newer main, retagging or duplicating Full

#### Scenario: Temporary Actions evidence expires
- **WHEN** an accepted release needs artifact recovery after its original temporary evidence expires
- **THEN** recovery retains the accepted main record and verifies the immutable source and original artifact evidence
