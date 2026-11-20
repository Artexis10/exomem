# Proposal

This change specifies verified copies of an original in more than one owner-chosen store. Implementation is not authorized yet; it needs its own approved plan.

## Why

A personal archive keeps each original on one disk. The vault already records an admission SHA-256 for every original. Its fixity check already repairs a damaged original from a copy that hashes to that digest. Cloud cells already offload encrypted originals through the `Store` seam. No owner can add a second store, and an original can name only one stored copy. So the owner of a large family archive cannot make the product keep a verified copy on a NAS or a remote store, see how many verified copies exist, or repair from any of them.

A drill on a disposable vault showed that the existing seam works on a second filesystem path. A damaged local original came back from the stored copy in one fixity pass, and search still found the sidecar after eviction. The change is therefore a generalisation of existing code, not a new storage subsystem.

## What Changes

- An original's binding lists every copy that the vault holds outside the original's own path. Each copy names its store, object, version and ciphertext digest. A binding with the current single stored-object pointer reads as a list of one.
- The configuration declares stores. Each store has a capability descriptor with three fields: failure domain, retrieval class, and whether `head` recomputes content. A vault entry without a list of stores, such as a Cloud cell's, reads as one store, as it does today.
- The test filesystem adapter becomes a supported `filesystem` adapter for a local disk or a mounted NAS. It never deletes and never creates its root. A store-identity marker keeps an unmounted NAS from reading as an empty store.
- An owner operation copies originals to a named store in bounded background batches, through the existing media job ledger and offload path.
- A replica is verified at upload by the checks offload already makes. It counts as recoverable only when its decryption key survives the loss of the primary machine.
- Fixity repair considers every verified copy whose key the install can read, not only the one stored-object pointer.
- An owner status reports copies, recoverable copies, failure domains and the last verification time, per original and in aggregate. Unknown values stay visible as unknown.

This change adds no eviction for personal installs, no automatic placement, no scheduled verification of remote copies and no new remote adapter. Later changes add those on this contract.

## Capabilities

### New Capabilities

- `original-replica-custody`: Owner-declared stores with a capability descriptor, a replica list per original, recoverability rules, an owner copy operation and an owner status.

### Modified Capabilities

- `original-fixity`: Repair candidates include every recoverable replica.

`add-cloud-media-store` remains active and specifies the stored-object pointer and Cloud offload. This change archives after `add-cloud-media-store`. It modifies that capability only after its requirements become canonical. Cloud keeps its edge adapter, its cellctl-written configuration and one copy per original until a later change says otherwise.

## Impact

The implementation extends `media_store`, `originals`, `original_fixity` and the media job ledger. It reuses `age` encryption, declared-digest upload and verified hydration. It adds no service and no dependency. Stored-object pointers already in Cloud sidecars stay readable without rewriting.
