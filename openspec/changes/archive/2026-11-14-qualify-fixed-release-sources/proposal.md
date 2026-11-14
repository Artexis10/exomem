# Proposal

## Why

A release currently requires its source to remain the main tip while Full CI runs.
Unrelated merges invalidate that selection and encourage repository-wide delivery holds.
Qualify one fixed source while ordinary source merges and PR pushes continue.

## What Changes

- Cut a publisher-owned branch from an explicitly selected, merged main commit.
- Run Release Please against that branch through the existing privileged controller on main.
- Reuse authentic Full evidence for the selected source, or dispatch one matching fallback.
- Keep version generation, release notes, squash merges and tag creation in Release Please.
- Reconcile released metadata onto current main without replacing intervening source or dependencies.
- Preserve the original source boundary for the next changelog through `last-release-sha`.
- Accept isolated tags through a checked per-tag record on trusted main; retain legacy publication support.
- Route both release-PR creation and release creation away from moving main.
- Replace global merge/push holds with serialization of release cuts and their metadata reconciliation.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `software-release`: fixed-source qualification, checked isolated origins, metadata continuity and resumable publication.

## Impact

The change affects `scripts/release_evidence.py`, release workflows, release metadata validation, scoped tests and `docs/release.md`.
CI must recognize isolated release pull requests and apply the existing metadata tier.
The controller must apply main's configured required-check policy even when the isolated branch has no branch protection.
Existing Python, image, recovery and consumer-verification boundaries remain in force.

This proposal changes the release contract and therefore needs durable requirements and an explicit approval before implementation.
It preserves the repository's squash-only merge policy and adds no model or runtime capability.
