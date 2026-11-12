# cloud-cell-catalogue-prewarm Specification

## Purpose

Build a cell's retrieval catalogue under the target release's catalogue identity before the cell stops for its upgrade, so a catalogue-identity release no longer adds its whole rebuild to the stop.

## ADDED Requirements

### Requirement: A catalogue build runs before the cell stops on every upgrade attempt

`cellctl` SHALL schedule a catalogue-build Job for a cell that is due an upgrade attempt, before the attempt stops the serving pod, while the serving pod still holds its volume. The Job SHALL run the target image on the node that holds the cell's volume and SHALL mount the cell's volume read-write. The attempt SHALL wait for the Job to reach a terminal state, bounded by a build timeout, and SHALL proceed on success, failure or timeout alike: a missing staged sibling only means the new pod rebuilds cold.

#### Scenario: A due cell gets a builder before its stop

- **WHEN** a cell's upgrade attempt is due on a release whose catalogue identity differs from the live sidecar's
- **THEN** the build Job runs to completion before the serving pod is stopped
- **AND** a staged sibling for the target identity exists on the cell's volume when the attempt continues

#### Scenario: The build fails or times out

- **WHEN** the build Job fails, or does not reach a terminal state inside the build timeout
- **THEN** the upgrade attempt proceeds to stop the serving pod
- **AND** the Job's outcome is recorded on the attempt

### Requirement: The build self-gates on catalogue identity

The build SHALL compute the catalogue identity its own image produces and compare it to the live sidecar's stored identity. It SHALL build only when they differ, and SHALL exit successfully without building when they match, or when a staged sibling already carries its identity. It SHALL also decline to start a second build for a cell that already has one in flight.

#### Scenario: A release that keeps the catalogue identity

- **WHEN** the scheduled build's computed identity equals the live sidecar's stored identity
- **THEN** the Job exits successfully without touching the volume's catalogue files

#### Scenario: A repeated build for the same target

- **WHEN** a staged sibling already carries the identity the build computes
- **THEN** the Job exits successfully without rebuilding

### Requirement: The build publishes a detached sibling, never the live sidecar

The build SHALL write its catalogue to a staged sibling file that is named by the target's catalogue identity, beside the live sidecar on the cell's volume. It SHALL NOT rename, replace, truncate or write the live sidecar, and SHALL leave the serving pod's catalogue reads and its repair worker undisturbed. A build that dies SHALL leave a staged sibling either absent or whole — never a torn file the new pod could adopt.

#### Scenario: The serving pod keeps serving during the build

- **WHEN** the build Job runs beside a serving pod
- **THEN** the live sidecar's path, contents and stored identity are unchanged for the build's whole run
- **AND** the serving pod's repair worker publishes and reconciles exactly as if no build ran

#### Scenario: A build is killed mid-write

- **WHEN** the build Job is terminated before it finishes
- **THEN** no adoptable staged sibling exists afterwards
- **AND** the live sidecar is untouched

### Requirement: The namespace quota admits exactly one builder beside the serving pod

The cell namespace's `ResourceQuota` SHALL carry one warm-build slot: one extra pod plus the requests and limits of the build's bounded resource profile. The slot SHALL be sized so the Job is admitted beside the serving pod and the namespace's ordinary Job headroom, and no larger.

#### Scenario: The builder is admitted during the window

- **WHEN** the build Job is created while the serving pod and the namespace's ordinary headroom are committed
- **THEN** the quota admits it

### Requirement: Cells the platform cannot read skip the build

A tenant-locked cell SHALL get no catalogue-build Job: the build would need the cell's content key, and the custody contract hands that key only to the replacement instance. A cell that is not running SHALL get no build Job. Such cells rebuild on boot as they do today.

#### Scenario: A tenant-locked cell is due

- **WHEN** a tenant-locked cell's upgrade attempt is due on a catalogue-identity release
- **THEN** no catalogue-build Job is created for it
- **AND** its stop carries the rebuild

### Requirement: Staged siblings are swept

`cellctl` SHALL remove staged siblings whose identity matches neither the cell's current live sidecar nor any pending target, keeping at most the siblings a current or pending target still wants. A cell's boot MAY sweep the same set. Sweep SHALL NOT remove a sibling while a build in flight could still be writing it.

#### Scenario: A rolled-back target leaves a sibling

- **WHEN** a staged sibling's identity belongs to a release that is no longer the target and was never adopted
- **THEN** the sibling is removed on the next reconcile pass
