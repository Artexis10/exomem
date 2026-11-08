## 1. Provider facts (D2)

- [ ] 1.1 On a disposable bucket, with sandbox credentials that the operator authorizes, check six points:
  - a superseded version and a hidden version are deleted after N days;
  - a current version survives;
  - deleting the hide marker keeps the version;
  - HEAD by name returns 404 for a hidden name;
  - a hide of an already hidden name is a no-op or a known error;
  - a hidden version reads by version ID through the edge.

  Verify: the results are recorded in design.md, and each point marked † in this change is confirmed or corrected.

## 2. Unique names and the release ledger (D1, D5)

- [x] 2.1 Give each new protection record a random name, and reuse the name on retry. Add the append-only release ledger, with a full-sync commit before each reference retires (`media_store.py`, `media_jobs.py`). Verify these with tests:
  - a re-add gets a new name;
  - a retry after a lost reply reuses the name;
  - a reuse keeps the source sidecar's name and version;
  - the existing allowance tests pass;
  - a crash between the release and the unreference keeps the reference.

## 3. Manifest in the backup (D5)

- [x] 3.1 Write the media manifest inside each snapshot in `exomem cell-backup`, from the clone and with the strict census of `add-cloud-media-store` task 4.5.1. Verify: the manifest lists live and Trash references, protected names and released names; an unknown census marks the manifest unknown.

## 4. Edge collector audience (D3)

- [x] 4.1 In `media_edge.py` and `media_edge_protocol.py`, add the collector audience and the `collectors` map in the active set. Add the `condemn` operation and the HEAD `condemned` status. Verify: a runtime credential is refused on `condemn`; a `condemn` of another cell's name is refused; a repeated `condemn` is idempotent; the replacement for `test_no_hide_list_or_delete_route_exists` covers both audiences.

## 5. cellctl and chart (D3)

- [x] 5.1 Sign collector credentials and render them only into the backup Job. Add backup Job egress to the edge port with its admission rule. Make the edge-off check refuse while `collectors` is not empty. Verify: render tests prove that the two credentials are never placed in the other workload.

## 6. Collector pass in the Job (D5)

- [x] 6.1 Read every retained manifest after backup and prune, compute the candidates, and call `condemn`. Verify with `tests/fake_b2.py`, extended by `b2_hide_file`, HEAD-by-name 404 for hidden names, and `lifecycle_tick(days)`:
  - (a) a retained old manifest names n, and n is released: no hide; after that snapshot is pruned, n is hidden;
  - (b) the clone holds the reference and the owner deletes it after the clone: no hide in this hold;
  - (c) a crash-consistent ledger copy where only the fully synced record survives: no hide;
  - (d) a reuse source is released while the reuser's record exists: no hide;
  - (e) several holds and lifecycle ticks, then a restore of the oldest snapshot: every reference HEADs current;
  - (f) one retained snapshot lacks a manifest: zero hide calls;
  - `lifecycle_tick` deletes only hidden or superseded versions.

## 7. Cell handling of `condemned` (D5)

- [x] 7.1 Make the cell refuse to evict on, or reuse, a `condemned` reference, and offload the bytes again under a new name. Verify: the export-restore case, in which an export or a restore holds an original whose version a hold condemned.

## 8. Terraform, watcher and meter (D2, D4)

- [x] 8.1 Add `cloud_media_condemned_days`, with no default for a stack with the media bucket, and the `days_from_hiding_to_deleting` field to the media bucket's one rule. Verify: a `terraform test` shows that a plan of the media bucket without a value fails, and that a plan with a value sets only that field of the rule and hides nothing by age.
- [x] 8.2 Add a list-only media bucket key to the out-of-cluster key watcher, and the hide-budget alert. Verify: a watcher test where a hide burst above the budget alerts with a count and a reason token, and no name. A burst below the budget raises no alert.
- [x] 8.3 Make the `cellctl` meter count names with more than one upload version. Verify: meter tests for zero, one and several repeated names, and a listing failure that reports unknown, not zero.

## 9. Runbook (D4, Rollout)

- [ ] 9.1 Write the runbook for rescue within the grace and for the canary gate. Include the operator steps of the design's Rollout section. Verify: on the disposable bucket from task 1, the runbook returns a hidden version to current. The gate refuses a bucket that holds a repeated name.
