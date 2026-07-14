# Retained-volume rebind

## Preconditions

The original HCloud `volumeHandle`, location, opaque tenant/cell IDs, operation,
and maximum provider fence must be in external state. The volume must be detached
and its immutable labels must match. Do not use the namespaced credential.

```bash
kubectl get pv -o custom-columns=NAME:.metadata.name,HANDLE:.spec.csi.volumeHandle,PHASE:.status.phase
```

There is deliberately no public rebind endpoint. Submit the complete restore
request through `POST /cells/restore`; the production worker invokes
`VolumeLifecycleWorker.rebind_static` only after it verifies the stored handle,
location, immutable labels, detach state, and fence. Never call the worker
primitive or create a PV by hand.

```bash
: "${IDEMPOTENCY_KEY:?use the original restore-operation key}"
bearer_file=/secure/operator/provisioner-api.bearer
restore_request=/secure/operator/volume-rebind-restore-request.json
test "$(stat -c %a "$bearer_file")" = 600
test "$(stat -c %a "$restore_request")" = 600
kubectl -n exomem-platform port-forward service/exomem-provisioner 18080:8080
```

With the port-forward running, retry the byte-identical request while it is
pending:

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
kubectl get pv -o jsonpath='{range .items[*]}{.spec.csi.volumeHandle}{" "}{.status.phase}{"\n"}{end}'
kubectl get pvc -n cell-replace-opaque
```

Require the recorded handle, one Bound PVC, authenticated readiness, and the
pre-loss canary note. A new dynamic volume is a failed recovery.
