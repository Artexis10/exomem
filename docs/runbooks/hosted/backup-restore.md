# Backup and restore

## Preconditions

Use the newest remotely verified encrypted object, not job-start time. Backup
age above 45 minutes warns; 60 minutes blocks invitations. A restore always uses
a new stopped candidate identity and a bounded scratch volume.

The platform renders four separate durability paths from the pinned workload
contract. `exomem-durability-backup` runs every 30 minutes with Kubernetes route
coordination, bounded 6 GiB scratch, the recovery upload-only B2 key, and the
provider identity signer. `exomem-database-backup` runs every 30 minutes without
a Kubernetes token, uses mode-`0600` PGSERVICE/PGPASS copies and the independent
database-backup upload key, and proves a clean scratch restore for the configured
opaque owner tenant/cell. Every plaintext portable delivery records its exact B2
version in the encrypted durability ledger before a URL is returned;
`exomem-export-gc` deletes expired recorded versions every five minutes without a
Kubernetes token or whole-bucket scan. The live bucket names and B2
origin are read from ConfigMap `exomem-durability-storage`; credentials are
individual Secret refs and never appear in that ConfigMap.

```bash
python3 infra/scripts/external_blackbox.py --contract infra/contracts/observability-v1.json
```

The GitHub Actions black-box schedule stays disabled until the hosted deployment
exists. Before the first production invite, configure the repository secrets
`EXOMEM_BLACKBOX_CONTROL_URL`, `EXOMEM_BLACKBOX_BACKUP_FRESHNESS_URL`, and
`EXOMEM_BLACKBOX_SCHEDULER_URL`; run the workflow manually and require three
healthy observations; then restore the `*/5 * * * *` schedule in
`.github/workflows/hosted-infrastructure.yml`. The five-minute cadence must stay
aligned with `poll_interval_seconds: 300` in the observability contract.

Start backup or restore through the product lifecycle endpoint with the original
idempotency key. Never archive a live filesystem or copy a source binding marker.
The restore request is the control plane's complete mode-`0600` `RestoreRequest`;
it binds the candidate, source cell, provider reference, archive and manifest
SHA-256 values, archive size, release, protocol, fence, and worker policy.

```bash
: "${IDEMPOTENCY_KEY:?use the original restore-operation key}"
bearer_file=/secure/operator/provisioner-api.bearer
restore_request=/secure/operator/restore-request.json
test "$(stat -c %a "$bearer_file")" = 600
test "$(stat -c %a "$restore_request")" = 600
kubectl -n exomem-platform port-forward service/exomem-provisioner 18080:8080
```

With the port-forward running, repeat this exact call while the response is
`202`; a final restore is `204`:

```bash
curl --fail-with-body --silent --show-error --max-redirs 0 --max-time 30 \
  -X POST http://127.0.0.1:18080/cells/restore \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "$(<"$bearer_file")") \
  -H 'Content-Type: application/json' \
  -H 'X-Exomem-Provisioner-Protocol: exomem-cell-provisioner.v1' \
  -H "Idempotency-Key: ${IDEMPOTENCY_KEY}" \
  --data-binary "@${restore_request}"
```

## Verify

```bash
kubectl get jobs -n exomem-system -l exomem.io/durability-operation
kubectl get pvc -A -l exomem.io/restore-candidate
kubectl -n exomem-platform get cronjob \
  exomem-durability-backup exomem-database-backup exomem-export-gc
kubectl -n exomem-platform get configmap/exomem-durability-storage \
  -o jsonpath='{.data.recovery-bucket}{"\n"}{.data.user-export-bucket}{"\n"}{.data.database-backup-bucket}{"\n"}'
```

For backup, require remote size/digest/metadata proof and route closure-to-resume
within two minutes. For restore, require a new destination binding, authenticated
readiness, and capture/recall/review/export.
