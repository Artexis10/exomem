<!-- authority:non-specification -->

# Exomem Cloud cell deletion

Each destructive live operation requires explicit operator approval.

## Preconditions

Record the exact cell and current provider volume and B2 key IDs privately.
Obtain the tenant data-disposal authority before the product deletion action.

## Delete a cell

For a plain Cloud cell managed by `cellctl`, use the product's deletion action
to set its row's `desired_state` to `deleted`.

Confirm completion in this order:

1. Confirm that `content_keys_retired_at` is set and the database root and backup wraps are absent.
2. Confirm that the child Secret and every cell workload are absent, including terminating Job Pods.
3. Confirm that the cell namespace is absent.
4. No PersistentVolume claims that namespace, and the recorded Hetzner volume
   is absent. Namespace deletion alone is not proof of volume deletion.
5. For a cell with a stored B2 key ID (made before the cell-data account), every B2 object
   version under the exact `cells/<cell_id>/` prefix is absent, including hidden versions.
   An empty current-object listing is insufficient. A newer cell's backups sit in the locked
   cell-data bucket: `cellctl` deletes nothing there and only lists them.
6. The row reports `observed_state = deleted`, with any stored B2 key ID and wrapped
   B2 and backup key material cleared.
7. `cellctl` raises the platform alert `cell-storage-cleanup-pending-<cell_id>`. It holds no
   credential that deletes keys, so the operator deletes the cell's object-store key. Follow
   [Cloud key custody](cloud-key-custody.md#clean-up-after-a-cell-deletion).

The controller retries incomplete steps. A failed provider observation is not
proof of absence; leave the cell deleting and resolve that observation failure.
Do not manually clear key columns, remove finalizers, or delete sibling prefixes
to force completion.

Database retirement runs before Kubernetes admission, capacity and provider reads.
The controller deletes `cell-credentials` independently of provider cleanup.
It keeps the separately wrapped B2 credential until object cleanup finishes.

`last_live_key_destroyed_at` starts only after live Secret and workload absence is proven.
Legacy shared keys and retained old volumes keep complete destruction unknown.
Do not delete a fleet's shared Secret for one cell.

`key_retention_sources` records each source, retention window and observation time from deployed policy.
`keys_expire_at` stays unknown if any required window or absence proof is missing.
Past deleted rows receive no invented destruction date.

### Retained etcd snapshots

K3s encrypts Secrets at rest. Existing etcd snapshots can still retain an
encrypted copy of a deleted cell's Secret until snapshot retention expires.
The controller's `deleted` state proves the live-resource and key-removal checks
above; it does not prove that every historical snapshot has expired.

Record the applicable retention bound from the deployed local and remote
snapshot policies, including storage lifecycle or retention settings, when
reporting deletion. Do not infer a time bound from a snapshot count alone or
claim immediate erasure of snapshot copies. Handle snapshot restoration as a
separate operator recovery: keep the restored cluster quarantined, check its
cell resources against the authoritative control database, and separately
remove and verify the absence of resources resurrected for deleted cells before
reopening ingress. `cellctl` skips rows already observed as deleted; waiting for
its ordinary reconciliation does not perform this recovery cleanup. Do not
prune shared cluster snapshots as part of one cell's deletion.

## Verify

```bash
kubectl get namespaces --selector=exomem.io/cloud-cell=true
```

Use the exact private cell identity to verify the absence of its namespace and provider resources.
A namespace listing alone does not prove complete deletion; retain every absence proof listed above.
