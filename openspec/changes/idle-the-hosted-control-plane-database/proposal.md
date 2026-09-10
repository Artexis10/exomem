## Why

The hosted control plane keeps its serverless PostgreSQL endpoint awake continuously, so it is billed as if it were a provisioned server that never idles. Measured 2026-09-10, the endpoint was active 771,672 of the 849,600 seconds since the billing period opened, 91% of the clock at its 0.25-CU floor, for a fleet of one cell. That is 166 CU-hours a month against a 100-CU-hour free allowance.

Nothing about the workload requires this. Five components contact the database on a fixed heartbeat, and every one of them lands inside the endpoint's five-minute autosuspend window:

| Component | Cadence |
|---|---|
| `exomem-provisioner-worker` | 1 second |
| `exomem-volume-worker` | 1 second |
| `exomem-deletion-dispatcher` | 1 minute |
| `exomem-capacity-receipt-collector` | 1 minute |
| `exomem-hosted-scheduler-exomem-reconcile` | 1 minute |

Autosuspend savings are gated by the most frequent consumer, so this is all-or-nothing: widening any four of these while the fifth still lands inside the window changes the bill by zero.

An earlier draft of this change proposed relocating the provisioner's store into the cluster instead. That is rejected. It would put the control plane's state on a node the control plane manages, so losing that node would lose the fleet and the record of the fleet together, and recovery would need the database restored before anything could be provisioned. It also does not survive a roadmap that auto-provisions nodes, where fleet nodes are cattle and control-plane state must not be one of them.

## What Changes

- Give both routine workers an idle backoff. They currently sleep a fixed second whenever there is no work; instead they escalate towards a configured idle interval after consecutive empty passes and reset to the floor the moment real work appears.
- Widen the three heartbeat schedules past the autosuspend window, and move the scheduler contract's missed-run alert threshold with them so a slower cadence does not read as a missed run.
- Lower the endpoint's autosuspend from the provider default of five minutes to sixty seconds, so an idle gap converts to a suspend quickly rather than after a wait as long as the new cadence.
- Extend the budget preflight so an always-on consumer sharing a metered scale-to-zero endpoint is a named failed control, resolved by endpoint identity rather than hostname.

## Capabilities

### Modified Capabilities

- `hosted-database-budget`: Placement and cadence are budget controls. The preflight distinguishes a request-driven consumer from an always-on one, and refuses to call a shared endpoint's baseline acceptable when a heartbeat holds it open.

## Impact

Touches the provisioner's two worker entrypoints and their settings models, the three schedule contracts vendored into `infra/helm/platform/files` with their pinned digests, `infra/scripts/audit_hosted_database_budget.py`, and `docs/runbooks/hosted/database-budget.md`.

Operation pickup latency rises from about a second to at most the idle interval. That is proportionate rather than disproportionate, because Substrate's own reconciler is the thing submitting most of this work and it is moving to the same cadence. Nothing in the lifecycle contract promises sub-minute pickup, and a cell provision already takes minutes.

This change does not relocate any database, alter tenant data placement, change the deployment lock or any runtime contract, or touch the governance migration.
