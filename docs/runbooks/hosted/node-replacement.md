# Node resize or replacement

## Preconditions

Use replacement only after a saved plan is reviewed and the retained-volume
registry, external database, static SOPS set, and latest verified backups are
available. The exact replacement address requires `--allow-destructive`.

```bash
export TF_CLOUD_ORGANIZATION=replace-with-approved-org
export TF_TOKEN_app_terraform_io=read-from-secret-manager
infra/scripts/plan.sh foundation /run/user/$UID/foundation-replacement.tfplan
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
