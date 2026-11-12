# cloud-cell-catalogue-prewarm Specification

## Purpose

Build a catalogue beside the serving cell before its upgrade attempt, so a ready catalogue can remove the full parse from the stop.

## ADDED Requirements

### Requirement: A catalogue build runs before the upgrade attempt

The feature SHALL default off. When enabled, `cellctl` SHALL schedule a target-image build for each running rollout candidate when its upgrade window opens, subject to capacity and skip rules. The Job SHALL share the serving pod's node through scheduler-enforced placement and mount the live claim read-write. Attempts SHALL retain their existing due rules and deadlines, without waiting for a build result.

#### Scenario: A candidate gets a builder during its window

- **WHEN** an eligible candidate's window opens before its attempt becomes due and capacity permits
- **THEN** its build Job runs beside the serving pod before the attempt
- **AND** a successful build leaves a staged catalogue for the replacement

#### Scenario: The build is unfinished when the attempt becomes due

- **WHEN** the attempt becomes due before cellctl observes build success
- **THEN** cellctl deletes the Job and confirms its pods have ended before stopping the cell
- **AND** the replacement does not adopt that build, including a file published during cancellation
- **AND** the attempt records the build outcome and continues under its existing deadlines

#### Scenario: The build fails, expires or loses eligibility

- **WHEN** the build fails, exceeds its bounded timeout, or loses eligibility through a pause, target change or closed window
- **THEN** cellctl deletes any unfinished build Job
- **AND** a due attempt proceeds without a warm catalogue when none is eligible

### Requirement: The build self-gates on catalogue identity

The build SHALL compute its identity from the target image and vault. It SHALL exit successfully without building when the live identity matches or a valid staged copy already carries its identity. It SHALL decline a second build while another holds the cell's staged-build lock.

#### Scenario: A release keeps the catalogue identity

- **WHEN** the build's computed identity equals the live catalogue's stored identity
- **THEN** the Job exits successfully without rebuilding or changing the live catalogue

#### Scenario: A repeated build finds a complete staged copy

- **WHEN** a valid staged copy already carries the computed identity
- **THEN** the Job exits successfully without rebuilding

#### Scenario: A build is already in progress

- **WHEN** another builder holds the cell's staged-build lock
- **THEN** the new builder declines without changing staged files
- **AND** an old timestamp does not permit it to remove the active builder's files

### Requirement: The build writes only staged files and coordination locks

The Job SHALL restrict shared-volume changes to staged catalogue families and their coordination locks. It SHALL publish a complete single-file staged copy named by its identity. It SHALL NOT change the live catalogue, vault content or another store. A killed build SHALL leave no torn file that boot can adopt.

#### Scenario: The serving pod keeps serving during the build

- **WHEN** the Job runs beside a serving pod
- **THEN** the Job leaves the live catalogue and the serving pod's repair worker undisturbed
- **AND** only the serving pod's ordinary work can change the live catalogue

#### Scenario: The rest of the volume stays unchanged

- **WHEN** the target-image Job runs and no other process writes during the observation
- **THEN** every shared-volume file outside staged families and coordination locks remains byte-identical
- **AND** the Job initializes or migrates no other store

#### Scenario: A build is killed mid-write

- **WHEN** the build dies before it publishes the complete staged copy
- **THEN** no adoptable copy from that build exists
- **AND** the live catalogue remains untouched by the build

### Requirement: Quota and node capacity bound the build

The namespace quota SHALL add exactly one pod and the build profile's requests and limits beside existing headroom. Requests SHALL cover the measured or estimated build peak. Builds SHALL use scheduler capacity checks, without `nodeName` or `priorityClassName`. Until shared concurrency accounting includes builds, cellctl SHALL permit at most one build per node.

#### Scenario: The quota admits a builder beside the serving pod

- **WHEN** the serving pod and ordinary Job headroom consume their allotted quota
- **THEN** the extra slot admits one builder with its full resource profile

#### Scenario: The node lacks build capacity

- **WHEN** the scheduler cannot fit the build's requests on the serving pod's node
- **THEN** the Job stays pending without bypassing capacity checks
- **AND** the due attempt cancels it and proceeds cold

#### Scenario: Another cell on the node already has a builder

- **WHEN** one build occupies the node's build slot
- **THEN** cellctl schedules no second build there

### Requirement: Tenant-locked and non-running cells skip the build

Tenant-locked cells SHALL receive no catalogue-build Job, even while unlocked. The build SHALL NOT join the custody key channel. Non-running cells SHALL receive no build Job. These cells SHALL retain their ordinary cold-rebuild behavior.

#### Scenario: A tenant-locked cell becomes a candidate

- **WHEN** a tenant-locked cell becomes a rollout candidate, including while unlocked
- **THEN** cellctl creates no build Job for it
- **AND** its replacement rebuilds cold when the catalogue identity changes

#### Scenario: A cell is not running

- **WHEN** a rollout candidate has no running serving pod
- **THEN** cellctl creates no build Job for it
- **AND** the cell rebuilds cold on boot when its catalogue requires it

### Requirement: The builder removes superseded staged copies

At startup, the build SHALL remove every completed staged copy except a valid copy of its own computed identity. Cleanup SHALL preserve active builds using their locks. Temporary-file cleanup SHALL also check every SQLite family member's age and open connections. File age alone SHALL NOT prove a build inactive.

#### Scenario: A rolled-back target leaves a staged copy

- **WHEN** a new builder starts with a different computed identity
- **THEN** it removes the previous target's completed staged copy
- **AND** it preserves temporary files protected by a live build lock
