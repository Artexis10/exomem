# Proposal

## Why

Releases repeat source tests and wait for unrelated image publication before returning qualified Python artifacts. A failed base check can also escape an unchecked merge sequence.

## What Changes

- Keep every pull request, draft or ready, on the partial tier and the supported floor Python; run Full CI only on schedule and manual dispatch, where it supplies release evidence.
- Reuse a Full receipt when its actual tested tree equals the selected release source tree.
- Keep release checks targeted; dispatch full CI when matching proof is absent.
- Run the advisory MCP read latency comparison nightly in its own workflow, so a Full run ends when its gate passes.
- Balance long K3s jobs with measured case durations, without dropping cases.
- Partition paired recall samples and compare each complete series once after checking coverage and provenance.
- Give package acquisition and installation separate bounds without replaying interrupted installation.
- Retain release metadata validation and a short integration check.
- Provide one guarded release merge command that stops after a failed preflight.
- Qualify Python distributions from an explicit publisher run and attempt while image publication continues.
- Keep optional CUDA cache export outside the publication critical path.
- Record ready-to-green CI and ready-to-qualified artifacts separately; retain consumer verification as a separate leg.

## Capabilities

### New Capabilities

- `software-release`: Source evidence, guarded release merge, scoped artifact qualification, and delivery timing.

### Modified Capabilities

- `product-e2e`: Accept full source evidence by tested-tree identity instead of requiring a duplicate full run on main.

## Impact

The change affects CI, paired sampling, K3s shards, package setup, release evidence automation, publication helpers, and the release runbook. Existing optional model and media test rules remain. It adds no service upgrade, image rollout, production model call, or storage migration.
