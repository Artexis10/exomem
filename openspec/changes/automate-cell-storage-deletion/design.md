## Context

The deletion path today, from `origin/main`:

- **Trust root.** Substrate owns a cell's `desired_state`. `write_observed` in `infra/cellctl/src/cellctl/db.py` limits `cellctl`'s writes to observed and media key columns. That limit is `cellctl`'s own code, and a compromised `cellctl` skips it. So this change rests on a database column grant instead (D1).
- **Keys first.** For a deleted cell, `cellctl` retires the content keys before any other step (`retire_content_keys` in the same file). It then removes the namespace, reports the cell `deleted` and raises `cell-storage-cleanup-pending-<cell_id>`. That alert never resolves itself.
- **Manual cleanup.** The operator runs `infra/scripts/cell_storage_cleanup.py` once per account (`docs/runbooks/cloud-key-custody.md`):
  - `founder` deletes the key and every version in the legacy backup bucket, for a cell created before the key custodian;
  - `cell-data` deletes the cell's key, and hides its current backup files 7 days after the operator's first dry run;
  - `media`, after the same grace, cancels unfinished large files and deletes every version in the media bucket.
- **Custodian.** Inside the cluster, the key custodian (`infra/cellctl/src/cellctl/custodian.py`) holds the only key that creates keys in the cell-data account. It creates one key for each cell namespace. It deletes no key of a deleted cell.
- **Edge.** The media edge's key holds `readFiles` and `writeFiles` (`infra/terraform/durability/media.tf`). The edge hides a name only for `condemn`, through a durable `hide` intent, with one write per name at a time.
- **Lifecycle.** The locked backup bucket's rule deletes a hidden version one day after the hide, once its lock lapses (`infra/terraform/cell-data/storage.tf`). The media bucket's rule deletes a hidden version after the media grace N, `cloud_media_condemned_days`.
- **Watcher.** The key watcher runs hourly in CI (`infra/scripts/b2_key_watcher.py`). It lists keys and the media bucket's versions, and it reports with its own receiver capability. A check that could not run reports nothing, so a blind run resolves no alert. The receiver accepts that capability only for the job `b2-key-watcher`, and it alerts when a run is missed.

This change uses these names:
- the **deletion grace G**, from a cell's deletion to its `purge_after`;
- the **media grace N**, the media bucket's days from a hide to a deletion;
- the **backup retention R**, the locked backup bucket's compliance retention, `backup_retention_days`;
- the **legacy backup bucket**, the old backup bucket in the founder account;
- the **executors**, the key custodian and the media edge, which act on a deletion grant;
- the **stop time** of a deleted cell, when Substrate first reads `cellctl`'s report that the cell is deleted (D4);
- the **version bound** of a deleted cell, by which the provider should have deleted all of its versions (D4).

Task 1 checked the provider behaviour that this document marked † on a disposable bucket. Each result is recorded where the point is stated.

## Goals / Non-Goals

**Goals**
- The platform deletes a deleted cell's backup and media files without an operator step, within a bounded time.
- This change adds no key that deletes file versions or creates keys in the founder account, in the cluster or in CI.
- A compromised `cellctl` can delay a deletion. It cannot cause one or make one happen sooner.
- The operator receives an alert at each state change of each deletion, and an anomaly alert when a deletion stalls or a hide comes early. No deletion alert comes from `cellctl`, and `cellctl` cannot silence a stall alert.
- The operator can hold a wrong deletion during the grace, and can rescue the ciphertext for a time after the hides.

**Non-Goals**
- Automating the legacy backup bucket (D6).
- Changing content-key retirement, the backup lock or media collection.
- A compromised Substrate. It holds the grant key, so it can sign a grant for any cell. This is a stated limit, as in `contain-cloud-controller-compromise`.

## Decisions

### D1. Substrate signs a deletion grant

When Substrate writes `desired_state = 'deleted'` for a cell, it signs a deletion grant with its grant key. The grant key is a dedicated Ed25519 key, `EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY`, separate from the key that signs operation grants. It stores the grant in a new column, `deletion_grant`, of `exomem_cloud_cells`. The database grants `cellctl`'s role read access to that column and no write access. Substrate's grant signer returns nothing while the grant key is absent, so nothing purges before the key exists.

`contain-cloud-controller-compromise` D9 calls this object a signed deletion record. This change calls it the deletion grant, because `make-cloud-cells-operator-resistant` already uses "deletion record" for the record that states `keys_expire_at`.

The grant is an Ed25519 JWT with exactly these claims:
- `aud`: `exomem-cell-delete`;
- `iat` and `jti`;
- `cell_id`;
- `purge_after`: an integer time, `iat` plus G.

**Rules.**
- G lives once, in Substrate's code, as the constant `CELL_DELETION_GRACE_SECONDS`. Its value is 7 days. A change to G is a Substrate release.
- The grant carries no `exp`. A deletion is final, so a grant stays true, and a replayed grant finds nothing left to purge. A grant therefore stays valid until the deletion has finished, through a hold or an executor outage.
- An executor refuses a grant whose `purge_after` is less than `iat` plus one day, so a bad configuration cannot remove the grace.
- An executor takes the cell ID only from the verified claims.
- An executor skips a malformed or unverifiable entry on its own, logs the refusal, and acts on the rest. Grant handling never stops the custodian from creating keys for new cells.

**Verification.** Each executor verifies a grant with a verifier in its own image. The custodian runs the `cellctl` image, which holds no `exomem` package, so it cannot import `cloud_origin.signed_claims`.
- The edge's verifier lives in `src/exomem`, beside `signed_claims`, which requires an `exp`.
- The custodian's verifier lives in `cellctl`. `pyjwt[crypto]` moves from `cellctl`'s dev group to its runtime dependencies.
- Each verifier checks the type, key, audience, exact claim set, integer `iat` and `purge_after`, and the format of `cell_id`.
- One fixture of signed and refused grants keeps one claim definition. Both test suites read it, as the `COLLECTOR_AUDIENCE` cross-test keeps one audience in step.

**Relay.** `cellctl` relays each grant into one ConfigMap, `exomem-cell-deletions`, in its own namespace. It removes a grant only after its list-only views of both buckets show no `upload` and no `start` entry under the cell's prefix.
- Admission lets `cellctl` write only that ConfigMap name and its one key, as one more exception to admission rule 1.
- The exception does not depend on `mediaEdge.enabled`, because the custodian reads the ConfigMap with the edge off.
- The custodian gains `get` on that one ConfigMap. The edge already runs in `cellctl`'s namespace, and it mounts the ConfigMap's key.
- `cellctl` can withhold or drop a grant, which delays a deletion. It cannot forge a grant, because it does not hold the grant key.

**Backfill.** After the rehearsal (Rollout), Substrate signs a grant for each cell that is already deleted. A backfilled grant starts a full grace. The executors find nothing to do for a cell that the script already cleaned.

**Finality.** Substrate never reverses a cell's deletion, and its database refuses a change to a deleted cell's desired state. A restore of a deleted cell's data goes into a new cell ID. An executor therefore needs no path that cancels a grant. The custodian also reads the namespace again at each hide pass (D2). So a namespace that appears again stops the hide, and `cellctl` can only delay through it.

### D2. The key custodian purges the backup prefix

The backups live in the locked backup bucket of the cell-data account. Inside the cluster, the custodian holds the only key there that creates keys.

1. **Delete the cell's key.** For a valid grant, the custodian reads the cell's namespace with a direct GET, not from a cache. When the GET returns 404, it deletes each key that the provider's key listing shows for the prefix `cells/<cell_id>/`. It takes key IDs only from that listing. The cell key's secret lived only in the namespace's Secret, so nothing can use the key any more.
2. **Mint a purge key.** At or after `purge_after`, the custodian reads the namespace again with a direct GET. Only while the GET returns 404 does it mint a purge key. The key has the backup bucket, the name prefix `cells/<cell_id>/`, exactly `listFiles` and `writeFiles`, and a short `validDurationInSeconds`.
3. **Hide.** With the purge key, the custodian hides every current file name under the prefix. It lists again until no current name remains. Then it deletes the purge key.

**Done.** A pass that finds no current name marks the cell done.
- The custodian keeps that mark in the ConfigMap `exomem-key-custodian-purges`, in its own namespace. Its admission rule gains a write of that one ConfigMap name.
- `cellctl` cannot write that ConfigMap, because admission rule 1 keeps `cellctl` out of the custodian's namespace.
- The custodian mints no further purge key for a done cell, so neither a later sweep nor a restart mints one.
- It drops a cell's mark once the cell's grant leaves `exomem-cell-deletions`.
- If the mark is lost, the next sweep checks the prefix once more and marks the cell again.

**Rules.**
- The custodian never mints a key for a cell without a valid grant.
- It keeps at most one key per prefix. Before it mints a purge key, it deletes each key listed for the prefix, such as a stale purge key from a pass that crashed.
- A pass that crashed resumes on the next sweep, from new listings.
- It skips a cell on the hold list (D5).
- While a valid grant names a cell, the custodian creates no cell key for that cell, even when its namespace appears again.

Confirmed (task 1.1): a key limited to the bucket and the prefix, with only `listFiles` and `writeFiles`, hides a current version in the locked bucket.

**Deletion.** The lifecycle rule (`days_from_hiding_to_deleting = 1`) deletes each hidden version once its lock lapses.
- The lock extender (`lock-cloud-backups-against-deletion` D3) is not yet live. It extends only current versions, and its guard refuses a lock more than R plus 1 days ahead.
- So a hidden version's lock ends at most R plus 1 days after the hide. Without the extender, a lock ends R days after its upload, which is earlier.
- The provider therefore deletes the cell's backup versions about R plus 1 days after the hide, plus its own delay. R is 30 days by default, and the variable allows 1 to 60.

Confirmed in part (task 1.1): the provider refuses to delete a locked version (401 `access_denied`), so no rule deletes a hidden version before its lock lapses. After the lapse, the hidden version was gone. The run did not observe the version while it was still locked: that read was lost. The lock lapsed at 2026-11-11 14:55 UTC, and the version was gone by 2026-11-12 18:04 UTC. So the delay after the due day is at most about 27 hours.

### D3. The media edge purges the media prefix

The media bucket lives in the founder account. The edge's key already holds `writeFiles`, which permits a hide. It holds no `listFiles`, so the edge cannot find a cell's names.

- **A list-only key.** Terraform adds `<prefix>-media-edge-list`, with only `listFiles` on the media bucket. Only the edge holds it. The second key gives no isolation, because the same process holds both keys. It exists because the provider cannot change a key in place, and replacing the edge key would cause an outage.
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

The collector credential of `collect-released-media` cannot do this job. Its pass hides only the names that the cell's manifest releases, and `cellctl` removes the cell from `collectors` at revocation.

### D4. The key watcher witnesses every deletion and raises every alert

The watcher runs in CI, outside the cluster, with its own list keys and its own receiver capability. The receiver alerts on a missed run. No deletion alert comes from `cellctl`, the custodian or the edge.

- **The deletion set.** A new Substrate endpoint returns every cell that Substrate deleted with a grant. For each cell it returns `iat`, `purge_after` and the stop time. The endpoint accepts only the watcher's receiver capability. The watcher never takes the set from `cellctl`.
- **The stop time.** `cellctl` reports a cell `deleted` only after it removes the namespace and the edge drops the cell (`add-cloud-media-store` D9). Substrate records the first time that it reads that report. Only the early-hide check uses it.
- **The watch window.** The watcher computes each cell's window from the cell's `purge_after` and the N and R values in its pinned set. The window runs from `iat` to one day after the version bound, and stays open while a current file or a key for the prefix remains. Substrate holds no window value, so the rule lives in one place.
- **A list key on the backup bucket.** Terraform adds a watcher key with only `listFiles` on the backup bucket of the cell-data account. With it and the existing media list key, the watcher lists each watched cell's prefix in both buckets.
- **Values.** The watcher reads R and N from its pinned set, which already holds the backup bucket's default retention and the media bucket's lifecycle rule.

**Per-cell states.** On each run, the watcher derives each watched cell's state again, from both buckets' listings, the key listing and the clock.
- A current file is a name whose newest entry, apart from `start` entries, is an `upload`.
- The states use only the files and keys that exist. A done cell lists no key and no current file, so it is Hidden. The absence of a purge key never raises an alert.
- Each alert name carries the cell ID, which the receiver's name format `[A-Za-z0-9_.:-]{1,64}` allows.

| State | Condition | Alerts |
|---|---|---|
| Pending | The grace runs, or a current file or a key for the prefix remains | `CELL_DELETION_PENDING:<cell_id>` fires |
| Hidden | No current file in either bucket and no key for the prefix, while an `upload` or `start` entry remains | PENDING resolves, and `CELL_DELETION_HIDDEN:<cell_id>` fires |
| Confirmed | No `upload` and no `start` entry in either bucket | HIDDEN resolves |

**Complete reads only.** The watcher derives per-cell states only from a run that read the deletion set, both bucket listings and the key listing in full.
- A failed listing, a listing that reaches the page cap, and a missing or refused key are each a failed read.
- After a failed read, the watcher sends no per-cell transition, so it resolves no alert.
- It raises `DELETIONS_UNKNOWN` for the set, and `DELETION_LISTING_FAILURE` for any other failed read.
- This is the existing watcher's rule: a check that could not run reports nothing.

`CELL_DELETION_ANOMALY:<cell_id>` fires while any of these holds:
- a current file or a key for the prefix remains after `purge_after` plus 1 day;
- an `upload` or `start` entry remains after the version bound;
- the cell has no stop time at `purge_after`, so a withheld report from `cellctl` is visible;
- a hide marker under the prefix carries a time inside the early-hide window.

**The version bound** starts at the later of `purge_after` plus 1 day and the newest hide marker under the prefix. It adds max(N, R + 1) days, plus 2 days.
- A hide may land up to one day after `purge_after` without an anomaly.
- The media bucket deletes a hidden version N days after its hide.
- A hidden backup version's lock ends at most R plus 1 days after its hide (D2).
- The last 2 days cover the provider's delay.
- A hold or an edge outage can delay the hides past `purge_after`. The bound then starts at the newest hide, so the delayed purge is watched to its end.

**The early-hide window** opens at the later of `iat` and the stop time, plus the skew margin. It closes the skew margin before `purge_after`. A cell without a stop time has no window yet, and gets ANOMALY at `purge_after`. The live cell's own hides before its stop fall outside the window, such as a restic prune or a media collection already in flight. So do hides that an executor's clock skew moves across `purge_after`.

**Leaving the window.** At the end of a cell's window, the watcher sends nothing more for the cell. The watcher keeps no state, so it still lists the cell on each run to see whether a current file or a key keeps the window open. A cell that still holds an entry then already has ANOMALY firing, because the version bound passed a day earlier. No resolution arrives, so the receiver keeps that alert until the operator resolves it there.

Confirmed (task 1.1): an expired key no longer appears in the provider's key listing. Either way, the custodian deletes a stale purge key before its next pass (D2).

Confirmed (task 1.1): a key with only `listFiles` sees the `start` entries of unfinished large files. The watcher's states and `cellctl`'s relay rule (D1) depend on it.

**Account-level alerts.**
- `MEDIA_HIDE_BURST` counts only the hides on prefixes of cells outside the deletion set. The watcher already lists the media bucket's versions on each run, and it groups the hides by `cells/<cell_id>/`.
- `PURGE_KEY_UNEXPECTED` fires for a key of the purge shape whose cell is outside the deletion set.
- `DELETIONS_UNKNOWN` fires when the watcher cannot read the deletion set. The watcher then treats every cell as outside the set: it counts every hide and reports every purge key.

**Pinned set.** The watcher accepts the purge shape as a second per-cell key shape, only for a cell in the deletion set. The pinned set gains the edge list key, the watcher's backup list key, the purge shape and the deletion watch.

**Public log.** CI logs are public. The watcher's log prints counts only and never a cell ID. The alert names that carry a cell ID go only to the receiver.

`cellctl`'s alert `cell-storage-cleanup-pending` stays only for a cell with data in the legacy backup bucket (D6).

### D5. Hold and rescue

- **Hold.** During the grace, the operator stops a wrong deletion by adding the cell ID to the chart value `cellDeletion.holdCells`, the hold list.
  - The value is admin tier, so `cellctl` cannot change it.
  - Each executor reads the hold list from a mounted file, so a change restarts neither.
  - Both executors skip a held cell. The watcher's anomaly alert fires after `purge_after` plus 1 day and keeps reminding the operator.
  - A hold keeps the files current, so the operator can restore them into a new cell (D1).
- **Release.** When the operator removes a cell from the hold list, both executors resume its purge. The cell's grant is still valid, because a grant has no expiry (D1).
- **Media rescue.** Within N days of a hide, the operator deletes the hide markers, and each version becomes current again. This is the existing procedure in `docs/runbooks/cloud-media-edge.md`, "Rescue a hidden version". The cleanup script gains a `--rescue` mode that deletes only `hide` entries under the cell's prefix, never an `upload` entry.
- **Backup rescue.** If the provider lets a key delete a hide marker in the locked bucket, `--rescue` works there too. Otherwise the operator copies the hidden versions to a new cell's prefix before their locks lapse.
- **Readable content.** `cellctl` retires the content keys before any purge, so hold and rescue keep only ciphertext.
  - The retired keys come back only from a control-database backup within its retention, about 31 days, together with the platform master key.
  - A cell with a cell-held backup password also needs its escrow copy (`contain-cloud-controller-compromise` D5), which the purge hides with the other files.
  - The edge serves each cell only its own prefix. So a restore into a new cell ID also needs the media copied and re-keyed into the new prefix.
  - This change provides neither the key recovery nor the copy. Without them, rescued media stays unreadable.

Confirmed (task 1.1): a key with `deleteFiles` can delete a hide marker in the locked bucket, so a rescue works there.

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
- **One edge key that also lists.** The provider changes no key in place, and replacing the edge key would cause an outage.
- **The collector credential.** Its pass follows the cell's manifest, and `cellctl` removes the cell from `collectors` at revocation.
- **A grant that expires.** A hold or an outage that outlasts the expiry would leave a purge with no tool to finish it.
- **A chart check that refuses to turn the edge off during a deletion.** It would refuse every upgrade, including the one that adds a hold. The anomaly alert already reports the stall.

## Risks / Trade-offs

- **A compromised Substrate can delete any cell after the grace, and this change cannot detect it.** Substrate holds the grant key. Every alert also goes to Substrate's own receiver, so Substrate can drop the alerts. `PURGE_KEY_UNEXPECTED` samples hourly a key that lives for seconds, and a cell without media trips no burst alert. This is a stated limit.
- **A compromised custodian or node in the cell-data account** can already mint any key there (`contain-cloud-controller-compromise` D9). The purge adds no capability.
- **A compromised edge** can already hide any name in the media bucket (`collect-released-media` D4). The list key adds only names and sizes, which the edge already sees as it writes them.
- **`cellctl` sets when the early-hide check starts.** A compromised `cellctl` can withhold its report that a cell is deleted, and the check then never opens for that cell. The watcher fires ANOMALY for such a cell at `purge_after`. `cellctl` cannot hide a file, and the stall conditions do not use the stop time.
- **Edge downtime delays the media purge.** With the edge off, no media pass runs. The watcher's anomaly alert reports the stall after `purge_after` plus 1 day. The pass resumes when the edge returns, because the grant stays valid (D1).
- **The version bound allows 2 days of provider delay.** Task 1 measures that delay. If it is longer, raise the margin before the bound fires falsely.
- **A deleted cell's backups stay about G plus R plus 1 days, plus the provider's delay.** With G = 7 and R = 30, that is about 38 days plus the delay. The compliance lock requires this.

**What would change this design.**
- If the lifecycle rule ignores the lock, the custodian mints a purge key with `deleteFiles` after the lock lapses. The cell-data account allows that key.
- If Substrate cannot add the endpoint, the grant goes into each prefix's file info. The watcher then loses stall detection.
- If the edge stays off for long periods, the media purge moves back to the script.
- If the provider refuses to delete a hide marker in the locked bucket, and a copy-out is not acceptable, G grows.

## Rollout

1. Run task 1 on a disposable bucket, and record each result in this document.
2. Confirm that Substrate's grant key (`EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY`) is live and the media bucket's lifecycle rule is applied (`collect-released-media` rollout step 5).
3. Apply the two Terraform keys from a saved plan.
4. After its owner agrees, update `KEY_WATCHER_PINNED_SET` by read-modify-write of its live value.
5. Add the watcher's backup list key and the endpoint address to the watcher workflow's secrets.
6. Ship Substrate's grant column, signer and endpoint, without the backfill.
7. Ship the watcher before the executors, so that it witnesses the first purge.
8. Release `cellctl`, the custodian, the edge and the chart together, with an empty hold list.
9. Rehearse on a disposable cell (task 10.1).
10. After task 10.1 passes, run the backfill, so that the backlog cells purge only after the rehearsal.
11. Keep this change active until task 10.2 confirms the provider's deletion.

Expected result: the operator receives each alert of the rehearsal cell, and both buckets list no `upload` and no `start` entry under its prefix.

## Open values

- **Deletion grace G.** 7 days, the value that the current procedure uses. It lives in Substrate's code, as the constant `CELL_DELETION_GRACE_SECONDS`.
- **Media grace N.** 14 days, decided on 2026-11-09.
- **Backup retention R.** The production value lives in the deployment's private variables. The default is 30 days.
- **Skew margin.** Proposed one hour. The watcher's pinned set holds it. It covers clock skew between an executor and the provider, and the requests in flight when a cell stops.
- **Purge key validity.** Proposed one hour. It must cover one hide pass of the largest cell, and a rerun replaces an expired key.
- **Sandbox authority for task 1.** The operator authorizes the disposable bucket and its sandbox credentials.
- **Substrate's grant key.** Whether `EXOMEM_CLOUD_DELETION_GRANT_SIGNING_KEY` is live. It is not the operation-grant key of `contain-cloud-controller-compromise` task 3.3. Until it is, nothing purges, and the manual path stays.
- **Cell counts.** The number of live cells with data in the legacy backup bucket, and the number of deleted cells that await cleanup.
- **Lock extender.** Whether it is live (`lock-cloud-backups-against-deletion` task 2.3). The version bound assumes that locks end at most R plus 1 days after a hide.
- **The pinned set.** The read-modify-write of `KEY_WATCHER_PINNED_SET` for the edge list key, the watcher's backup list key, the purge shape and the deletion watch. `contain-cloud-controller-compromise` owns the watcher's pinned set, so its owner agrees first.
