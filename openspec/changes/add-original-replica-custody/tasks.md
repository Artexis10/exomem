# Tasks

This change is proposed and not authorized for implementation. Its implementation starts only under an approved plan, after `add-cloud-media-store` archives or with that change's owner agreeing on the stored-object pointer format.

## 1. Configuration and adapter

- [ ] 1.1 Extend the media store configuration parser with a list of stores and the three-field descriptor. Prove that an incomplete entry makes only that store unavailable, and that a Cloud entry without a list reads as one `immediate` store with unknown fields.
- [ ] 1.2 Promote the filesystem adapter to a supported `filesystem` adapter that never creates its root and requires a matching store-identity marker. Prove that an unmounted NAS mount point refuses uploads and `head` and leaves replica states unchanged.

## 2. Replica list and recoverability

- [ ] 2.1 Add the replica list, with key version, to the original binding, and read a legacy single pointer as a list of one. Prove that a binding and a configuration written by this change read correctly on the previous release, and that a second-store replica never changes the original's state.
- [ ] 2.2 Record the owner's key-export confirmation per key version. Prove that the status shows an encrypted copy as not recoverable until the owner confirms it.

## 3. Copy operation

- [ ] 3.1 Add the owner `replicate` operation on the media job ledger and the offload path. Prove bounded batches, reuse of the recorded name after a lost reply, and no foreground busy refusal from the copy on a 10,000-original vault.

## 4. Repair and status

- [ ] 4.1 Make fixity try local copies first, then verified `immediate` replicas whose key the install can read, naming each tried replica in the route. Prove a restore from a second replica when the first is damaged.
- [ ] 4.2 Add the owner status aggregate and the paged per-original view. Prove one bounded query at 500,000 originals, and unknown values until the background rebuild finishes after the table is lost.

## 5. Acceptance

- [ ] 5.1 On a disposable vault with two `filesystem` stores on separate paths, copy the originals, damage each layer in turn, and show restore or honest `corrupt_no_copy` with the run log recorded.
