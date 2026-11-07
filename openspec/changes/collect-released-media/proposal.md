## Why

`add-cloud-media-store` keeps every stored version of an original. When an owner deletes a recording, its ciphertext stays in the media bucket and stays billed. That change plans to remove old versions with a key that deletes versions, held by `cellctl`. `contain-cloud-controller-compromise` limits what a compromised controller can do. A version-deleting key in the controller would let it destroy the media of every cell.

The media store also names an object by a keyed digest of the original. A re-add after a delete uploads a second version under the same name. A lifecycle rule would then destroy the older version, and a retained backup snapshot can still need it.

On 2026-11-06 the operator chose to build collection before any cell other than the canary uses the media store.

## What Changes

- **Unique object names.** A new protection record gets a random object name. A name receives one upload only. A hide of a name then affects one stored original, never a later upload of the same bytes.
- **One lifecycle rule.** The media bucket deletes a hidden or superseded version after a configured number of days. The number has no default. No process in the cluster deletes versions.
- **A narrow `condemn` operation.** The backup Job asks the media edge to hide a name. It uses its own collector credential, which no other workload holds. `cellctl` stays list-only.
- **A release ledger and a manifest per backup.** The cell commits a release row before a reference retires. Each backup snapshot carries a media manifest. After backup and prune, a name is a candidate only when it is released and no retained manifest names it.
- **Detection and rescue.** `cellctl` reports hidden names for each cell. The out-of-cluster key watcher alerts when hide markers exceed a budget. Within the grace period, the operator rescues a name by deleting its hide marker.
- **Default off, soft fail.** The collector pass runs only for a cell with a collector entry. With a missing or unknown manifest, it condemns nothing. A missed release only leaks storage.

No server-side model is added. No clock decides a deletion.

## Capabilities

### New Capabilities

- `cloud-media-collection`: unique object names, the lifecycle rule, the `condemn` operation and collector credential, the release ledger, backup manifests, candidate selection, detection and rescue.

### Modified Capabilities

- `cloud-media-store`: not canonical yet. The edge operations, bucket retention and owner deletion requirements of `add-cloud-media-store` change in place, as listed below.

### Modified elsewhere (follow-up edit in this delivery)

- `add-cloud-media-store`:
  - design D6: the edge gains the `condemn` operation for a collector credential, and HEAD gains the `condemned` status;
  - design D7: the bucket carries the condemned-days lifecycle rule;
  - design D9: collection is this change's mechanism. An owner-deleted original is hidden at the first backup hold after the last referencing snapshot is pruned, and deleted a configured number of days later;
  - design D4 and task 3.3: new object names are random, not keyed digests;
  - design Risks: the compromised-edge entry loses "cannot destroy versions", because the edge can now hide a name;
  - spec: the edge operations, bucket retention and owner deletion requirements follow the same edits;
  - task 4.6: becomes a pointer to this change;
  - task 6.3: names this change as a second precondition of the fleet switch, after task 5.5.
- `contain-cloud-controller-compromise`, with no task ticks:
  - design D3: the closed template for the backup Job gains the collector credential and edge egress;
  - design D9: the key watcher gains a list-only media bucket key and the hide-budget check;
  - design D7: a hide burst becomes an attack signal.

## Impact

- **Exomem (`src/exomem`):**
  - unique names and the release ledger, in `media_store.py` and `media_jobs.py`;
  - the manifest in `exomem cell-backup`, and the collector pass in the backup Job;
  - the `condemn` operation and HEAD status, in `media_edge.py` and `media_edge_protocol.py`;
  - cell handling of `condemned`.
- **cellctl and chart:**
  - collector credentials and the `collectors` map in the active set;
  - backup Job egress, admission rules and the edge-off check;
  - the meter of names with more than one upload version.
- **Terraform:** the lifecycle variable with no default, in the durability stack.
- **Key watcher:** a list-only key for the media bucket and a hide budget. The pinned set changes by read-modify-write of its live value.
- **Sequencing:** the canary cell may use the media store now. The fleet switch waits for this change and for task 5.5 of `add-cloud-media-store`.
- **Privacy:** a collector credential can hide names under its own cell's prefix. It cannot read, upload or delete. The custody and disclosure rules of `add-cloud-media-store` do not change.
