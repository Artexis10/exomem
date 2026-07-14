# Backup and restore

## Preconditions

Use the newest remotely verified encrypted object, not job-start time. Backup
age above 45 minutes warns; 60 minutes blocks invitations. A restore always uses
a new stopped candidate identity and a bounded scratch volume.

```bash
python3 infra/scripts/external_blackbox.py --contract infra/contracts/observability-v1.json
```

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
```

For backup, require remote size/digest/metadata proof and route closure-to-resume
within two minutes. For restore, require a new destination binding, authenticated
readiness, and capture/recall/review/export.
