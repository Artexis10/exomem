# Proposal

## Why

Public health readers need persisted functional observations rather than pod liveness. Existing platform jobs can produce those observations.
The new synthetic runner crosses a network and database boundary. A durable contract limits it to one provisioned canary.

## What Changes

- Add optional control, email and memory health jobs through the existing chart.
- Keep new jobs disabled by default and exclude them from disabled-job observability.
- Reuse one schedule contract and the existing immutable gateway image.
- Route public GET health requests to the functional reader without changing pod probes.

## Capabilities

### New Capabilities

- `hosted-functional-health-jobs`: Isolated functional observations and their public route.

### Modified Capabilities

- `cloud-platform-operations`: Extend the schedule contract with optional functional observations.

## Impact

The platform chart, schedule data, operational documentation and relevant infrastructure tests change.
Deployment uses the existing owner and approved operational window. Source delivery does not enable the jobs.
