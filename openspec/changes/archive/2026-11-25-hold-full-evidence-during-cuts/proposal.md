# Proposal

## Why

The Full-evidence dispatch counted a cut's own Full run in its age window. That run tests a source before the cut's reconciliation, and `pick-source` accepts only sources that contain the preceding reconciliation. So the run could not serve the next release, yet it held off a usable Full run on main for a whole window. A Full run on main during an open cut has the same problem.

## What Changes

- The dispatch waits while a cut is open (its controller run is in progress).
- The age window counts only Full runs on main. A cut's run still counts as in flight and as evidence for the commit it tested.
- The landing of a cut's reconciliation therefore usually starts the next Full run on main.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `software-release`: "Main keeps recent Full evidence for release cuts".

## Impact

`scripts/release_evidence.py`, its tests and `docs/release.md`. No workflow or controller change.
