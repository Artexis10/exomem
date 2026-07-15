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
helm template exomem-platform infra/helm/platform --namespace exomem-platform \
  --values infra/helm/platform/values.yaml \
  --values "${deploy_work_dir}/release-values.json" \
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
and deletion workers; the vault-backup reconciler mounts it alongside the signer
to authenticate discovered provider objects and prove both halves share one
trust root. Generate and hand off both halves as one atomic keypair operation, then
include both exact ciphertext artifacts in the signed active-secret registry.
Never place both halves in one Secret or mount the signing seed into the routine
or deletion worker. The volume worker derives its verifier from this same seed;
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
the same atomic boundary. Their private seeds route only to the named collector;
their public verifiers route to operator escrow:

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

helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform \
  --values infra/helm/platform/values.yaml \
  --values "${deploy_work_dir}/release-values.json" \
  --values "$durability_values" --atomic --wait --timeout 10m
```

## Verify

```bash
kubectl wait --for=condition=Available deployment/exomem-cloudflared -n exomem-platform --timeout=180s
kubectl wait --for=condition=Available deployment/exomem-provisioner-api -n exomem-platform --timeout=180s
kubectl wait --for=condition=Available deployment/exomem-provisioner-worker -n exomem-platform --timeout=180s
kubectl -n exomem-platform get service/exomem-provisioner -o jsonpath='{.spec.ports[0].port}{"\n"}'
kubectl -n exomem-platform get configmap/exomem-hosted-release-v1 \
  -o jsonpath='{.data.exomem-hosted-release-v1\.json}' | jq -e \
  '.artifact == "exomem-hosted-release" and .schemaVersion == 1 and (.commandRegistry | length) == 21'
kubectl get storageclass exomem-hcloud-encrypted-retain
kubectl auth can-i create pods \
  --as=system:serviceaccount:exomem-platform:exomem-provisioner-api -n exomem-platform
```

The Service port must print `8080`; the final authorization command must print
`no`. Only the worker service account may mutate tenant resources.
