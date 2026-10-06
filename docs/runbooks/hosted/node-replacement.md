<!-- authority:non-specification -->

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
fleet_vars_text="$(infra/scripts/active_ansible_vars.py hosted-node)"
mapfile -t fleet_vars <<< "${fleet_vars_text}"
infra/scripts/verify_ansible_convergence.py --inventory infra/ansible/inventory.yml "${fleet_vars[@]}"
kubectl get nodes,pv
```

All acknowledged writes must be present and cells ready inside the recorded
60-minute node-replacement RTO.
