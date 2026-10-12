<!-- authority:non-specification -->

# Node resize or replacement

## Preconditions

Review a saved replacement plan and record the current external control rows, CSI volume IDs, static SOPS selection and verified backups privately.
Verify that each original CSI volume survives the planned host action. Local storage requires the separate [node-loss procedure](cloud-node-loss.md).
Record the cell identity, PVC/PV, volume handle, custody baseline, current image and active hold before changing a host.
Wait for active holds and release volume users through the ordinary cell stop procedure before planned movement.
An unplanned loss requires inspection of actual provider attachments before recovery; absence of the old Node does not prove volume release.

The exact replacement address requires `--allow-destructive` and explicit operator approval.
Prepare the plan outside the repository through the existing saved-plan path:

```bash
infra/scripts/plan.sh foundation "$EXOMEM_REPLACEMENT_PLAN"
infra/scripts/apply_saved_plan.sh foundation "$EXOMEM_REPLACEMENT_PLAN" --allow-destructive hcloud_server.alpha
```

1. Apply the reviewed saved plan, and retain its exact resource actions.
2. Regenerate the fleet inventory and converge Ansible twice, with the replacement node ready.
3. Apply the verified active SOPS registry and reviewed Helm inputs, with control workloads healthy.
4. Recover the original CSI binding under [the retained-volume procedure](cloud-node-loss.md), with the recorded volume handle unchanged.
5. Resume through the ordinary cell lifecycle, with authenticated readiness and matching canonical data and host custody.

If the original volume is unavailable, use the existing [B2 restore helper](cloud-operator-restore.md) instead.
Never recreate an empty PVC over a retained original or use the deleted Hosted provider-fence protocol.
An older etcd restore requires the quarantine and current-control-row checks in [cell deletion](cloud-cell-deletion.md).

## Verify

The retained `hosted-node` destination group contains the K3s and etcd variables used by the current Cloud fleet.
It is an active Ansible matrix group, not a Hosted runtime profile.

```bash
fleet_vars_text="$(infra/scripts/active_ansible_vars.py hosted-node)"
mapfile -t fleet_vars <<< "${fleet_vars_text}"
infra/scripts/verify_ansible_convergence.py --inventory "$EXOMEM_FLEET_INVENTORY" "${fleet_vars[@]}"
kubectl get nodes,pv
```

Verify the exact original provider volume and matching PVC/PV, then check route reads and a governed write.
Compare canonical vault data and `/data/host` custody and durable state against the recorded baseline.
Record backup age, downtime and cleanup. The node-loss target is 60 minutes when the original CSI volume survives.
A ready Pod and a volume attachment alone do not prove that target or data recovery.
