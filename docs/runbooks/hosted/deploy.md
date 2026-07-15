# Reviewed hosted deployment

## Preconditions

The complete application release manifest, real B2 locking proof, static-secret
ciphertexts, and owner-only invitation gate must be green. Production mutation
always uses a saved plan; a second plan is never computed during apply. The
release manifest is the reviewed artifact path exported by the release pipeline
with the exact `exomem-hosted-release`
schema: source commit, release, hosted protocol, immutable runtime image,
published tag, both contract digests, and the ordered 21-command registry.

```bash
infra/scripts/validate.sh
openspec validate add-hosted-private-alpha-infrastructure --strict
```

Generate non-sensitive inventory and run the governed two-pass convergence gate:

```bash
set -euo pipefail
umask 077
command -v jq >/dev/null
deploy_work_dir="$(mktemp -d)"
trap 'rm -rf -- "${deploy_work_dir}"' EXIT
terraform -chdir=infra/terraform/foundation output -json \
  | jq '{server_ipv4, private_node_ip}' > "${deploy_work_dir}/foundation-output.json"
infra/scripts/generate_ansible_inventory.py \
  "${deploy_work_dir}/foundation-output.json" "${deploy_work_dir}/inventory.json"
infra/scripts/verify_ansible_convergence.py --inventory "${deploy_work_dir}/inventory.json" \
  --vars infra/secrets/ansible/k3s-server-token.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-access-key.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-secret-key.v1.sops.json
```

Prepare one private Helm-values file from that release unit. The chart renders
the authoritative `exomem-hosted-release-v1` ConfigMap from the embedded unit;
the preparer rejects unknown/missing fields, mutable images, a tag/commit
mismatch, partial overrides, and an invalid command registry. Never set the
image, release, protocol, registry, or digest independently.
`EXOMEM_PROVISIONER_IMAGE` is the separately built control-plane artifact, not
an override for the cell runtime image inside the release manifest.

The minute capacity collector is also fail-closed. Before rendering, its
operator contract and chart copy must be byte-identical, the live cost/Paddle
evidence and statement digests must be reviewed, and both collector public-key
IDs must be pinned:

```bash
cmp infra/operations/private-alpha-capacity-v1.json \
  infra/helm/platform/files/private-alpha-capacity-v1.json
jq -e '
  .live_costs_verified == true and
  (.receipt_authentication.capacity_public_key_id | test("^[a-f0-9]{64}$")) and
  (.receipt_authentication.economics_public_key_id | test("^[a-f0-9]{64}$")) and
  (.evidence.provider_invoice_reference | test("^[a-f0-9]{64}$")) and
  (.evidence.paddle_statement_reference | test("^[a-f0-9]{64}$"))
' infra/operations/private-alpha-capacity-v1.json >/dev/null
```

```bash
: "${EXOMEM_HOSTED_RELEASE_MANIFEST:?set the reviewed release-manifest path from the release pipeline}"
release_manifest="$EXOMEM_HOSTED_RELEASE_MANIFEST"
: "${EXOMEM_PROVISIONER_IMAGE:?set the reviewed ghcr.io/artexis10/exomem-provisioner@sha256 digest}"
infra/scripts/verify_provisioner_image.py --image "$EXOMEM_PROVISIONER_IMAGE"
test -f "$release_manifest"
test ! -L "$release_manifest"
control_hostname="$(terraform -chdir=infra/terraform/foundation output -raw control_hostname)"
transfer_hostname="$(terraform -chdir=infra/terraform/foundation output -raw transfer_hostname)"
infra/scripts/prepare_hosted_release.py \
  --manifest "$release_manifest" \
  --values-output "${deploy_work_dir}/release-values.json" \
  --provisioner-image "$EXOMEM_PROVISIONER_IMAGE" \
  --control-hostname "$control_hostname" \
  --transfer-hostname "$transfer_hostname"

: "${EXOMEM_B2_S3_ENDPOINT:?set the exact HTTPS B2 S3 origin}"
: "${EXOMEM_B2_S3_REGION:?set the B2 S3 region}"
: "${EXOMEM_DATABASE_BACKUP_PROOF_TENANT_ID:?set the opaque owner-proof tenant ID}"
: "${EXOMEM_DATABASE_BACKUP_PROOF_CELL_ID:?set the opaque owner-proof cell ID}"
recovery_bucket="$(terraform -chdir=infra/terraform/durability output -raw recovery_bucket_name)"
user_export_bucket="$(terraform -chdir=infra/terraform/durability output -raw user_export_bucket_name)"
database_backup_bucket="$(terraform -chdir=infra/terraform/durability output -raw database_backup_bucket_name)"
durability_values="${deploy_work_dir}/durability-values.json"
jq -n \
  --arg endpoint "$EXOMEM_B2_S3_ENDPOINT" \
  --arg region "$EXOMEM_B2_S3_REGION" \
  --arg recovery "$recovery_bucket" \
  --arg export "$user_export_bucket" \
  --arg database "$database_backup_bucket" \
  --arg proof_tenant "$EXOMEM_DATABASE_BACKUP_PROOF_TENANT_ID" \
  --arg proof_cell "$EXOMEM_DATABASE_BACKUP_PROOF_CELL_ID" \
  '{durability: {b2Endpoint: $endpoint, b2Region: $region,
    recoveryBucket: $recovery, userExportBucket: $export,
    databaseBackupBucket: $database, databaseBackup: {
      proofTenantId: $proof_tenant, proofCellId: $proof_cell}}}' \
  > "$durability_values"
chmod 0600 "$durability_values"

# Bind every capacity receipt and worker observation to the exact foundation server.
hcloud_server_id="$(terraform -chdir=infra/terraform/foundation output -raw server_id)"
case "$hcloud_server_id" in
  ''|*[!0-9]*|0) echo "foundation server_id is invalid" >&2; exit 1 ;;
esac
capacity_values="${deploy_work_dir}/capacity-values.json"
jq -n --argjson server_id "$hcloud_server_id" \
  '{capacityCollector: {hcloudServerId: $server_id}}' > "$capacity_values"
chmod 0600 "$capacity_values"
helm template exomem-platform infra/helm/platform --namespace exomem-platform \
  --values infra/helm/platform/values.yaml \
  --values "${deploy_work_dir}/release-values.json" \
  --values "$capacity_values" \
  --values "$durability_values" \
  --show-only templates/namespaces.yaml | kubectl apply -f -
```

Apply every declared active K3s ciphertext before installing the immutable
platform release. The registry is an Ed25519-signed, matrix-bound artifact from
the secret-release custodian and names one explicit version and ciphertext
digest for every active K3s destination. The deployment operator holds only the
registry public key. Missing, extra, stale, changed, or unsigned entries stop
deployment before Helm runs.

The provider-recovery identity keypair is split across two Kubernetes Secrets.
`exomem-provider-recovery-signer/private-key` is mounted only into the API
identity issuer, the two scheduled backup workloads, and the privileged volume
worker;
`exomem-provider-recovery-verifier/public-key` is the raw 32-byte Ed25519 public
key encoded as unpadded base64url. It is the only half mounted into the routine
worker and short-lived deletion Jobs; the vault-backup reconciler mounts it alongside the signer
to authenticate discovered provider objects and prove both halves share one
trust root. Generate and hand off both halves as one atomic keypair operation, then
include both exact ciphertext artifacts in the signed active-secret registry.
Never place both halves in one Secret or mount the signing seed into the routine
or deletion Job. The volume worker derives its verifier from this same seed;
a second recovery signing root is forbidden because a single-key verifier could
not rediscover its objects. Create both ciphertexts and the escrow copy through
the atomic handoff only:

```bash
SOPS_AGE_RECIPIENTS=age1... \
  infra/scripts/provider_recovery_keypair_handoff.py \
  --matrix infra/contracts/secret-destinations-v1.json \
  --repository-root "$PWD" --version v1 --pair provider-recovery
```

Generate the independent capacity, economics, and rotation receipt pairs through
the same atomic boundary. Private seeds route only to the named collector. The
capacity public verifier routes both to operator escrow and to
`exomem-capacity-receipt-verifier/public-key`, which is consumed only by the
routine and volume-registration workers; the other public verifiers remain in
operator escrow:

```bash
for pair in capacity-receipt economics-receipt rotation-receipt; do
  SOPS_AGE_RECIPIENTS=age1... \
    infra/scripts/provider_recovery_keypair_handoff.py \
    --matrix infra/contracts/secret-destinations-v1.json \
    --repository-root "$PWD" --version v1 --pair "$pair"
done
```

```bash
: "${EXOMEM_ACTIVE_SECRET_REGISTRY:?set the signed active-secret registry path}"
: "${EXOMEM_ACTIVE_SECRET_REGISTRY_PUBLIC_KEY:?set its trusted Ed25519 public-key path}"
infra/scripts/apply_active_sops_secrets.py \
  --matrix infra/contracts/secret-destinations-v1.json \
  --registry "$EXOMEM_ACTIVE_SECRET_REGISTRY" \
  --registry-public-key "$EXOMEM_ACTIVE_SECRET_REGISTRY_PUBLIC_KEY" \
  --trust-contract infra/contracts/active-secret-registry-v1.json
```

Bootstrap the dedicated provisioner role/schema before the first Helm install.
The runtime URL is already in `exomem-provisioner-database`; the admin URL is
operator-only and MUST arrive through a non-printing prompt, FIFO, or provider
credential helper. Never put it in a command argument, the destination matrix,
or a permanent cluster Secret. This one-shot Job uses the same immutable image
as the release and its command emits only a content-free failure.

For Neon, both URLs use the direct
`postgresql+asyncpg://ROLE:PASSWORD@ep-<endpoint-id>.<region>.aws.neon.tech/DATABASE?ssl=require`
shape; the `ep-<endpoint-id>-pooler...neon.tech` transaction-pooling endpoint is
unsupported. A separately reviewed proxy with a backend-session-affinity
guarantee may use `pool_mode=session` as the local contract marker. That marker
does not make a transaction pool safe.

```bash
: "${EXOMEM_DATABASE_ADMIN_ROTATION_RECEIPT:?set the private receipt path}"
: "${EXOMEM_DATABASE_BOOTSTRAP_ATTEMPT_STATE:?set a persistent private attempt-state path}"
set +x
bootstrap_secret=exomem-provisioner-database-bootstrap-admin
bootstrap_job=exomem-provisioner-database-bootstrap
bootstrap_state="$EXOMEM_DATABASE_BOOTSTRAP_ATTEMPT_STATE"
if test -e "$bootstrap_state"; then
  test -f "$bootstrap_state"
  test ! -L "$bootstrap_state"
  test "$(stat -c %u "$bootstrap_state")" = "$(id -u)"
  case "$(stat -c %a "$bootstrap_state")" in 400|600) ;; *) exit 1 ;; esac
  prior_attempt_id="$(jq -er '.attemptId' "$bootstrap_state")"
  prior_credential_version="$(jq -er '.credentialVersion' "$bootstrap_state")"
  prior_attempt_started_ns="$(jq -er '.attemptStartNs' "$bootstrap_state")"
  infra/scripts/database_bootstrap_rotation_gate.py \
    --receipt "$EXOMEM_DATABASE_ADMIN_ROTATION_RECEIPT" \
    --attempt-id "$prior_attempt_id" \
    --credential-version "$prior_credential_version" \
    --attempt-start-ns "$prior_attempt_started_ns" \
    --job-status 0
  rm -- "$bootstrap_state"
fi
: "${EXOMEM_PROVISIONER_DATABASE_ADMIN_URL:?read the one-use admin URL without shell tracing}"
: "${EXOMEM_DATABASE_ADMIN_CREDENTIAL_VERSION:?set the non-secret provider credential version}"
if kubectl -n exomem-platform get "secret/${bootstrap_secret}" >/dev/null 2>&1 \
  || kubectl -n exomem-platform get "job/${bootstrap_job}" >/dev/null 2>&1; then
  echo "stale database bootstrap authority requires deletion and provider rotation" >&2
  exit 1
fi
bootstrap_attempt_id="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
bootstrap_attempt_started_ns="$(date +%s%N)"
case "$EXOMEM_DATABASE_ADMIN_CREDENTIAL_VERSION" in
  *[!A-Za-z0-9._:-]*|'') echo "database admin credential version is invalid" >&2; exit 1 ;;
esac
test "${#EXOMEM_DATABASE_ADMIN_CREDENTIAL_VERSION}" -le 128
bootstrap_state_tmp="${bootstrap_state}.new.$$"
test ! -e "$bootstrap_state_tmp"
(umask 077; jq -n \
  --arg attempt "$bootstrap_attempt_id" \
  --arg version "$EXOMEM_DATABASE_ADMIN_CREDENTIAL_VERSION" \
  --argjson started "$bootstrap_attempt_started_ns" \
  '{schemaVersion: 1, attemptId: $attempt, credentialVersion: $version,
    attemptStartNs: $started}' > "$bootstrap_state_tmp")
mv -- "$bootstrap_state_tmp" "$bootstrap_state"
bootstrap_materialized=0
bootstrap_job_status=0
bootstrap_signal_status=0
bootstrap_cleanup_status=0
bootstrap_cleanup() {
  kubectl -n exomem-platform delete "job/${bootstrap_job}" \
    "secret/${bootstrap_secret}" --ignore-not-found --wait=true >/dev/null \
    || bootstrap_cleanup_status=$?
  unset EXOMEM_PROVISIONER_DATABASE_ADMIN_URL
}
trap 'bootstrap_cleanup; rm -rf -- "${deploy_work_dir}"' EXIT
trap 'bootstrap_signal_status=130' INT
trap 'bootstrap_signal_status=143' TERM
bootstrap_materialized=1
set +e
printf '%s' "$EXOMEM_PROVISIONER_DATABASE_ADMIN_URL" \
  | kubectl -n exomem-platform create secret generic "$bootstrap_secret" \
      --from-file=url=/dev/stdin --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
bootstrap_job_status=$?
unset EXOMEM_PROVISIONER_DATABASE_ADMIN_URL
if test "$bootstrap_job_status" -eq 0; then
  kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${bootstrap_job}
  namespace: exomem-platform
  annotations:
    exomem.io/database-bootstrap-attempt: ${bootstrap_attempt_id}
    exomem.io/database-admin-credential-version: ${EXOMEM_DATABASE_ADMIN_CREDENTIAL_VERSION}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 300
  template:
    spec:
      automountServiceAccountToken: false
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: bootstrap
          image: ${EXOMEM_PROVISIONER_IMAGE}
          command: ["exomem-provisioner-database-bootstrap"]
          env:
            - name: EXOMEM_PROVISIONER_DATABASE_ADMIN_URL
              valueFrom:
                secretKeyRef: {name: ${bootstrap_secret}, key: url}
            - name: EXOMEM_PROVISIONER_DATABASE_URL
              valueFrom:
                secretKeyRef: {name: exomem-provisioner-database, key: url}
            - {name: EXOMEM_PROVISIONER_DATABASE_SCHEMA, value: exomem_provisioner}
            - {name: EXOMEM_PROVISIONER_DATABASE_ROLE, value: exomem_provisioner_runtime}
            - {name: EXOMEM_PROVISIONER_DATABASE_LOCK_TIMEOUT_SECONDS, value: "60"}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            runAsUser: 10001
            runAsGroup: 10001
            capabilities: {drop: [ALL]}
EOF
  bootstrap_job_status=$?
fi
if test "$bootstrap_job_status" -eq 0; then
  timeout --signal=TERM --kill-after=10s 310s \
    kubectl -n exomem-platform wait --for=condition=complete \
      "job/${bootstrap_job}" --timeout=300s
  bootstrap_job_status=$?
fi
if test "$bootstrap_signal_status" -ne 0; then
  bootstrap_job_status="$bootstrap_signal_status"
fi
bootstrap_cleanup
if test "$bootstrap_cleanup_status" -ne 0 && test "$bootstrap_job_status" -eq 0; then
  bootstrap_job_status="$bootstrap_cleanup_status"
fi
trap 'rm -rf -- "${deploy_work_dir}"' EXIT
trap - INT TERM
set -e
test -z "${EXOMEM_PROVISIONER_DATABASE_ADMIN_URL:-}"
echo "Rotate or revoke the database admin credential, then write the current attempt-bound receipt." >&2
echo "Attempt: ${bootstrap_attempt_id}; credential version: ${EXOMEM_DATABASE_ADMIN_CREDENTIAL_VERSION}" >&2
if test "$bootstrap_materialized" -eq 1; then
  infra/scripts/database_bootstrap_rotation_gate.py \
    --receipt "$EXOMEM_DATABASE_ADMIN_ROTATION_RECEIPT" \
    --attempt-id "$bootstrap_attempt_id" \
    --credential-version "$EXOMEM_DATABASE_ADMIN_CREDENTIAL_VERSION" \
    --attempt-start-ns "$bootstrap_attempt_started_ns" \
    --job-status "$bootstrap_job_status"
fi
rm -- "$bootstrap_state"
test "$bootstrap_job_status" -eq 0
```

Delete and provider-rotate or revoke the admin credential after every attempt,
including a failed Job or interrupted operator session. The content-free
rotation/revocation receipt is a mandatory live gate because this repository has
no reviewed provider API for that mutation. The receipt is owner-readable only,
is newer than the attempt boundary, and contains exactly this content-free shape:

```json
{
  "schemaVersion": 1,
  "kind": "exomem-database-admin-rotation",
  "attemptId": "<attempt printed by the runbook>",
  "credentialVersion": "<EXOMEM_DATABASE_ADMIN_CREDENTIAL_VERSION>",
  "rotatedOrRevokedAt": "<current RFC 3339 UTC timestamp>"
}
```

The persistent private attempt-state file prevents a retry from materializing
another credential until the preceding attempt's exact receipt has passed. Do
not put that state inside `deploy_work_dir`, which is intentionally ephemeral.
Confirm both ephemeral resources are absent before continuing. A later chart
upgrade never receives admin authority:
its pre-upgrade hook only validates that the database is already at the new
image head and blocks any revision-advancing rollout.

```bash
test -z "$(kubectl -n exomem-platform get "job/${bootstrap_job}" \
  "secret/${bootstrap_secret}" --ignore-not-found -o name)"

helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform \
  --values infra/helm/platform/values.yaml \
  --values "${deploy_work_dir}/release-values.json" \
  --values "$capacity_values" \
  --values "$durability_values" --atomic --wait --timeout 10m
```

## Verify

```bash
kubectl wait --for=condition=Available deployment/exomem-cloudflared -n exomem-platform --timeout=180s
kubectl wait --for=condition=Available deployment/exomem-provisioner-api -n exomem-platform --timeout=180s
kubectl wait --for=condition=Available deployment/exomem-provisioner-worker -n exomem-platform --timeout=180s
kubectl wait --for=condition=Available deployment/exomem-volume-worker -n exomem-platform --timeout=180s
kubectl -n exomem-platform get service/exomem-provisioner -o jsonpath='{.spec.ports[0].port}{"\n"}'
kubectl -n exomem-platform get configmap/exomem-hosted-release-v1 \
  -o jsonpath='{.data.exomem-hosted-release-v1\.json}' | jq -e \
  '.artifact == "exomem-hosted-release" and .schemaVersion == 1 and (.commandRegistry | length) == 21'
kubectl get storageclass exomem-hcloud-encrypted-retain
kubectl -n exomem-platform get configmap/exomem-capacity-contract \
  -o jsonpath='{.immutable}{"\n"}'
kubectl auth can-i create pods \
  --as=system:serviceaccount:exomem-platform:exomem-provisioner-api -n exomem-platform
```

The Service port must print `8080`; the final authorization command must print
`no`. Only the worker service account may mutate tenant resources.
