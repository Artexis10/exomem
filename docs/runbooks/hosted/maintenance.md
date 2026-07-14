# Cell maintenance gate

## Preconditions

The durable provisioner operation lock must be available. Maintenance is never
performed by deleting routes or scaling a StatefulSet by hand.

```bash
operation_id=replace-opaque-operation
cell_id=replace-opaque-cell
bearer_file=/secure/operator/provisioner-api.bearer
quiesce_request=/secure/operator/quiesce-request.json
resume_request=/secure/operator/resume-request.json
test "$(stat -c %a "$bearer_file")" = 600
test "$(stat -c %a "$quiesce_request")" = 600
test "$(stat -c %a "$resume_request")" = 600
```

Forward the internal API without exposing it from the cluster. Keep this process
running in a second terminal until both calls and their retries have completed:

```bash
kubectl -n exomem-platform port-forward service/exomem-provisioner 18080:8080
```

Invoke the exact product action with the saved request body and original
idempotency key. A `202` response is pending: retry the identical command and
body after `retryAfterSeconds`; do not mint a new key.

```bash
curl --fail-with-body --silent --show-error --max-redirs 0 --max-time 30 \
  -X POST http://127.0.0.1:18080/cells/quiesce \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "$(<"$bearer_file")") \
  -H 'Content-Type: application/json' \
  -H 'X-Exomem-Provisioner-Protocol: exomem-cell-provisioner.v1' \
  -H "Idempotency-Key: ${operation_id}-quiesce" \
  --data-binary "@${quiesce_request}"
```

Quiesce must close control and transfer routes, externally prove both closed,
drain active work, and only then report routing stopped. Resume only after the
maintenance work and verification succeed:

```bash
curl --fail-with-body --silent --show-error --max-redirs 0 --max-time 30 \
  -X POST http://127.0.0.1:18080/cells/resume \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "$(<"$bearer_file")") \
  -H 'Content-Type: application/json' \
  -H 'X-Exomem-Provisioner-Protocol: exomem-cell-provisioner.v1' \
  -H "Idempotency-Key: ${operation_id}-resume" \
  --data-binary "@${resume_request}"
```

## Verify

```bash
kubectl get ingressroute -A -l "exomem.io/cell=$cell_id"
kubectl get lease -A -l "exomem.io/operation=$operation_id"
```

After release/resume, require authenticated runtime readiness before either route
reopens. An unused ticket issued before maintenance must remain unusable.
