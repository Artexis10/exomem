## Why

The provisioner's operational store shares one serverless PostgreSQL endpoint with Substrate, and `exomem-provisioner-worker` and `exomem-volume-worker` poll it once per second by construction. A fixed-interval poller and a scale-to-zero database are incompatible: any interval shorter than the provider's autosuspend window holds the endpoint at full duty, and any interval longer than it defeats the worker. Measured 2026-09-10, the endpoint was active 214 of the 236 hours since the billing period opened, 91% of the clock at its 0.25-CU floor, for a fleet of one cell.

That floor is a fixed number. The smallest endpoint held open continuously is 182.5 CU-hours per month, against a 100-CU-hour free allowance, so the free tier is unreachable by construction rather than by usage, and a spending cap on a workload whose floor already exceeds the budget is a kill switch rather than a budget. Neither a plan change, a poll-interval change, nor a compute quota addresses it. Placement does.

## What Changes

- Give the provisioner its own PostgreSQL inside the existing k3s cluster, on the Hetzner CSI encrypted-retain storage class the platform already uses, and repoint `EXOMEM_PROVISIONER_DATABASE_URL` at it.
- Migrate the `exomem_provisioner` schema, which is already isolated from Substrate's `public` schema in the shared database: 15 tables, 6.7 MB, 288 rows at the time of writing.
- Keep Substrate on the serverless endpoint, where its request-driven traffic and single reconcile cron actually match how that product is priced.
- Bring the existing suspended database-backup CronJob into service against the in-cluster store, so relocating operational state does not relocate it out of a backup path.
- Extend the budget preflight so it reports the retained compute baseline's *cause*, not only its existence, and so an always-on consumer sharing a scale-to-zero endpoint is a named failed control rather than an unexplained baseline.

## Capabilities

### Modified Capabilities

- `hosted-database-budget`: Placement is a budget control. The preflight distinguishes a request-driven consumer from an always-on one and refuses to call a shared endpoint's baseline acceptable when a poller holds it open.

## Impact

Touches the platform chart (a new in-cluster PostgreSQL workload, its volume, its service and its NetworkPolicy), the provisioner database secret and its egress CIDR preflight, the durability backup and restore-verification jobs, `infra/scripts/audit_hosted_database_budget.py`, and `docs/runbooks/hosted/database-budget.md`.

Durability changes shape and the change must answer for it: the provisioner's state moves from a replicated managed service onto the single node it manages, so a node loss would take both. The backup path is therefore in scope, not a follow-up. Substrate's data, the Neon project, the deployment lock pair and every runtime contract are untouched.

This change does not alter poll intervals, reconciler cadence, tenant data placement, or the hosted release pipeline.
