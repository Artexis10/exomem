## Why

The hosted control plane keeps its serverless database active even with no user traffic. A tiny data set therefore does not imply Free-tier compute usage, and an unconstrained autoscaling maximum permits avoidable spend spikes.

## What Changes

- Define explicit, separately verified endpoint capacity, monthly spending-alert and optional monthly compute-quota controls.
- Add a read-only provider audit and operational runbook; use provider metadata rather than SQL so the audit cannot itself wake compute.
- Report unknown provider state as a failed audit, never as a passing budget check. Never automatically delete branches or change production settings.
- Preserve existing lifecycle, authorization renewal, magic-link delivery and backup schedules. True idle suspension needs a separately approved cross-service scheduler design; this change does not claim to remove the measured always-awake baseline.

## Capabilities

### New Capabilities

- `hosted-database-budget`: Verifiable database cost guardrails and explicit whole-control-plane cutoff semantics.

### Modified Capabilities

None.

## Impact

Adds an infrastructure audit script, focused tests and a hosted operations runbook. Reads Neon API v2 through the authenticated Neon CLI. No application schema, runtime, workload schedule, credential, DNS or database migration; no model or paid preview creation.
