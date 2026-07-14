# Cell inspection and retry

## Preconditions

Use only opaque tenant, cell, request, and operation IDs from the provisioner.
Do not use email/name selectors, edit the database, or hand-create a cell.

For the operator-side invitation check, require authenticated, mode-`0600`
receipts from the live Kubernetes/HCloud collector and reviewed
provider-invoice/Paddle statement collector. Handwritten observation JSON is
not accepted:

The in-cluster `exomem-capacity-receipt-collector` runs every minute, counts
only governed tenant Namespaces and attached HCloud volumes, advances its
keep-policy ConfigMap sequence, and signs a five-minute receipt with
`exomem-capacity-receipt-signer/private-key`. Its separate HCloud token is
read-only. Export the latest signed receipt without editing it:

```bash
umask 077
capacity_receipt=/secure/operator/capacity/live-capacity.receipt.json
kubectl -n exomem-platform get configmap/exomem-capacity-receipt \
  -o jsonpath='{.data.receipt\.json}' > "$capacity_receipt"
test -s "$capacity_receipt"
```

The independent economics collector runs the repository entrypoint below in
its isolated environment. Its mode-`0600` evidence JSON contains the reviewed
numeric cost/Paddle fields; the collector hashes the actual provider invoice
and Paddle statement, requires them to match the reviewed capacity contract,
and writes a 31-day domain-separated receipt. Its private key never crosses
into the operator environment.

```bash
infra/helm/platform/files/operational_receipt_collector.py economics \
  --contract infra/operations/private-alpha-capacity-v1.json \
  --evidence /secure/collector/economics-evidence.json \
  --provider-invoice /secure/collector/provider-invoice.pdf \
  --paddle-statement /secure/collector/paddle-statement.csv \
  --private-key-file /secure/collector/economics-receipt.private \
  --sequence 1 \
  --output /secure/collector/live-economics.receipt.json
```

Both reviewed public-key IDs, the verified cost fields, and the two statement
digests must be non-null in `private-alpha-capacity-v1.json`; its chart copy and
configured SHA-256 must remain byte-identical before deployment.

```bash
capacity_receipt=/secure/operator/capacity/live-capacity.receipt.json
economics_receipt=/secure/operator/capacity/live-economics.receipt.json
capacity_public_key=/secure/operator/capacity/live-capacity-collector.public.pem
economics_public_key=/secure/operator/capacity/live-economics-collector.public.pem
capacity_replay_state=/secure/operator/capacity/gate-replay-state.json
test "$(stat -c %a "$capacity_receipt")" = 600
test "$(stat -c %a "$economics_receipt")" = 600
test -f "$capacity_public_key" && test ! -L "$capacity_public_key"
test -f "$economics_public_key" && test ! -L "$economics_public_key"
infra/scripts/capacity_gate.py \
  --contract infra/operations/private-alpha-capacity-v1.json \
  --capacity-receipt "$capacity_receipt" \
  --economics-receipt "$economics_receipt" \
  --capacity-public-key-file "$capacity_public_key" \
  --economics-public-key-file "$economics_public_key" \
  --replay-state "$capacity_replay_state"

cell_namespace=cell-replace-opaque
kubectl get namespace "$cell_namespace" -o jsonpath='{.metadata.labels.exomem\.io/tenant-cell}{"\n"}'
kubectl get statefulset,pvc,service,networkpolicy -n "$cell_namespace"
```

This check does not replace the production gate: the provisioner worker
re-queries tenant namespaces and attached HCloud volumes through
`KubernetesHCloudCapacityGate` immediately before namespace or PVC creation.
The operator workstation holds only the two Ed25519 public keys. The independent
capacity signer is confined to its dedicated K3s collector CronJob; the
economics signer stays in the external provider/Paddle collector. Neither
private key reaches the capacity gate, provisioner API/routine worker, or
operator workstation, and neither public verifier can sign a receipt.

Every rendered tenant object carries its own
`exomem.io/recovery-envelope`, supplied through the cell chart's exact
`providerRecoveryEnvelopes` map. The Ed25519 v1 payload binds that one object's
canonical provider reference to the opaque tenant, cell, operation, and fence
generation. Reusing an envelope from the Namespace, PVC, StatefulSet, or either
IngressRoute for any other object is an authentication failure. The routine
worker receives only `EXOMEM_PROVIDER_RECOVERY_PUBLIC_KEY`; it never receives
the signing seed.

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
