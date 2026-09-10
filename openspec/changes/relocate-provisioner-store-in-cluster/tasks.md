## 1. Cluster store

- [ ] 1.1 Add a single-replica PostgreSQL workload to `infra/helm/platform`, pinned by digest, with a PersistentVolumeClaim on `exomem-hcloud-encrypted-retain`, resource requests sized against the measured 4.6 GB of free node memory, and no public Service.
- [ ] 1.2 Create the `exomem_provisioner` schema and the `exomem_provisioner_runtime` role from the existing `provisioner.databaseSchema` and `provisioner.databaseRole` values, so the chart remains the single source of both names.
- [ ] 1.3 Replace the public `provisioner.databaseEgressCidrs` allowance with a NetworkPolicy admitting only the api, worker and volume-worker pods to the new Service, and prove a pod outside that set is refused.

## 2. Backup and restore before cutover

- [ ] 2.1 Point the existing `exomem-database-backup` CronJob at the in-cluster store and unsuspend it.
- [ ] 2.2 Prove one full restore into the `exomem_restore_verification` scratch database and assert the restored schema matches the live one table for table and row for row.
- [ ] 2.3 Record retention and the measured restore duration in `docs/runbooks/hosted/database-budget.md`; a backup whose restore time is unknown is not a backup.

## 3. Cutover

- [ ] 3.1 Scale the api, worker and volume-worker to zero and confirm the live cell keeps serving from its own pod throughout.
- [ ] 3.2 Dump the `exomem_provisioner` schema from the serverless endpoint and restore it into the cluster store; compare table count, row counts per table and every sequence's current value.
- [ ] 3.3 Reseal the `exomem-provisioner-database` secret at the in-cluster DSN, scale back up, and confirm all three workloads reach ready.
- [ ] 3.4 Revoke `exomem_provisioner_runtime` on the serverless endpoint and drop the vacated schema only after one clean backup of the cluster store exists.

## 4. Budget preflight

- [ ] 4.1 Extend `infra/scripts/audit_hosted_database_budget.py` so it names each consumer of a metered endpoint as request-driven or always-on, and fails the control when an always-on consumer shares a scale-to-zero endpoint.
- [ ] 4.2 Add the arithmetic to the runbook: the smallest endpoint held open continuously is 182.5 CU-hours a month, so an always-on consumer makes any free allowance and any spend cap below that figure unreachable by construction.
- [ ] 4.3 State in the runbook which provider-metadata controls still apply to an in-cluster store and which cluster-side checks replace the ones that do not.

## 5. Verification

- [ ] 5.1 Re-run the budget preflight against the Neon project and require a pass with Substrate as the only remaining consumer.
- [ ] 5.2 Measure the endpoint's active time over a full week after cutover and record the observed CU-hours against the 182.5 always-on figure.
- [ ] 5.3 Full provisioner suite, `ruff`, and `openspec validate --all --strict --no-interactive`.
