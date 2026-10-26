# Proposal

## Why

Releases repeat source tests and wait for unrelated image publication before returning qualified Python artifacts. A failed base check can also escape an unchecked merge sequence.

## What Changes

- Reuse successful full CI when its actual tested tree equals the selected release source tree.
- Keep partial PR checks distinct from full CI; dispatch full CI when complete matching proof is absent.
- Retain release metadata validation and a short integration check.
- Provide one guarded release merge command that stops after a failed preflight.
- Qualify Python distributions from an explicit publisher run and attempt while image publication continues.
- Record source readiness, artifact qualification, and consumer verification as separate timestamps.

## Capabilities

### New Capabilities

- `software-release`: Source evidence, guarded release merge, scoped artifact qualification, and delivery timing.

### Modified Capabilities

- `product-e2e`: Accept full source evidence by tested-tree identity instead of requiring a duplicate full run on main.

## Impact

The change affects CI, release evidence automation, publication helpers, and the release runbook. It adds no service upgrade, image rollout, model call, or storage migration.
