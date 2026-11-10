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
5. The staging bucket holds no version, delete marker or unfinished upload under the exact
   `cells/<cell_id>/` prefix. `cellctl` lists that prefix with its staging key, deletes what it
   finds, and lists it again before it goes on.
6. For a cell with a stored B2 key ID (made before the cell-data account), every B2 object
   version under the exact `cells/<cell_id>/` prefix is absent, including hidden versions.
   An empty current-object listing is insufficient. A newer cell's backups sit in the locked
   cell-data bucket: `cellctl` deletes nothing there and only lists them.
7. The row reports `observed_state = deleted`, with its wrapped B2 and backup key material cleared.
   A cell with a stored B2 key ID keeps that ID, as the mark of its clean-up in the old bucket.
8. The key custodian and the media edge purge the cell's prefixes in the locked backup bucket and
   the media bucket after the grant's `purge_after`. The key watcher reports each step. The operator
   runs no clean-up there: see "Purge a deleted cell's storage" below. Only a cell with a stored B2
   key ID raises the platform alert `cell-storage-cleanup-pending-<cell_id>`. For that cell, follow
   [Cloud key custody](cloud-key-custody.md#clean-up-after-a-cell-deletion) for the old bucket.

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

### Retained access log

The access log and its recovery code are prepared features, disabled until their operational checks pass.
Their availability does not change the deletion checks above.

When enabled, the authenticated owner can obtain a log-only recovery code before deletion.
The service stores its digest and the immutable old owner, tenant and cell scope.
It retains no recovery contact or address fingerprint.
The code opens only the retained log; it cannot restore Cloud, MCP, billing, provisioning or OAuth access.
Re-admission at the erased address creates no authority over the old log.

1. Offer the recovery code before deletion; the owner can save the code privately.
2. Explain that email cannot recover a lost code; the owner understands the recovery limit.
3. Complete deletion whether the code is saved or not; no save confirmation blocks erasure.
4. Keep received audit evidence for its configured retention; product sessions and content keys remain revoked.

Use `/exomem/access-log` to exchange the code through its browser challenge for a separate log-only session.
Expiry or revocation of the log grant invalidates that session.
Do not reconstruct access for an already-erased owner from another product's contact information.
The view lists received events and gaps; it does not claim a complete record of node activity.

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
kubectl get namespaces --selector=exomem.io/cloud-cell
```

Use the exact private cell identity to verify the absence of its namespace and provider resources.
A namespace listing alone does not prove complete deletion; retain every absence proof listed above.

## Purge a deleted cell's storage

At the deletion, Substrate signs a deletion grant that names the cell and its `purge_after`, the end of
the 7-day grace. `cellctl` relays each grant into the ConfigMap `exomem-cell-deletions` in `exomem-cloud`.
It drops a grant only when its list-only views of both buckets show no stored version under the cell's
prefix. Two executors act on a grant:

- The key custodian deletes the cell's key in the cell-data account once the cell's namespace is gone.
  After `purge_after`, it hides each current backup file with a purge key that lives one hour. The bucket's
  lock keeps each hidden version until its retention ends, and the provider then deletes it.
- The media edge hides each current name under the cell's prefix after `purge_after`, with its list-only
  key. The media bucket's lifecycle rule deletes each hidden version when the media grace ends.

Each executor verifies a grant against the chart value `cellDeletion.grantPublicKeys`, and skips each cell
in `cellDeletion.holdCells`, the hold list. The chart puts both values into the ConfigMap
`exomem-cell-deletion-policy`, in `exomem-key-custody` and in `exomem-cloud`. Each executor reads that
ConfigMap again on each pass, so a change restarts neither. An empty `grantPublicKeys` verifies no grant,
so nothing is purged. An unreadable hold list stops every purge.

### Create the grant signing key

Substrate signs each deletion grant with `EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY`, an Ed25519 private key in PEM.
It is a key apart from the operation-grant key. BWS prod holds it as `SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY`.
Do this once, with the operator's approval and a prod grant. The private key goes only through pipes.

1. Make the key pair and store the private half in BWS prod:
   `openssl genpkey -algorithm ed25519 | bwsx-secret set --project prod SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY --format opaque --if-absent`
2. In the Substrate checkout linked to its Vercel project, pipe the key into the Production environment only:
   `bwsx-run SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY -- sh -c 'printf "%s\n" "$SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY"' | vercel env add EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY production --sensitive`
3. Do not set the key in Preview or Development. Those builds run unreviewed code.
4. Redeploy Substrate's current production deployment: `vercel redeploy <production deployment URL>`
5. Read the deletion set with the key watcher's capability. The capability stays out of the command line:
   `bwsx-run EXOMEM_KEY_WATCHER_ALERT_URL -- sh -c 'printf "url = \"%s\"\n" "$EXOMEM_KEY_WATCHER_ALERT_URL"' | sed 's#/api/exomem/alerts/#/api/exomem/cell-deletions/#' | curl -sS --config - | jq .schema_version`
   Expect `1`. This proves the endpoint, not the key: with no valid key, the set is empty and still valid.

Expected result: the next cell that Substrate deletes appears in the deletion set, with its `iat` and `purge_after`.
If it does not, read Substrate's production log for `exomem_cloud_grant_key_invalid`.
Unset, a deletion still succeeds and stores no grant.

### Turn on the purge

Do this once. Agree each live change with its owner first, as the rollout of `automate-cell-storage-deletion` says.
Before you start, make sure that:

- the release that you deploy has `cellctl` and cell images with the purge code;
- the key custodian runs, as the cutover in [Cloud key custody](cloud-key-custody.md) says;
- the grant signing key exists, as "Create the grant signing key" says.

1. Pin the deletion watch, as "Pin the deletion watch" in [Cloud key custody](cloud-key-custody.md) says.
2. If the platform has the media edge, create its list-key Secret, as [Cloud media edge](cloud-media-edge.md) step 7 of "Create the Secrets" says.
3. Print the PEM public half of the grant signing key. Do not write the private key to a file:
   `bwsx-run SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY -- sh -c 'printf "%s\n" "$SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY"' | openssl pkey -pubout`
4. Set `cellDeletion.grantPublicKeys` in the private values file to a list with that public half. Do not use the operation-grant key.
5. Keep `cellDeletion.holdCells` empty.
6. Run the Helm upgrade in [Cloud platform deploy](cloud-platform-deploy.md#render-and-upgrade-the-release).
7. If the platform has the media edge, read its log: `kubectl -n exomem-cloud logs deployment/exomem-media-edge | grep "purge pass is off"`
   Expect no output.

Expected result: the next deleted cell raises `CELL_DELETION_PENDING:<cell_id>`, and then
`CELL_DELETION_HIDDEN:<cell_id>` after its `purge_after`.

### Alerts

The key watcher raises every deletion alert from CI, with its own list keys and receiver. Its log prints
counts only: `deletions: <n> in the set, <w> watched: <p> pending, <h> hidden, <c> confirmed, <a> anomalous`.
A cell ID goes only to the receiver, in the alert name.

| Alert | Meaning | Action |
| --- | --- | --- |
| `CELL_DELETION_PENDING:<cell_id>` | The grace runs, or a current file or a key for the cell's prefix remains | None. It resolves after the hides. |
| `CELL_DELETION_HIDDEN:<cell_id>` | No current file and no key remain, and hidden versions wait for the provider | None. It resolves when the provider deletes the last version. |
| `CELL_DELETION_ANOMALY:<cell_id>` | The purge stalls or a hide came early | Follow "Triage an anomaly". |
| `DELETIONS_UNKNOWN` | The watcher cannot read Substrate's deletion set. It then counts every media hide and reports every purge key. | Check the secret `KEY_WATCHER_DELETIONS_URL` and Substrate's endpoint. |
| `DELETION_LISTING_FAILURE` | A listing failed or reached the page cap, or the provider refused a key. No cell changes state. | Check the secrets `B2_WATCHER_BACKUP_LIST_KEY_ID`, `B2_WATCHER_BACKUP_LIST_KEY` and the media watch key. |
| `<account>:PURGE_KEY_UNEXPECTED` | A key of the purge shape exists for a cell outside the deletion set | Follow "Triage an unexpected purge key". |

Pending and hidden keep the watcher's run green. An anomaly fails it. When a cell's watch window ends,
the watcher sends nothing more for that cell, so resolve a remaining anomaly in the receiver.

`cellctl` raises `cell-storage-cleanup-pending-<cell_id>` only for a cell with a stored B2 key ID. That alert
covers the old bucket only, as step 8 of "Delete a cell" says.

### Hold a cell

A hold stops both executors for one cell. Use it during the grace to stop a wrong deletion. The files
stay current, but `cellctl` already retired the cell's content keys, so they are ciphertext.

1. Add the cell ID to `cellDeletion.holdCells` in the private values file.
2. Run the Helm upgrade in [Cloud platform deploy](cloud-platform-deploy.md#render-and-upgrade-the-release).
3. Read the hold list: `kubectl -n exomem-key-custody get configmap exomem-cell-deletion-policy -o jsonpath='{.data.hold-cells\.json}'`
4. If the media edge is on, run the same command with `-n exomem-cloud`.
5. Wait two minutes, so that the kubelet updates the file that each executor reads.

Expected result: each listing shows the cell ID. From `purge_after` plus 1 day, `CELL_DELETION_ANOMALY:<cell_id>`
fires and stays while the hold lasts. If the executors hid files before the hold, rescue them as below.

### Release a hold

1. Remove the cell ID from `cellDeletion.holdCells` in the private values file.
2. Run the Helm upgrade in [Cloud platform deploy](cloud-platform-deploy.md#render-and-upgrade-the-release).
3. Read the hold list in both namespaces, as steps 3 and 4 of "Hold a cell" say. Expect no cell ID.

Expected result: both executors purge the cell at their next pass, because a grant has no expiry. At the
watcher's next run after the hides, the anomaly resolves and `CELL_DELETION_HIDDEN:<cell_id>` fires.

A restore of a held cell into a new cell needs its retired content keys from a control-database backup.
This runbook does not cover that restore.

### Rescue hidden files

A rescue deletes the hide markers under a cell's prefix, so each hidden version becomes current again. It
works only before the provider deletes the versions. In the media bucket, that is the media grace after the
hide. In the locked backup bucket, it is one day after each version's lock ends. A rescued file is ciphertext.
For one media name, "Rescue a hidden version" in [Cloud media edge](cloud-media-edge.md) gives a manual path.

1. Hold the cell, as "Hold a cell" says. Otherwise the executors hide the files again at their next pass.
2. Export an operator key that is limited to the bucket, with `listFiles` and `deleteFiles` and no key capability.
   `export CELL_CLEANUP_B2_KEY_ID=<key id> CELL_CLEANUP_B2_APPLICATION_KEY=<application key>`
3. Run the dry run. It changes nothing and prints counts only.
   `infra/scripts/cell_storage_cleanup.py <cell_id> --account <media or cell-data> --bucket-id <bucket id>`
   Take the media bucket's ID from the `cloud_media_bucket_id` output of `infra/terraform/durability`.
   Take the locked bucket's ID from `infra/terraform/cell-data`. Expect `mode=dry-run` and `hides_found` above 0.
4. Run the same command with `--rescue`. Expect `mode=rescue` and `hides_deleted` equal to `hides_found`.
5. Run the dry run again. Expect `hides_found=0`, and `uploads_found` as in step 3.

Expected result: each name under the prefix has a current version again. Keep the hold: a release starts
the purge again.

The script deletes only `hide` entries, never an `upload` entry or an unfinished file. In the locked
bucket the provider may refuse to delete a hide marker. The run then stops with exit 1 and
`cleanup stopped`. Before the locks end, copy the hidden versions to a new cell's prefix instead. This
runbook does not cover that copy.

### Triage an anomaly

`CELL_DELETION_ANOMALY:<cell_id>` carries no reason. Do these checks in order, and stop at the first one
that explains the alert.

1. Read the hold list, as step 3 of "Hold a cell" says. If it names the cell, the alert is the hold's reminder.
2. Read the cell's `purge_after` from its relayed grant:
   `kubectl -n exomem-cloud get configmap exomem-cell-deletions -o jsonpath='{.data.grants\.json}' | jq -r '.[] | split(".")[1] | gsub("-";"+") | gsub("_";"/") | @base64d | fromjson | select(.cell_id == "<cell_id>") | .purge_after | todate'`
   If the output is empty, `cellctl` relays no grant for the cell. Read why in its log:
   `kubectl -n exomem-cloud logs deployment/cellctl --since=24h | grep -e "deletion grant" -e "exomem-cell-deletions"`
3. If the alert fired before `purge_after`, a hide came early. Hold the cell, rescue its files, then do step 8.
4. If the cell's row does not read `observed_state = deleted`, the cell has no stop time. Finish "Delete a cell" for it.
5. Count the grant keys in each namespace that runs an executor, `exomem-key-custody` and, with the media edge, `exomem-cloud`:
   `kubectl -n <namespace> get configmap exomem-cell-deletion-policy -o jsonpath='{.data.grant-public-keys\.json}' | jq length`
   If the count is 0, set `cellDeletion.grantPublicKeys`, as "Turn on the purge" says.
6. Read the custodian's log: `kubectl -n exomem-key-custody logs deployment/key-custodian --since=24h | grep -e "<cell_id>" -e "deletion grants" -e "hold list" -e "done marks"`
   `deletion grants: <n> relayed entries refused` means that no pinned key verifies a grant. A line for the cell
   names the provider error. No line for the cell means that its namespace `exo-cell-<cell_id>` still exists.
7. If the cell used the media store, read the edge's log: `kubectl -n exomem-cloud logs deployment/exomem-media-edge --since=24h | grep -e "purge" -e "deletion grants" -e "stay pending" -e "still unknown"`
   `the purge pass is off` means that the edge has no list key: create its Secret as [Cloud media edge](cloud-media-edge.md) says.
   `write intents stay pending` with the cell means that a write for it is still unknown, and the edge waits.
   Every 15 seconds, the edge asks the provider again and sends again a hide that did not land. If the line
   stays, the `is still unknown` lines for the cell name the provider error.
   No line for the cell means that the edge still admits it: read the active set as step 2 of
   "Turn the edge off" in that runbook says.
8. If no step explains the alert, treat it as a possible compromise of an executor or its key. Keep the
   cell held. Compare each executor's image with its reviewed digest, and list the hide markers with their
   times as "Rescue a hidden version" in [Cloud media edge](cloud-media-edge.md) says.

An anomaly that fires after the version bound means that a version outlived its lock or the media grace.
Check the backup bucket's retention and the media bucket's lifecycle rule against the pinned set.

### Triage an unexpected purge key

`<account>:PURGE_KEY_UNEXPECTED` means that a key with only `listFiles` and `writeFiles` on a cell's prefix
exists for a cell outside the deletion set. Only the key custodian creates such a key, and it names it
`purge-<cell_id>`. Treat the alert as a possible custodian compromise.

1. If `DELETIONS_UNKNOWN` fires too, the watcher could not read the set. Fix that first, then wait for the next run.
2. In the provider's console for the cell-data account, find the key with that shape, and note its prefix's cell ID.
3. Delete that key in the console.
4. Run the dry run of "Rescue hidden files" for that cell, with `--account cell-data`. If `hides_found` is above 0, rescue them.
5. Read the custodian's image and log, as step 6 of "Triage an anomaly" says.

Expected result: the alert resolves at the watcher's next run.

### Rotate the grant key

Each executor accepts a grant that any key in `cellDeletion.grantPublicKeys` verifies, and the list holds
at most two keys. A grant has no expiry and stays relayed until its purge ends. So pin the next key
before Substrate signs with it, and remove the old key only when no relayed grant needs it.

1. Make the next key pair as step 1 of "Create the grant signing key" says. Use the BWS name `SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY_NEXT`.
2. Print its public half as step 3 of "Turn on the purge" does, with the `_NEXT` name.
3. Add that public half as the second entry of `cellDeletion.grantPublicKeys`.
4. Run the Helm upgrade in [Cloud platform deploy](cloud-platform-deploy.md#render-and-upgrade-the-release).
5. Count the grant keys as step 5 of "Triage an anomaly" says. Expect 2 in each namespace.
6. Remove Substrate's current key: `vercel env rm EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY production --yes`
7. Pipe the `_NEXT` key into the same variable, as step 2 of "Create the grant signing key" does.
8. Redeploy Substrate as step 4 of "Create the grant signing key" says. Write down the switch time as Unix seconds.
9. Count the relayed grants that Substrate signed before the switch:
   `kubectl -n exomem-cloud get configmap exomem-cell-deletions -o jsonpath='{.data.grants\.json}' | jq '[.[] | split(".")[1] | gsub("-";"+") | gsub("_";"/") | @base64d | fromjson | select(.iat < <switch time>)] | length'`
10. If the count is above 0, wait and repeat step 9. A held cell keeps its grant, so release or resolve it first.
11. Remove the old key from `cellDeletion.grantPublicKeys`, and run the Helm upgrade again.
12. Copy the `_NEXT` key over the base BWS name, so that "Turn on the purge" step 3 prints the live key:
   `bwsx-run SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY_NEXT -- sh -c 'printf "%s\n" "$SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY_NEXT"' | bwsx-secret set --project prod SUBSTRATE_EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY --format opaque`
13. Delete the `_NEXT` entry in BWS.

Expected result: a repeat of step 5 shows 1 key in each namespace, and the watcher raises no anomaly.
