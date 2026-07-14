# Reviewed hosted deployment

## Preconditions

The complete application release manifest, real B2 locking proof, static-secret
ciphertexts, and owner-only invitation gate must be green. Production mutation
always uses a saved plan; a second plan is never computed during apply. The
release manifest is the reviewed `infra/contracts/exomem-hosted-release-v1.json`
file with the exact `exomem-hosted-release`
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

```bash
release_manifest=infra/contracts/exomem-hosted-release-v1.json
: "${EXOMEM_PROVISIONER_IMAGE:?set the reviewed ghcr.io/artexis10/exomem-provisioner@sha256 digest}"
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
helm template exomem-platform infra/helm/platform --namespace exomem-platform \
  --values infra/helm/platform/values.yaml \
  --values "${deploy_work_dir}/release-values.json" \
  --show-only templates/namespaces.yaml | kubectl apply -f -
```

Apply every declared active K3s ciphertext before installing the immutable
platform release. This includes platform and provisioner API/worker inputs; a
missing artifact stops deployment before Helm runs.

```bash
while IFS=$'\t' read -r destination target; do
  artifact="${target/\{version\}/v1}"
  test -f "$artifact"
  infra/scripts/apply_sops_secret.py \
    --matrix infra/contracts/secret-destinations-v1.json \
    --destination "$destination" \
    --artifact "$artifact"
done < <(
  jq -r '.secrets | to_entries[] | .value.destinations | to_entries[] |
    select(.key | startswith("k3s.")) | [.key, .value.target] | @tsv' \
    infra/contracts/secret-destinations-v1.json
)

helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform \
  --values infra/helm/platform/values.yaml \
  --values "${deploy_work_dir}/release-values.json" --atomic --wait --timeout 10m
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
