# cloud-cell-catalogue-prewarm Specification

## Purpose

Build a catalogue beside the serving cell before its upgrade attempt, so a ready catalogue can remove the full parse from the stop.

## ADDED Requirements

### Requirement: A catalogue build runs before the upgrade attempt

The feature SHALL default off. When enabled, `cellctl` SHALL schedule a build Job for each running rollout candidate when its upgrade window opens, subject to capacity and skip rules. The build Job SHALL use the target image and share the serving pod's node through scheduler-enforced placement, mounting the live claim read-write.

Before the final force minutes, `cellctl` SHALL count a running build Job as activity in the due rule, like an in-flight request. The force period SHALL retain its existing default of 10 minutes and override build activity. A build Job waiting for node capacity SHALL NOT defer the attempt. When the build Job ends, `cellctl` SHALL use ordinary request idleness to determine whether the cell is due. Attempts SHALL retain their existing hold clocks and deadlines.

#### Scenario: A candidate gets a build Job during its window

- **WHEN** an eligible candidate's window opens and capacity permits
- **THEN** its build Job runs beside the serving pod before the attempt
- **AND** a successful build leaves a staged copy for the replacement

#### Scenario: An idle cell's build finishes inside its window

- **WHEN** an idle cell's window opens and its build Job runs before the final force minutes
- **THEN** build activity defers the idle attempt while the serving pod keeps serving
- **AND** when the build Job finishes inside the window, ordinary request idleness makes the cell due
- **AND** the replacement adopts the staged copy if boot validation passes

#### Scenario: The build Job still runs when the attempt becomes due

- **WHEN** the force minutes make the attempt due while its build Job still runs
- **THEN** cellctl deletes the build Job and confirms its pods have ended before stopping the cell
- **AND** the attempt records the build outcome and continues under its existing deadlines

#### Scenario: The build Job fails, expires or no longer applies

- **WHEN** the build Job fails, exceeds its bounded timeout, or no longer applies after a pause, target change or closed window
- **THEN** cellctl deletes any unfinished build Job
- **AND** a due attempt rebuilds cold when no staged copy passes boot validation

### Requirement: The build Job self-gates on catalogue identity

The build Job SHALL compute its identity from the target image and vault. It SHALL exit successfully without building when the live identity matches or a valid staged copy already carries its identity. A second build Job SHALL decline while another build Job holds the cell's staged-build lock.

#### Scenario: A release keeps the catalogue identity

- **WHEN** the build Job's computed identity equals the live catalogue's stored identity
- **THEN** the build Job exits successfully without rebuilding or changing the live catalogue

#### Scenario: A repeated build Job finds a complete staged copy

- **WHEN** a valid staged copy already carries the computed identity
- **THEN** the build Job exits successfully without rebuilding

#### Scenario: A build Job already runs

- **WHEN** another build Job holds the cell's staged-build lock
- **THEN** the new build Job declines without changing staged copies or `.building` families
- **AND** an old timestamp does not permit it to remove the active build Job's files

### Requirement: The build Job writes only staged copies, temporary families and coordination locks

The build Job SHALL restrict shared-volume changes to staged copies, `.building` families and their coordination locks. It SHALL publish a complete single-file staged copy named by its identity. It SHALL NOT change the live catalogue, vault content or another store. A killed build Job SHALL leave no torn file that boot can adopt.

#### Scenario: The serving pod keeps serving during the build

- **WHEN** the build Job runs beside a serving pod
- **THEN** the build Job leaves the live catalogue and the serving pod's repair worker undisturbed
- **AND** only the serving pod's ordinary work can change the live catalogue

#### Scenario: The rest of the volume stays unchanged

- **WHEN** the build Job runs and no other process writes during the observation
- **THEN** every shared-volume file outside staged copies, `.building` families and coordination locks remains byte-identical
- **AND** the build Job initializes or migrates no other store

#### Scenario: A build Job dies mid-write

- **WHEN** the build Job dies before it publishes the complete staged copy
- **THEN** no adoptable staged copy from that build Job exists
- **AND** the build Job leaves the live catalogue unchanged

### Requirement: Quota and node capacity bound the build Job

The namespace quota SHALL add exactly one pod and the build profile's requests and limits beside existing headroom. Requests SHALL cover the measured or estimated build peak. Build Jobs SHALL use scheduler capacity checks, without `nodeName` or `priorityClassName`. Until shared concurrency accounting includes build Jobs, cellctl SHALL permit at most one build Job per node.

#### Scenario: The quota admits a build Job beside the serving pod

- **WHEN** the serving pod and ordinary Job headroom consume their allotted quota
- **THEN** the extra slot admits one build Job with its full resource profile

#### Scenario: The node lacks build capacity

- **WHEN** the scheduler cannot fit the build Job's requests on the serving pod's node
- **THEN** the build Job stays pending without bypassing capacity checks or deferring the idle due rule
- **AND** the due attempt cancels the build Job and proceeds cold

#### Scenario: Another cell on the node already has a build Job

- **WHEN** one build Job occupies the node's build slot
- **THEN** cellctl schedules no second build Job there

### Requirement: Tenant-locked and non-running cells skip the build Job

Tenant-locked cells SHALL receive no build Job, even while unlocked. The build Job SHALL NOT join the custody key channel. Non-running cells SHALL receive no build Job. These cells SHALL retain their ordinary cold-rebuild behavior.

#### Scenario: A tenant-locked cell becomes a candidate

- **WHEN** a tenant-locked cell becomes a rollout candidate, including while unlocked
- **THEN** cellctl creates no build Job for it
- **AND** its replacement rebuilds cold when the catalogue identity changes

#### Scenario: A cell is not running

- **WHEN** a rollout candidate has no running serving pod
- **THEN** cellctl creates no build Job for it
- **AND** the cell rebuilds cold on boot when its catalogue requires it

### Requirement: The build Job removes superseded staged copies

At startup, the build Job SHALL remove every completed staged copy except a valid staged copy of its own computed identity. Cleanup SHALL preserve active build Jobs using their locks. Startup cleanup of `.building` families SHALL also check every SQLite family member's age and open connections. File age alone SHALL NOT prove a build Job inactive.

#### Scenario: A rolled-back target leaves a staged copy

- **WHEN** a new build Job starts with a different computed identity
- **THEN** it removes the previous target's completed staged copy
- **AND** it preserves temporary files protected by a live build lock

### Requirement: Backups exclude staged copies and temporary families

Restic SHALL exclude staged copies and `.building` families by their fixed filename patterns, including SQLite sidecars. These exclusions SHALL apply to both hourly and pre-upgrade backups. Backups SHALL retain the live catalogue and unrelated files under the existing backup paths.

#### Scenario: A backup sees a staged copy and an unfinished build

- **WHEN** an hourly or pre-upgrade backup reads a volume containing staged copies and `.building` families
- **THEN** restic excludes both families, including SQLite sidecars
- **AND** the backup retains the live catalogue and unrelated files
