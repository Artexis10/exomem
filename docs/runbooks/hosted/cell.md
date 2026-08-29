<!-- authority:non-specification -->

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

## Init-retry false-negative recovery

This is the only manual provider-operation recovery. Use it only for the
recorded `PROVISION / ERROR / failed /
PROVISIONER_PROVIDER_METADATA_CONFLICT` init-retry false-negative, after the
selected signed provisioner image contains the recovery command. It is not an
API action, SQL procedure, or a way to edit any other operation state.

Keep Substrate reconciliation suspended. Capture the current controller state,
suspend every database-mutating reconcile CronJob, drain its child Jobs, and
scale the API, routine worker, and volume worker to zero. Leave the receipt
collector running. Stop if any migration/bootstrap process or other database
consumer remains, or if the collector cannot provide a fresh signed receipt.

Supply the confidential internal operation ID only through a current-user-owned
regular mode-`0600` file. Do not put it in a shell history, command argument,
manifest, log, receipt, or ticket.

```bash
umask 077
recovery_identity=/secure/operator/recovery-operation-id
test -f "$recovery_identity" && test ! -L "$recovery_identity"
test "$(stat -c %u "$recovery_identity")" = "$(id -u)"
test "$(stat -c %a "$recovery_identity")" = 600
```

The recovery helper runs only in a short-lived, non-network-published operator
Pod. Use the exact digest selected by the deployment lock; do not use a tag or
substitute the API/worker ServiceAccount. The chart renders the dedicated
`exomem-init-retry-recovery` ServiceAccount and read-only ClusterRole. It can
observe only namespaces, PVCs/PVs, ConfigMaps, StatefulSets, Jobs, and
IngressRoutes. It cannot read Secrets or create, update, patch, or delete any
Kubernetes object. Stop if that rendered role differs; do not widen the routine
provisioner role.

The Pod has no Service, has default-deny ingress, runs read-only as non-root
with no capabilities, and uses its projected in-cluster token only for those
read-only observations. Create it with an empty fixed command, then stream the
confidential identity from the local mode-0600 file. The identity never enters
the manifest, argv, a Secret, an annotation, logs, shell history, or a remote
file.

```bash
set -euo pipefail
operator_pod=exomem-init-retry-recovery
helm_release=exomem-platform
mapfile -t lock_names < <(kubectl -n exomem-platform get configmap \
  -l app.kubernetes.io/name=exomem-hosted-deployment-lock \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
test "${#lock_names[@]}" -eq 1
lock_name="${lock_names[0]}"
lock_digest="$(kubectl -n exomem-platform get configmap "$lock_name" \
  -o jsonpath='{.metadata.annotations.exomem\.io/deployment-lock-sha256}')"
lock_key="$(kubectl -n exomem-platform get configmap "$lock_name" -o json | jq -r \
  '.data | keys[] | select(test("^exomem-hosted-deployment-lock-v[23]\\.json$"))')"
[[ "$lock_key" =~ ^exomem-hosted-deployment-lock-v[23]\.json$ ]] || exit 1
lock_json="$(kubectl -n exomem-platform get configmap "$lock_name" -o json | \
  jq -r --arg key "$lock_key" '.data[$key]')"
test "$(printf %s "$lock_json" | sha256sum | awk '{print $1}')" = "$lock_digest"
helm_manifest="$(helm -n "$helm_release" get manifest "$helm_release")"
printf '%s\n' "$helm_manifest" | yq -e --arg name "$lock_name" --arg digest "$lock_digest" \
  'select(.kind == "ConfigMap" and .metadata.name == $name and
          .metadata.annotations."exomem.io/deployment-lock-sha256" == $digest)' >/dev/null
operator_image="$(printf %s "$lock_json" | jq -r '.components.provisioner.image')"
[[ "$operator_image" =~ ^ghcr\.io/artexis10/exomem-provisioner@sha256:[a-f0-9]{64}$ ]] || exit 1
runtime_selection="$(kubectl -n exomem-platform get deployment exomem-provisioner-api \
  -o jsonpath='{.spec.template.metadata.annotations.exomem\.io/runtime-selection}')"
case "$runtime_selection" in active|rollback) ;; *) exit 1 ;; esac
kubectl -n exomem-platform apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $operator_pod
  labels:
    app.kubernetes.io/name: exomem-init-retry-recovery
spec:
  enableServiceLinks: false
  serviceAccountName: exomem-init-retry-recovery
  automountServiceAccountToken: true
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: recovery
      image: $operator_image
      imagePullPolicy: IfNotPresent
      command: ["/bin/sh", "-c", "sleep 1200"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        runAsNonRoot: true
        capabilities: {drop: [ALL]}
      env:
        - {name: EXOMEM_RECOVERY_DATABASE_URL, valueFrom: {secretKeyRef: {name: exomem-provisioner-database, key: url}}}
        - {name: EXOMEM_RECOVERY_ENVELOPE_KEY, valueFrom: {secretKeyRef: {name: exomem-provisioner-wrapping-key, key: key-material}}}
        - {name: EXOMEM_RECOVERY_PROVIDER_RECOVERY_PUBLIC_KEY, valueFrom: {secretKeyRef: {name: exomem-provider-recovery-verifier, key: public-key}}}
        - {name: EXOMEM_RECOVERY_HCLOUD_TOKEN, valueFrom: {secretKeyRef: {name: exomem-hcloud-capacity-reader, key: token}}}
        - {name: EXOMEM_RECOVERY_DEPLOYMENT_LOCK_JSON, valueFrom: {configMapKeyRef: {name: $lock_name, key: $lock_key}}}
        - {name: EXOMEM_RECOVERY_RUNTIME_SELECTION, value: $runtime_selection}
        - {name: EXOMEM_RECOVERY_DATABASE_SCHEMA, value: exomem_provisioner}
        - {name: EXOMEM_RECOVERY_DATABASE_ROLE, value: exomem_provisioner_runtime}
        - {name: EXOMEM_RECOVERY_DATABASE_LOCK_TIMEOUT_SECONDS, value: "60"}
        - {name: EXOMEM_RECOVERY_HCLOUD_LOCATION, value: fsn1}
EOF
kubectl -n exomem-platform wait --for=condition=Ready "pod/$operator_pod" --timeout=60s
```

Before the preflight, scale the routine provisioner worker to zero and leave it
there through reopen and recovery verification. This is not a global database
quiesce: do not run a migration, backup, hold, or schema operation. Confirm the
production database revision is exactly `0006_operation_wire_protocol`; an
unknown revision or `0007` refuses. v0.46's 0007 provisioner candidate was
never deployed.

```bash
kubectl -n exomem-platform scale deployment/exomem-provisioner-worker --replicas=0
kubectl -n exomem-platform rollout status deployment/exomem-provisioner-worker --timeout=60s
```

The dedicated recovery environment is supplied only as the exact set of
read-only references below: database URL, envelope key, recovery public key,
read-only HCloud token, deterministic location/schema/role/timeout, and the
selected lock JSON. Do not add bearer, signing private key, capacity mutation, Helm mutation, or
volume-encryption configuration. Use the approved operator secret projection
mechanism for those references; its Secret names/keys are never recorded in
the identity input or output.

Run the next commands in the same Bash shell; each helper step must return
success before the next one starts.

```bash
set -euo pipefail
run_recovery() {
  local mode="$1"
  timeout 75s kubectl -n exomem-platform exec -i "$operator_pod" -- \
    exomem-provisioner-recover-init-retry "$mode" --stdin < "$recovery_identity"
}
run_recovery preflight
run_recovery reopen
run_recovery verify-recovery
recovery_verified=true
```

Any refusal, conflict, resource drift, recovery-verification failure, or timeout
is a stop condition. In particular, if the `reopen` acknowledgement is uncertain,
run `verify-recovery` first; never issue another `reopen`. Never reverse the
checkpoint manually. Preserve only content-free JSON output and the recovery digest.

Only after `verify-recovery` returns successfully may the routine worker be
restored. Keep this Pod alive through the complete ten-minute inspection window;
its 20-minute sleep gives the bounded command and cleanup margin. Bounded-poll
the content-free `inspect` result until `FINAL / complete`; stop on terminal
error, unexpected resource mutation, refusal, or timeout. Restore the API only
after provider finalization, run one manual Substrate reconcile Job at a time
until the original control operation succeeds and the cell is ready, then
restore the volume worker/durability state and scheduled reconciliation last.

```bash
test "$recovery_verified" = true
kubectl -n exomem-platform scale deployment/exomem-provisioner-worker --replicas=1
final=false
for attempt in $(seq 1 20); do
  inspect_output="$(run_recovery inspect)"
  if printf '%s' "$inspect_output" | jq -e \
    '.state == "final" and .checkpoint == "complete" and .final_proof == true' >/dev/null; then
    final=true
    break
  fi
  sleep 30
done
test "$final" = true
```

When inspection is complete or any stop condition occurs, delete the Pod and
confirm it is gone. Keep the local identity file until the incident record is
closed, then remove it using the operator's approved local retention procedure.

```bash
kubectl -n exomem-platform delete pod "$operator_pod" --wait=true --timeout=60s
kubectl -n exomem-platform get pod "$operator_pod"
```

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
