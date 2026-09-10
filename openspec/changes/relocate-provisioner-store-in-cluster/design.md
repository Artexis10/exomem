## Context

Measured on the live alpha, 2026-09-10:

| Observation | Value |
|---|---|
| Endpoint serving both consumers | one, reached on the pooled host by Substrate and the direct host by the provisioner |
| Active time, 1–10 September | 771,672 s of a 849,600 s window, 91% |
| Compute billed in that window | 198,578 CU-seconds, 55.16 CU-hours |
| Average size while active | 0.257 CU, the 0.25 floor |
| Autosuspend | provider default, five minutes |
| Provisioner poll interval | `EXOMEM_PROVISIONER_POLL_SECONDS=1`, two workers |
| `exomem_provisioner` schema | 15 tables, 6,736 kB, 288 rows |
| `public` schema (Substrate) | 60 tables, 6,160 kB, 1,683 rows |
| Node headroom | 4 vCPU, 4.6 GB RAM available, 36 GB disk free |

Two hostnames hid the sharing. A per-service DSN audit that stops at the hostname concludes two independent databases exist; the endpoint id behind the pooled and direct names is the same.

## Goals / Non-Goals

**Goals.** Take the always-on consumer off metered serverless compute. Keep the provisioner's operational store backed up and restore-verified. Make the budget preflight able to name this failure class instead of tolerating it.

**Non-Goals.** Changing reconciler cadence or worker poll intervals; they are correct for what they do and are not the defect. Moving Substrate. Moving tenant vault data, which never lived here. Chasing storage cost, which is under a cent a month.

## Decisions

**In-cluster PostgreSQL, not a second managed provider.** The node is already paid for and already runs stateful workloads on Hetzner CSI encrypted-retain volumes. A second managed provider would re-import the same pricing mismatch at a different vendor.

**Keep the schema boundary that already exists.** `provisioner.databaseSchema` and `provisioner.databaseRole` are already first-class chart values, and the two services are already schema-isolated in one database. The migration is therefore a schema dump and restore, not an application change.

**Backups are in scope.** Relocating state onto the node the control plane manages trades provider replication for local durability. The chart already carries a `databaseBackup` block with a maintenance service and a restore-verification scratch database, and the CronJob exists and is suspended. Cutover requires it running and one proven restore, not a promise of one.

**Cut over with the workers stopped.** The store is 6.7 MB and 288 rows. Scaling the api, worker and volume-worker to zero for the dump and restore is cheaper and safer than any replication scheme, and hosted operations already tolerate a pause: the live cell serves from its own pod and does not consult this database.

## Risks / Trade-offs

- **Node loss now takes the control plane's state with it.** Mitigated by the backup job plus restore verification, and bounded by the fact that the provisioner's state is reconstructible from the cluster and the deployment lock, which is what the 33-stale-cell-record incident already demonstrated in the other direction.
- **The preflight's provider-metadata contract does not apply to an in-cluster store.** The budget spec is written around provider management APIs. The modified capability must state which controls still mean something and which are replaced by cluster-side equivalents, or the preflight will report `unknown` forever and be ignored.
- **Egress allowlisting changes.** `provisioner.databaseEgressCidrs` exists precisely so the database endpoint is explicit; an in-cluster service replaces a public CIDR with a cluster-internal policy, and the NetworkPolicy has to be tightened in the same change rather than left open.
