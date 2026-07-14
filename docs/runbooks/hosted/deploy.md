# Reviewed hosted deployment

## Preconditions

The application release manifest, real B2 locking proof, static-secret
ciphertexts, and owner-only invitation gate must be green. Production mutation
always uses a saved plan; a second plan is never computed during apply.

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

Apply static secrets before installing the immutable platform release:

```bash
: "${EXOMEM_HOSTED_IMAGE:?set an immutable ghcr.io/artexis10/exomem@sha256 digest}"
infra/scripts/apply_sops_secret.py --matrix infra/contracts/secret-destinations-v1.json \
  --destination k3s.cloudflared.active \
  --artifact infra/secrets/platform/cloudflared-token.v1.sops.json
infra/scripts/apply_sops_secret.py --matrix infra/contracts/secret-destinations-v1.json \
  --destination k3s.scheduler.active \
  --artifact infra/secrets/platform/hosted-scheduler.v1.sops.json
infra/scripts/apply_sops_secret.py --matrix infra/contracts/secret-destinations-v1.json \
  --destination k3s.hcloud-csi.active \
  --artifact infra/secrets/platform/hcloud-csi-token.v1.sops.json
infra/scripts/apply_sops_secret.py --matrix infra/contracts/secret-destinations-v1.json \
  --destination k3s.volume-encryption.active \
  --artifact infra/secrets/platform/volume-encryption.v1.sops.json
helm upgrade --install exomem-platform infra/helm/platform --namespace exomem-platform \
  --create-namespace --values infra/helm/platform/values.yaml \
  --set-string runtime.image="${EXOMEM_HOSTED_IMAGE}" --atomic --wait --timeout 10m
```

## Verify

```bash
kubectl wait --for=condition=Available deployment/exomem-cloudflared -n exomem-platform --timeout=180s
kubectl get storageclass exomem-hcloud-encrypted-retain
kubectl auth can-i create pods --as=system:serviceaccount:exomem-system:exomem-provisioner -n exomem-platform
```

The last command must print `no`.
