# Proposal

This change specifies verified replicas of an original in more than one owner-chosen store. Its first release keeps the pointer that `add-cloud-media-store` specifies and adds the replicas beside it.

## Why

A personal archive keeps each original on one disk. The vault already records an admission SHA-256 for every original. Its fixity check already repairs a damaged original from a copy that hashes to that digest. Cloud cells already offload encrypted originals through the `Store` seam. No owner can add a second store, and an original can name only one stored copy. So the owner of a large family archive cannot make the product keep a verified replica on a NAS or a remote store, see how many verified replicas exist, or repair from any of them.

A drill on a disposable vault showed that the existing seam works on a second filesystem path. A damaged local original came back from the stored copy in one fixity pass, and search still found the sidecar after eviction. The change is therefore a generalisation of existing code, not a new storage subsystem.

## What Changes

- An original's binding keeps its current `stored_object` pointer, which is the replica in the store that serves eviction. This is the store that the per-vault `store` key names, the only store that offload and eviction use. A new sibling key `replicas` lists every other replica. Each entry has the same seven fields as the pointer: store, object, version, key version, ciphertext size, ciphertext digest and the format. The logical list is the pointer, then `replicas`. A release without this change ignores `replicas`.
- The configuration declares stores in an optional `stores` list beside the kept `store` key. Each store has a capability descriptor with three fields: failure domain, retrieval class, and whether `head` recomputes content. A vault entry without a list of stores, such as a Cloud cell's, reads as one store, as it does today.
- The test filesystem adapter becomes a supported `filesystem` adapter for a local disk or a mounted NAS, and `filesystem-test` stays as its alias. It never deletes and never creates its root. A store-identity marker, written only by an explicit owner declaration, keeps an unmounted NAS from reading as an empty store.
- An owner operation, `process_media` with operation `replicate`, copies every original to a named store in bounded background batches, with progress counts.
- A replica is verified at upload by the checks offload already makes. It counts as recoverable only when its key version's decryption identity survives the loss of the primary machine, and the owner confirms an export once for each key version.
- Fixity repair considers every verified replica whose identity the install can read and whose store is `immediate`, not only the eviction store's pointer. It names the others, and each replica that it tried, in the route.
- An owner status reports replicas, recoverable replicas, failure domains and the oldest verification time, per store and in aggregate, and per original. Unknown values stay visible as unknown.

This change adds no eviction for personal installs, no automatic placement, no scheduled verification of remote copies and no new remote adapter. Later changes add those on this contract.

## Capabilities

### New Capabilities

- `original-replica-custody`: Owner-declared stores with a capability descriptor, a replica list per original, recoverability rules, an owner copy operation and an owner status.

### Modified Capabilities

- `original-fixity`: Repair candidates include every verified replica that the install can read and that an `immediate` store holds.

`add-cloud-media-store` remains active and specifies the stored-object pointer and Cloud offload. This change archives after `add-cloud-media-store`. It modifies that capability only after its requirements become canonical. Cloud keeps its edge adapter, its cellctl-written configuration and one copy per original until a later change says otherwise.

## Impact

The implementation extends `media_store`, `originals`, `original_fixity` and the media job ledger, and adds a `media_replicas` module for the replica table and its copy queue. It reuses `age` encryption, declared-digest upload and verified hydration. It adds no service and no dependency; the copy queue runs on its own background thread in the existing service. Stored-object pointers already in Cloud sidecars stay readable without rewriting.
