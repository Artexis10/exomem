# Proposal

## Why

The release runbook selects main's tip as the cut source. Main lands through the merge queue on the push tier, so the tip rarely has Full evidence. Each cut then waits for its own Full run, which took 94 and 126 minutes for the last two releases. The cut controller can reuse Full evidence for any merged main commit, but such evidence is rare: Full runs on main only nightly, and each cut's own Full run tests a commit before its reconciliation. Across the 25 releases from v0.180.0 to v0.202.0, a commit since the preceding reconciliation had a successful Full run before the cut started for at most one release. A selector alone therefore saves little; main also needs recent Full evidence.

## What Changes

- Add `release_cuts.py pick-source`. It returns the newest main commit since the last release whose Full evidence the cut can reuse, or main's tip when none exists.
- The release runbook selects the source with `pick-source`.
- The merge queue workflow dispatches Full on main when main has moved and its newest Full run is older than a configurable window (default 4 hours), so a recent tested commit usually exists.
- A release now ends at the newest fully tested commit. Later main commits wait for the next release.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `software-release`: the supported release command selects a fully tested source by default.

## Impact

The change affects `scripts/release_cuts.py`, `scripts/release_evidence.py`, the merge queue workflow, their tests and `docs/release.md`. Full on main runs up to six times a day instead of once; the repository variable `FULL_EVIDENCE_HOURS` sets the window, and `0` turns the dispatch off. The privileged controller, its evidence checks and the release workflows do not change. The time saved per release is unverified until releases run with it. The controller re-checks every source it receives, so the selector grants no authority.
