# Verify Cloud backup repositories

## Why

A successful backup confirms that restic wrote a snapshot. Later pack corruption can make that snapshot unreadable without changing its backup receipt.

## What Changes

- Let the Cloud controller check a rotating subset of each enabled repository.
- Record the exact slice and outcome separately from backup freshness.
- Give backup, prune, restore, and maintenance priority over verification.
- Ship with the verification allowlist empty. Rehearse with synthetic data before requesting live activation.

## Capabilities

### New Capabilities

- `cloud-backup-verification` — Bounded verification of retained Cloud backup packs and reporting of each observed result.

### Modified Capabilities

## Impact

The Cloud controller, Helm configuration, and existing backup repository credentials support a separate Job. The Job uses the restic binary already in the cell image.
