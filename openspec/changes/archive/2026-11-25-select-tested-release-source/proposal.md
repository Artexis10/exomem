# Proposal

## Why

The release runbook selects main's tip as the cut source. Main lands through the merge queue on the push tier, so the tip rarely has Full evidence. Each cut then waits for its own Full run, which took 94 and 126 minutes for the last two releases. The cut controller already reuses Full evidence for any merged main commit. Only the choice of source makes it wait.

## What Changes

- Add `release_cuts.py pick-source`. It returns the newest main commit since the last release whose Full evidence the cut can reuse, or main's tip when none exists.
- The release runbook selects the source with `pick-source`.
- A release now ends at the newest fully tested commit. Later main commits wait for the next release.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `software-release`: the supported release command selects a fully tested source by default.

## Impact

The change affects `scripts/release_cuts.py`, its tests and `docs/release.md`. The privileged controller, its evidence checks and the release workflows do not change. The controller re-checks every source it receives, so the selector grants no authority.
