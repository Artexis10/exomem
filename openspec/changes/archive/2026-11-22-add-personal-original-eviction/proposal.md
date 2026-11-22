# Proposal

This change lets a personal install free its vault disk. An owner names one declared `filesystem` store, such as a folder on a second drive, to serve eviction. Exomem then keeps a verified, encrypted copy of each original in that store, drops the local bytes of idle originals when the disk passes a limit that the owner sets, and brings an original back when something reads it.

## Why

A personal install keeps every original on the vault disk. A large archive of video fills that disk long before the owner's other drives. Cloud cells already free space this way: offload writes an encrypted copy to the cell's store, eviction drops confirmed, idle originals, and hydration verifies an original before it reappears. None of that code checks for a Cloud cell. What a personal install lacks is safe ground to run it on:

- A personal install has no platform key custody. The encrypted copy is the only copy once the local bytes are gone, and the key that decrypts it lives on the same disk as the vault. A lost machine would leave an unreadable store.
- The common second drive on a Windows machine reaches WSL through a bridge that refuses an atomic no-replace rename. The held filesystem layer refuses the whole mount for that one primitive, although the `filesystem` adapter never renames: it publishes each version create-only through a no-replace hard link, which the bridge supports.
- The bridge gives no evidence that a flush reaches the disk. Eviction removes the only local copy, so it needs evidence that the stored copy outlives a crash or a power cut.
- No owner command makes a store serve eviction, and no space limit stops an upload from filling the vault disk while offload catches up.

## What Changes

- An owner command makes one declared `filesystem` store serve eviction on a personal install, and sets how much of the vault disk to keep free. A second command turns it off, and the existing drain brings evicted originals back as space allows. Eviction stays off until the owner runs the command.
- When `EXOMEM_MEDIA_STORE_CONFIG` is unset, a personal install reads its media store configuration from a file under its state root. The owner command writes that file. A Cloud cell keeps its explicit configuration.
- The held filesystem probe reports an atomic no-replace rename as its own capability, as it already reports hard links. A caller that needs the full set keeps the current refusal. The `filesystem` adapter asks only for create-only publication, flushing and unlinking, so a store on a bridged Windows drive works.
- On a personal install, eviction requires a confirmed export of the pointer's key version. It also requires the store to have confirmed the copy's content at least a settle period after the copy was published. The settle period is a configuration value.
- An upload of an original is refused with a retry hint when it would take the vault disk's free space below the owner's floor. The refusal says why and when to retry.
- The owner status reports how many originals are local and how many are evicted, and why eviction waits: no key export, the store is unavailable, copies are settling, or nothing is idle.

## Capabilities

### New Capabilities

- `personal-original-eviction`: The owner command, the configuration file, the key-export and settle gates, the upload floor and the status for eviction on a personal install.

### Modified Capabilities

- `original-replica-custody`: The `filesystem` adapter serves a root on a mount without an atomic no-replace rename, such as a Windows drive in WSL.

## Impact

- Code: `held_fs` and its POSIX backend (probe and capability set), `media_store` (adapter acquisition, eviction gates), `originals` (configuration path), the upload admission path, `media_replicas` status, and the CLI.
- Owners: nothing changes until an owner runs the command. A Cloud cell's behaviour does not change.
- Operations: a store on a bridged drive inherits that drive's backup coverage. The evicted original's plaintext leaves any mirror of the vault, so the store and the exported key are then the original's only recovery path. The owner command says so.
