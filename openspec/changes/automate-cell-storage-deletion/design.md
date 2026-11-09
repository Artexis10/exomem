## Context

The deletion path today, from `origin/main`:

- **Trust root.** Only Substrate writes a cell's `desired_state`. `cellctl` writes only observed columns: `write_observed` in `infra/cellctl/src/cellctl/db.py` refuses any other column.
- **Keys first.** For a deleted cell, `cellctl` retires the content keys before any other step (`retire_content_keys` in the same file). It then removes the namespace, reports the cell `deleted` and raises `cell-storage-cleanup-pending-<cell_id>`. That alert never resolves itself.
- **Manual cleanup.** The operator runs `infra/scripts/cell_storage_cleanup.py` once per account (`docs/runbooks/cloud-key-custody.md`):
  - `founder` deletes the key and every version in the legacy backup bucket, for a cell created before the key custodian;
  - `cell-data` deletes the cell's key, and hides its current backup files 7 days after the operator's first dry run;
  - `media`, after the same grace, cancels unfinished large files and deletes every version in the media bucket.
- **Custodian.** The key custodian (`infra/cellctl/src/cellctl/custodian.py`) creates one key for each cell namespace. It deletes no key of a deleted cell.
- **Edge.** The media edge's key holds `readFiles` and `writeFiles` (`infra/terraform/durability/media.tf`). The edge hides a name only for `condemn`, through a durable `hide` intent, with one write per name at a time.
- **Lifecycle.** The locked backup bucket's rule deletes a hidden version one day after the hide, once its lock lapses (`infra/terraform/cell-data/storage.tf`). The media bucket's rule deletes a hidden version after the media grace N, `cloud_media_condemned_days`.
- **Watcher.** The key watcher runs hourly in CI (`infra/scripts/b2_key_watcher.py`). It lists keys and the media bucket's versions, and it reports with its own receiver capability. The receiver accepts that capability only for the job `b2-key-watcher`, and it alerts when a run is missed.

This change uses these names:
- the **deletion grace G**, from a cell's deletion to its `purge_after`;
- the **media grace N**, the media bucket's days from a hide to a deletion;
- the **backup retention R**, the locked backup bucket's compliance retention, `backup_retention_days`;
- the **legacy backup bucket**, the old backup bucket in the founder account;
- the **executors**, the key custodian and the media edge, which act on a deletion grant.

Provider behaviour in this document marked † is not yet confirmed on the provider. Task 1 confirms each such point on a disposable bucket and records the result here.

## Goals / Non-Goals

**Goals**
- The platform deletes a deleted cell's backup and media files without an operator step, within a bounded time.
- This change adds no key that deletes file versions or creates keys in the founder account, in the cluster or in CI.
- A compromised `cellctl` can delay a deletion. It cannot cause one or make one happen sooner.
- The operator receives an alert at each state change of each deletion, and an anomaly alert when a deletion stalls or a hide comes early. No deletion alert depends on `cellctl`.
- The operator can hold a wrong deletion during the grace, and can rescue files for a time after the hides.

**Non-Goals**
- Automating the legacy backup bucket (D6).
- Changing content-key retirement, the backup lock or media collection.
- A compromised Substrate. It holds the grant key, so it can sign a grant for any cell. This is a stated limit, as in `contain-cloud-controller-compromise`.

## Decisions

### D1. Substrate signs a deletion grant

When Substrate writes `desired_state = 'deleted'` for a cell, it signs a deletion grant with its grant key. It stores the grant in a new column, `deletion_grant`, of `exomem_cloud_cells`. `cellctl`'s role can read the column and cannot write it. Substrate's existing grant signer returns nothing while the grant key is absent, so nothing purges before the key exists.

`contain-cloud-controller-compromise` D9 calls this object a signed deletion record. This change calls it the deletion grant, because `make-cloud-cells-operator-resistant` already uses "deletion record" for the record that states `keys_expire_at`.

The grant is an Ed25519 JWT with exactly these claims:
- `aud`: `exomem-cell-delete`;
- `iat`, `exp` and `jti`;
- `cell_id`;
- `purge_after`: an integer time, `iat` plus G.

**Rules.**
- G lives once, in Substrate's configuration. Its value is 7 days.
- The grant lifetime, `exp - iat`, is 30 days.
- An executor verifies a grant with `cloud_origin.signed_claims`. That function checks the type, key, audience, exact claim set, integer `iat` and `exp`, lifetime and cell ID. The executor then refuses a grant whose `purge_after` is less than `iat` plus one day, so a bad configuration cannot remove the grace.
- An executor takes the cell ID only from a verified grant.

**Relay.** `cellctl` relays each unexpired grant into one ConfigMap, `exomem-cell-deletions`, in its own namespace. Admission lets `cellctl` write only that ConfigMap name and its one key, as one more exception to admission rule 1. The custodian gains `get` on that one ConfigMap. The edge already runs in `cellctl`'s namespace, and it mounts the ConfigMap's key. `cellctl` can withhold a grant, which delays a deletion. It cannot forge a grant, because it does not hold the grant key.

**Backfill.** Substrate signs a grant for each cell that is already deleted. A backfilled grant starts a full grace. The executors find nothing to do for a cell that the script already cleaned.

**Finality.** Substrate never reverses a cell's deletion. A restore of a deleted cell's data goes into a new cell ID. An executor therefore needs no path that cancels a grant.

### D2. The key custodian purges the backup prefix

The backups live in the locked backup bucket of the cell-data account. The custodian already holds the only key there that creates keys.

1. **Delete the cell's key.** For a valid grant, the custodian reads the cell's namespace with a direct GET, not from a cache. When the GET returns 404, it deletes each key that the provider's key listing shows for the prefix `cells/<cell_id>/`. It takes key IDs only from that listing. The cell key's secret lived only in the namespace's Secret, so nothing can use the key any more.
2. **Mint a purge key.** At or after `purge_after`, the custodian mints a purge key. The key has the backup bucket, the name prefix `cells/<cell_id>/`, exactly `listFiles` and `writeFiles`, and a short `validDurationInSeconds`.
3. **Hide.** With the purge key, the custodian hides every current file name under the prefix. It lists again until no current name remains. Then it deletes the purge key.

**Rules.**
- The custodian never mints a key for a cell without a valid grant.
- It keeps at most one key per prefix. Before it mints a purge key, it deletes each key listed for the prefix, such as a stale purge key from a pass that crashed.
- Every step is idempotent. A rerun repeats the listings and changes nothing that is already done.
- It skips a cell on the hold list (D5).
- While a valid grant names a cell, the custodian creates no cell key for that cell, even when its namespace appears again.

† A key limited to the bucket and the prefix, with only `listFiles` and `writeFiles`, hides a current version in the locked bucket.

**Deletion.** The lifecycle rule (`days_from_hiding_to_deleting = 1`) deletes each hidden version once its lock lapses. The lock extender extends only current versions, to the current day plus R. So a hidden version's lock ends at most R days after the hide. The provider therefore deletes the cell's backup versions within about R plus 1 days of the hide. R is 30 days by default, and the variable allows 1 to 60.

† The lifecycle rule deletes a hidden version in the locked bucket only after its lock lapses, and deletes it then.

### D3. The media edge purges the media prefix

The media bucket lives in the founder account. The edge's key already holds `writeFiles`, which permits a hide. It holds no `listFiles`, so the edge cannot find a cell's names.

- **A list-only key.** Terraform adds `<prefix>-media-edge-list`, with only `listFiles` on the media bucket. Only the edge holds it. The edge keeps its existing key, because the provider cannot change a key in place, and a swap would open an outage window.
- **An internal pass.** The purge is a pass inside the edge. No caller can start it, and the edge gains no HTTP operation.
- **Conditions.** The edge purges a cell only when all of these hold:
  - the grant verifies against the grant public key from a chart value, not from a value that `cellctl` renders;
  - the loaded active set lists the cell in neither `cells` nor `collectors`;
  - `pending_by_cell` holds no write intent for the cell;
  - the current time is at or after `purge_after`;
  - the hold list does not name the cell.
- **Hides.** For each current name under `cells/<cell_id>/`, the edge persists a `hide` intent and then hides the name, under the one-write-per-name rule. A restart resumes from the persisted intents and a new listing. The edge lists again until no current name remains.
- **Unfinished large files.** The bucket's lifecycle rule already cancels them after `cloud_media_unfinished_upload_days`, which is 2 days by default. A revoked cell cannot start a new upload.
- **Deletion.** The bucket's rule deletes each hidden version after the media grace N. N is 14 days, decided on 2026-11-09. The operator must first apply that rule to the bucket (`collect-released-media`, rollout step 5). That change's task 1 confirms the provider behaviour.
- **Turning the edge off.** The chart refuses to turn the edge off while `exomem-cell-deletions` lists any grant. Otherwise a turn-off would leave a deleted cell's media current with nothing to hide it.

The collector credential of `collect-released-media` cannot do this job. Its pass hides only the names that the cell's manifest releases, and `cellctl` removes the cell from `collectors` at revocation.

### D4. The key watcher witnesses every deletion and raises every alert

The watcher runs in CI, outside the cluster, with its own list keys and its own receiver capability. The receiver alerts on a missed run. No deletion alert comes from `cellctl`, the custodian or the edge.

- **The deletion set.** A new Substrate endpoint returns each cell whose grant Substrate signed within the watch window W, with its `iat` and `purge_after`. The endpoint accepts only the watcher's receiver capability. The watcher never takes the set from `cellctl`.
- **The watch window.** W is a Substrate configuration value apart from the grant lifetime. It must exceed G plus max(N, R) plus 2 days, so that the watcher still sees a cell when its last anomaly bound passes.
- **A list key on the backup bucket.** Terraform adds a watcher key with only `listFiles` on the backup bucket of the cell-data account. With it and the existing media list key, the watcher lists each deleting cell's prefix in both buckets.
- **Values.** The watcher reads R and N from its pinned set, which already holds the backup bucket's default retention and the media bucket's lifecycle rule.

**Per-cell states.** On each run, the watcher derives each deleting cell's state again, from both buckets' listings, the key listing and the clock. A current file is a name whose newest entry is an `upload`. Each alert name carries the cell ID, which the receiver's name format `[A-Za-z0-9_.:-]{1,64}` allows.

| State | Condition | Alerts |
|---|---|---|
| Pending | The grace runs, or a current file or a key for the prefix remains | `CELL_DELETION_PENDING:<cell_id>` fires |
| Hidden | No current file in either bucket and no key for the prefix, while an `upload` or `start` entry remains | PENDING resolves, and `CELL_DELETION_HIDDEN:<cell_id>` fires |
| Confirmed | No `upload` and no `start` entry in either bucket | HIDDEN resolves |

`CELL_DELETION_ANOMALY:<cell_id>` fires while any of these holds:
- a current file or a key for the prefix remains after `purge_after` plus 1 day;
- an `upload` or `start` entry remains after `purge_after` plus max(N, R) plus 2 days;
- a hide marker under the prefix carries a time after `iat` and before `purge_after`.

The live cell's own restic prunes and media collection leave hide markers older than `iat`, so the last condition ignores them.

**Account-level alerts.**
- `MEDIA_HIDE_BURST` counts only the hides on prefixes of cells outside the deletion set. The watcher already lists the media bucket's versions on each run, and it groups the hides by `cells/<cell_id>/`.
- `PURGE_KEY_UNEXPECTED` fires for a key of the purge shape whose cell is outside the deletion set.
- `DELETIONS_UNKNOWN` fires when the watcher cannot read the deletion set. The watcher then treats every cell as outside the set: it counts every hide and reports every purge key.

**Pinned set.** The watcher accepts the purge shape as a second per-cell key shape, only for a cell in the deletion set. The pinned set gains the edge list key, the watcher's backup list key, the purge shape and the deletion watch.

**Public log.** CI logs are public. The watcher's log prints counts only and never a cell ID. The alert names that carry a cell ID go only to the receiver.

`cellctl`'s alert `cell-storage-cleanup-pending` stays only for a cell with data in the legacy backup bucket (D6).

### D5. Hold and rescue

- **Hold.** During the grace, the operator stops a wrong deletion by adding the cell ID to the chart value `cellDeletion.holdCells`, the hold list. The value is admin tier, so `cellctl` cannot change it. Both executors skip a held cell. The watcher's anomaly alert fires after `purge_after` plus 1 day and keeps reminding the operator. A hold keeps the files current, so the operator can restore them into a new cell (D1).
- **Media rescue.** Within N days of a hide, the operator deletes the hide markers, and each version becomes current again. This is the existing procedure in `docs/runbooks/cloud-media-edge.md`, "Rescue a hidden version". The cleanup script gains a `--rescue` mode that deletes only `hide` entries under the cell's prefix, never an `upload` entry.
- **Backup rescue.** If the provider lets a key delete a hide marker in the locked bucket, `--rescue` works there too. Otherwise the operator copies the hidden versions to a new cell's prefix before their locks lapse.
- **Readable content.** `cellctl` retires the content keys before any purge, so a rescued file stays ciphertext. The retired keys come back only from a control-database backup within its retention, about 31 days, together with the platform master key. A cell with a cell-held backup password also needs its escrow copy (`contain-cloud-controller-compromise` D5), which the purge hides with the other files.

† The provider does or does not let a key delete a hide marker in the locked bucket.

### D6. The legacy backup bucket stays manual

A cell created before the key custodian keeps its backups in the legacy backup bucket. Their deletion needs `deleteFiles` and key operations in the founder account, and this change adds neither. So:
- `cellctl` raises `cell-storage-cleanup-pending-<cell_id>` only for such a cell;
- the operator keeps the script's `founder` mode for it;
- the custodian and the edge still purge that cell's files in the cell-data account and the media bucket.

This path ends when the last such cell migrates (`contain-cloud-controller-compromise` task 6.2 and `lock-cloud-backups-against-deletion` task 3.1).

## Rejected options

- **A per-prefix lifecycle rule for the deleted cell.** It needs `writeBuckets`, which can also remove the bucket's default retention (`lock-cloud-backups-against-deletion` D3).
- **Restic `forget` and `prune`.** They leave the repository's config, its key files and the escrow copy, and the cell's restic password is already retired.
- **Leaving the files current.** A current version never expires, and the lock extender keeps extending its lock.
- **A purge from CI.** CI would become a second holder of `writeFiles` in the founder account, and the witness would also be the executor.
- **Acting on `cellctl`'s word,** such as its alert or its observed state. A compromised `cellctl` could then get a live cell's data deleted.
- **A purge Job in the cell namespace.** `cellctl` writes Jobs there, so it would choose what the Job does.
- **Name lists that `cellctl` passes to an executor.** A ConfigMap holds at most 1 MiB, and `cellctl` would choose the names.
- **A new HTTP operation on the edge.** It adds a remote surface, and the purge needs no caller.
- **One edge key that also lists.** The provider changes no key in place, and a swap opens an outage window.
- **The collector credential.** Its pass follows the cell's manifest, and `cellctl` removes the cell from `collectors` at revocation.

## Risks / Trade-offs

- **A compromised Substrate can delete any cell after the grace.** It holds the grant key. The watcher's pending alert and the hold list give the operator G days to act. If Substrate also leaves the cell out of the deletion set, `MEDIA_HIDE_BURST` and `PURGE_KEY_UNEXPECTED` still fire.
- **A compromised custodian or node in the cell-data account** can already mint any key there (`contain-cloud-controller-compromise` D9). The purge adds no capability.
- **A compromised edge** can already hide any name in the media bucket (`collect-released-media` D4). The list key adds only names and sizes, which the edge already sees as it writes them.
- **Edge downtime delays the media purge.** The chart refuses a turn-off while the ConfigMap lists a grant. A deleted cell without media also blocks a turn-off, until its grant expires.
- **The anomaly bound for versions has no spare margin.** It allows about one day of provider delay after the due day. Task 1 measures that delay. If the delay is longer, raise the margin before the bound fires falsely.
- **A deleted cell's backups stay about G plus R plus 1 days.** With G = 7 and R = 30, the provider confirms the deletion after about 38 days. The compliance lock requires this.

**What would change this design.**
- If the lifecycle rule ignores the lock, the custodian mints a purge key with `deleteFiles` after the lock lapses. The cell-data account allows that key.
- If Substrate cannot add the endpoint, the grant goes into each prefix's file info. The watcher then loses stall detection.
- If the edge stays off for long periods, the media purge moves back to the script.
- If the provider refuses to delete a hide marker in the locked bucket, and a copy-out is not acceptable, G grows.

## Rollout

1. Run task 1 on a disposable bucket, and record each result in this document.
2. Apply the two Terraform keys from a saved plan.
3. Update `KEY_WATCHER_PINNED_SET` by read-modify-write of its live value, after its owner agrees.
4. Ship Substrate's grant column, signer and endpoint. Without the grant key, it signs nothing.
5. Ship the watcher before the executors, so that it witnesses the first purge.
6. Deploy `cellctl`, the custodian, the edge and the chart in one release, with an empty hold list.
7. Rehearse on a disposable cell (task 10.1). The implementation merges once task 10.1 passes.
8. Keep this change active until task 10.2 confirms the provider's deletion.

Expected result: the operator receives each alert of the rehearsal cell, and both buckets list no `upload` and no `start` entry under its prefix.

## Open values

- **Deletion grace G.** 7 days, the value that the current procedure uses. It lives in Substrate's configuration.
- **Media grace N.** 14 days, decided on 2026-11-09.
- **Backup retention R.** The production value lives in the deployment's private variables. The default is 30 days.
- **Grant lifetime.** 30 days, a reversible choice made on 2026-11-09.
- **Watch window W.** Proposed 90 days. It must exceed G plus max(N, R) plus 2 days, which is 69 days at the largest allowed R.
- **Purge key validity.** Proposed one hour. It must cover one hide pass of the largest cell, and a rerun replaces an expired key.
- **Sandbox authority for task 1.** The operator authorizes the disposable bucket and its sandbox credentials.
- **Substrate's grant key.** Whether it is live (`contain-cloud-controller-compromise` task 3.3). Until it is, nothing purges, and the manual path stays.
- **Cell counts.** The number of live cells with data in the legacy backup bucket, and the number of deleted cells that await cleanup.
- **Lock extender.** Whether it is live (`lock-cloud-backups-against-deletion` task 2.3). The anomaly bound for versions assumes that locks end at most R days after a hide.
- **The pinned set.** The read-modify-write of `KEY_WATCHER_PINNED_SET` for the edge list key, the watcher's backup list key, the purge shape and the deletion watch. `contain-cloud-controller-compromise` owns the watcher's pinned set, so its owner agrees first.
