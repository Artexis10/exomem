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

## Change the CPU architecture

Every release publishes the Exomem Cloud cell and cellctl images for
`linux/amd64` and `linux/arm64` under one digest, so the same release runs on
x86 and Arm64 nodes. One cluster runs one architecture: the fleet server and
every agent are all x86, or all Arm64 (a Hetzner CAX server). Mixed clusters
are not supported, and Arm64 agent types are not qualified yet, so an Arm64
cluster runs on its fleet server alone.

A move to the other architecture replaces every node, as above. Then rebuild
each cell's vector index on the new architecture before you accept the cell.
The same chunk's bge-m3 int8 vectors differ between architectures (cosine
0.9685–0.987 between a cx33 and a cax21, 1.000000 on either alone, measured
2026-10-10). The vector index records its model, not the architecture, so no
check rebuilds it, and new queries would rank against the old vectors.

For each cell:

1. Stop the cell and start the helper pod, as
   [import steps 5 and 6](../cloud-operator-import.md#5-stop-the-cell) do.
   Raise the helper's memory limit to the cell's `3Gi`: the rebuild loads the
   recall model.
2. Rebuild the vector index with the full rebuild of `maintain_memory`. The
   `--no-dry-run` flag is required: without it, fix mode only reports.

   ```bash
   helper env EXOMEM_CLOUD_CELL=1 EXOMEM_CLOUD_CELL_ID="$CELL_ID" EXOMEM_VAULT_PATH=/data/vault exomem maintain_memory --mode fix --no-dry-run --rebuild-embeddings --json
   ```

   Fix mode also applies its safe content fixes, such as canonical wikilinks,
   and lists the rest under `proposed`. Expect `"success": true`,
   `"dry_run": false` and a nonzero `summary.embeddings_chunks`.
3. Delete the helper pod and start the cell, as
   [import step 7](../cloud-operator-import.md#7-start-the-cell) does.
4. Accept the cell with recall, governance status and a governed write.

The command ran against the cloud image on a test vault (2026-10-10). No real
cell has moved between architectures yet.
