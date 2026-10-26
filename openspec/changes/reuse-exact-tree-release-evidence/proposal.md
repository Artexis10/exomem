# Proposal

## Why

Releases repeat source tests and wait for unrelated image publication before returning qualified Python artifacts. A failed base check can also escape an unchecked merge sequence.

## What Changes

- Run historical full CI alongside ordinary checks when a non-release PR is ready.
- Reuse that full receipt when its actual tested tree equals the selected release source tree.
- Keep draft checks partial and release checks targeted; dispatch full CI when matching proof is absent.
- Balance long K3s jobs with measured case durations, without dropping cases.
- Retain release metadata validation and a short integration check.
- Provide one guarded release merge command that stops after a failed preflight.
- Qualify Python distributions from an explicit publisher run and attempt while image publication continues.
- Record ready-to-green CI and ready-to-qualified artifacts separately; retain consumer verification as a separate leg.

## Capabilities

### New Capabilities

- `software-release`: Source evidence, guarded release merge, scoped artifact qualification, and delivery timing.

### Modified Capabilities

- `product-e2e`: Accept full source evidence by tested-tree identity instead of requiring a duplicate full run on main.

## Impact

The change affects CI, K3s test shards, release evidence automation, publication helpers, and the release runbook. Existing optional model and media test rules remain. It adds no service upgrade, image rollout, production model call, or storage migration.
