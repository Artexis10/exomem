# Cell inspection and retry

## Preconditions

Use only opaque tenant, cell, request, and operation IDs from the provisioner.
Do not use email/name selectors, edit the database, or hand-create a cell.

For the operator-side invitation check, require authenticated, mode-`0600`
receipts from the live Kubernetes/HCloud collector and reviewed
provider-invoice/Paddle statement collector. Handwritten observation JSON is
not accepted:

```bash
capacity_receipt=/secure/operator/capacity/live-capacity.receipt.json
economics_receipt=/secure/operator/capacity/live-economics.receipt.json
capacity_key=/secure/operator/capacity/live-capacity-authentication.key
economics_key=/secure/operator/capacity/live-economics-authentication.key
test "$(stat -c %a "$capacity_receipt")" = 600
test "$(stat -c %a "$economics_receipt")" = 600
test "$(stat -c %a "$capacity_key")" = 600
test "$(stat -c %a "$economics_key")" = 600
infra/scripts/capacity_gate.py \
  --contract infra/operations/private-alpha-capacity-v1.json \
  --capacity-receipt "$capacity_receipt" \
  --economics-receipt "$economics_receipt" \
  --capacity-key-file "$capacity_key" \
  --economics-key-file "$economics_key"

cell_namespace=cell-replace-opaque
kubectl get namespace "$cell_namespace" -o jsonpath='{.metadata.labels.exomem\.io/tenant-cell}{"\n"}'
kubectl get statefulset,pvc,service,networkpolicy -n "$cell_namespace"
```

This check does not replace the production gate: the provisioner worker
re-queries tenant namespaces and attached HCloud volumes through
`KubernetesHCloudCapacityGate` immediately before namespace or PVC creation.

Retry through the same Substrate endpoint and idempotency key. A pending result
is healthy progress; never invent a new key to bypass it.

For direct operator diagnosis, use the authenticated internal API and the exact
mode-`0600` health request captured by the control plane. It contains the full
target identity and no human identifier:

```bash
: "${IDEMPOTENCY_KEY:?use the original health-operation key}"
bearer_file=/secure/operator/provisioner-api.bearer
health_request=/secure/operator/cell-health-request.json
test "$(stat -c %a "$bearer_file")" = 600
test "$(stat -c %a "$health_request")" = 600
kubectl -n exomem-platform port-forward service/exomem-provisioner 18080:8080
```

With the port-forward left running, call and retry the same action verbatim
until it returns a final `200` response:

```bash
curl --fail-with-body --silent --show-error --max-redirs 0 --max-time 30 \
  -X POST http://127.0.0.1:18080/cells/health \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "$(<"$bearer_file")") \
  -H 'Content-Type: application/json' \
  -H 'X-Exomem-Provisioner-Protocol: exomem-cell-provisioner.v1' \
  -H "Idempotency-Key: ${IDEMPOTENCY_KEY}" \
  --data-binary "@${health_request}"
```

## Verify

```bash
kubectl get pvc -n "$cell_namespace" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}'
kubectl get pods -n "$cell_namespace" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[0].ready}{"\n"}{end}'
```

Exactly one 10 GiB claim and one ready serving pod are expected.
