# Node resize or replacement

## Preconditions

Use replacement only after a saved plan is reviewed and the retained-volume
registry, external database, static SOPS set, and latest verified backups are
available. The exact replacement address requires `--allow-destructive`.

```bash
terraform -chdir=infra/terraform/foundation init -input=false -backend-config="$TF_BACKEND_CONFIG_FILE"
terraform -chdir=infra/terraform/foundation plan -input=false -out=/run/user/$UID/foundation-replacement.tfplan
chmod 0600 /run/user/$UID/foundation-replacement.tfplan
terraform -chdir=infra/terraform/foundation show -json /run/user/$UID/foundation-replacement.tfplan > /run/user/$UID/foundation-replacement.plan.json
chmod 0600 /run/user/$UID/foundation-replacement.plan.json
infra/scripts/inspect_terraform_plan.py /run/user/$UID/foundation-replacement.plan.json \
  --allow-destructive hcloud_server.alpha
infra/scripts/apply_saved_plan.sh foundation /run/user/$UID/foundation-replacement.tfplan \
  --allow-destructive hcloud_server.alpha
```

Apply the saved plan, regenerate inventory, converge Ansible twice, apply SOPS
static secrets, reinstall Helm, then run provider-fence rediscovery before any
lifecycle mutation.

## Verify

```bash
infra/scripts/verify_ansible_convergence.py --inventory infra/ansible/inventory.yml \
  --vars infra/secrets/ansible/k3s-server-token.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-access-key.v1.sops.json \
  --vars infra/secrets/ansible/etcd-s3-secret-key.v1.sops.json
kubectl get nodes,pv
```

All acknowledged writes must be present and cells ready inside the recorded
60-minute node-replacement RTO.
