## MODIFIED Requirements

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

## ADDED Requirements

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
