# Design

## Context

Offload, eviction, hydration and the drain exist for Cloud cells (`add-cloud-media-store`). Their only gate is the vault's entry in the media store configuration: the `store` key, `enabled`, `eviction_enabled`, the eviction target and margin, the idle time and the hydration limits. Nothing in `media_store`, `media_worker` or `originals` checks for a Cloud cell. The `filesystem` adapter already serves eviction in the test suite, and `FilesystemStore.head` recomputes the whole ciphertext digest, so the head that eviction reads is a content check.

A drill on a Windows drive in WSL (9p drvfs, on two machines) gave these results. Create, write, hard links, symlink refusal, directory fsync and unlink work. `renameat2(RENAME_NOREPLACE)` fails with `EINVAL`. `chmod` is ignored: files read as mode 0777. `held_fs.acquire` therefore refuses the mount at its probe's rename step.

Scale target: one personal vault, up to 50,000 originals and 10 TB of them, each from 1 MB to 2 GiB. Eviction passes, checks and hydration are already bounded per pass. This change adds no query that grows with the number of originals in a request.

The engineering roster's pick-by-problem table ("Agentic engineering routing") has no row for filesystem durability or storage custody, so this design cites none.

## Goals / Non-Goals

**Goals:**

- An owner can free the vault disk by evicting idle originals to a declared `filesystem` store, including a store on a bridged Windows drive.
- Eviction on a personal install never leaves an original that a lost machine or a crash makes unrecoverable.
- Uploads cannot fill the vault disk while offload and eviction catch up.

**Non-Goals:**

- An originals root outside the vault, or plaintext originals on the second drive.
- Eviction to a remote store. The eviction store is a declared `filesystem` store.
- A re-proof path for a quarantine with the failure `absent`. This is the follow-up that `verify-stored-replicas` recorded.
- A held filesystem for the vault or the state root on a bridged mount. Those keep the full capability set.

## Decisions

### D1. One declared `filesystem` store serves eviction

The owner command takes the name of a store that `declare-store` already declared. It writes that name as the vault's `store` key. The command refuses a store that holds replicas, because a replica and an evicted original's pointer would then share a store under two rules. It also refuses a store whose adapter is not `filesystem`.

Alternative: allow any declared store. A remote store adds retrieval latency and cost to every read of an evicted original. That is a separate decision, so this change does not take it.

### D2. A configuration file under the state root

When `EXOMEM_MEDIA_STORE_CONFIG` is unset, `originals.configuration` reads `media-store.json` under the install's state root. The owner command writes the vault's entry there through a held, create-or-replace write of the whole file. The file keeps the existing schema, so one reader serves both paths. A Cloud cell sets the variable explicitly, and that path does not change.

The command writes the values that the existing keys need. It derives `eviction_target` and `eviction_margin` from the owner's free-space floor and the volume's size, and it writes defaults for the idle time, the hydration limits and the new `eviction_settle_seconds`. Each value is a command option, so the owner can change it.

Alternative: require the owner to set the variable in the service environment. That requires a service-unit edit and a restart for a setting that the product can own.

### D3. The held filesystem probe reports no-replace rename separately

`Capabilities` gains `no_replace_rename`, next to `hard_link`. The probe records `False` when the no-replace rename fails with `EINVAL`, `ENOSYS` or `EOPNOTSUPP`, and continues with the rest of its checks. It cleans up through `unlink`. `acquire` takes the set of capabilities that the caller needs. The default set is the full set, so every current caller keeps its current refusal. `FilesystemStore` asks for relative operations, create-only hard-link publication, directory flushing and unlink.

A caller that reaches a rename without the capability gets `CAPABILITY_UNAVAILABLE` from the call. It never gets a replacing rename. Because the kernel returns `EINVAL` for the flag, the system call can refuse but cannot replace.

Alternative: emulate no-replace rename with link and unlink. That emulation is not atomic for a directory, and no adapter path needs it.

### D4. Eviction on a personal install needs a confirmed key export

Before it reads the store, `_evict_one` reads the recoverability of the pointer's key version. On a personal install, eviction keeps the original until the owner confirms an export of that key version (`media-replicas confirm-key-export`). On a Cloud cell, the configured platform custody counts, as `original-replica-custody` already states. The gate is one ledger read for each candidate.

### D5. Eviction waits a settle period after publication

Eviction drops the local bytes only when the stored copy was published at least `eviction_settle_seconds` ago. The head that eviction reads at that time re-reads the whole ciphertext and compares its digest. The owner command's default is 600 seconds. A Cloud cell's configuration may omit the key, which reads as 0.

Reason: a flush through the bridge has no proof that the data reached the disk. Windows writes dirty pages back within seconds, so a copy that still hashes correctly ten minutes after its publication has survived the window in which a crash loses unwritten pages. This is a bound on the risk, not a proof. The status names the wait, so the owner sees it.

Alternative: read the copy back right after the flush. A read through the bridge can be served from the Windows cache, so it proves nothing about the disk.

The media ledger records publication time for each uploaded version. A reused version can use the exact version's publication time from another filing. If no filing records that time, eviction keeps the original.

### D6. An upload floor protects the vault disk

The owner command also sets `upload_floor_bytes`. The shared allocation check refuses an original that would lower the vault's free space below the floor. One-shot upload, held upload, resumable sessions and preserve use the same check before they allocate bytes. The floor is below the eviction target so the worker has time to free space. A refusal returns `MEDIA_SPACE_BUSY` and a retry hint. Bytes that a resumable session already accepted stay held.

### D7. Status says why eviction waits

The owner status reports local and evicted bound physical originals and their plaintext sizes. The worker reuses the drain's physical inventory and records its observation time. Two filings of a shared object contribute one physical original. The worker records one wait reason for a pass. The reason is `key_not_exported`, `store_unavailable`, `settling`, `nothing_idle` or `below_target`. Unreadable values remain unknown.

## Risks / Trade-offs

- [The eviction store holds the only copy of an evicted original] → D4 keeps the key recoverable. The command's output says that a mirror of the vault no longer holds the plaintext. A second replica (`replicate`) is the owner's choice.
- [A bridged drive is not mounted at boot] → The store reads as unavailable. Hydration fails with `MEDIA_STORE_UNAVAILABLE`, and nothing is quarantined, as `verify-stored-replicas` specifies for a volume that goes away.
- [The settle period is a heuristic] → It is configurable and visible. The scheduled content checks of `verify-stored-replicas` re-read the copy later.
- [Files on a bridged drive are readable by any Windows program] → They are encrypted. The command says so.

## Migration Plan

Nothing changes until an owner runs the command. Turning eviction off uses the existing drain, which brings evicted originals back as space allows and keeps the rest readable from the store.
