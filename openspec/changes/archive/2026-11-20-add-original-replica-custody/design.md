# Design

## Context

See `proposal.md` for the outcome. These parts already exist:

- **Admission digest.** Every original's binding records `binary_sha256` and `binary_size`.
- **The `Store` protocol.** It has three calls: `upload(name, stream, size, sha256)`, `head(name, version)` and `open(name, version)`. Together they carry offload, eviction and verified hydration.
  - Offload hashes the plaintext against the binding, encrypts it with `age`, uploads it, and publishes the stored-object pointer only after the store confirms the ciphertext size and digest.
- **Adapters.** `media-edge` serves Cloud and `filesystem-test` serves tests. Only cellctl writes the store configuration.
- **The store that serves eviction.** The per-vault `store` key names one store. Offload fills it, eviction trusts it, and the sidecar's `stored_object` pointer names its version. In this design "the store that serves eviction" always means that store. Every other store is a replica store.
- **Fixity.** Fixity restores a damaged original only from a copy that hashes to the binding. The candidates are the media-store version, a pool blob, another stored original and a local-import source file.

A drill ran once, on one laptop, against a disposable vault with a filesystem store on a second path (20 Nov 2026). It found these facts:

| Check | Result |
|---|---|
| Damaged local original | Restored from the stored copy |
| Both copies damaged | Reported as `corrupt_no_copy` |
| Search after eviction | Still found the sidecar |
| 1 GiB offload | About 5 s |
| 1 GiB verified hydration | About 3 s |
| Damaged stored copy | Not reported. `FilesystemStore.head` rehashes the object, so eviction refused without a finding. `EdgeStore.head` returns the digest declared to the provider. |

The damaged-stored-copy gap belongs to the next change, `verify-stored-replicas`. This design adds the `head` descriptor field that it needs.

The repository records no load target. This design sizes for one vault of 500,000 originals and 25 TB, with every original replicated to two stores.

## Goals / Non-Goals

**Goals:**

- One original can have several verified replicas in configured stores.
- Repair uses any verified replica the install can decrypt.
- The owner sees how protected each original is.

**Non-Goals:**

- Eviction on personal installs.
- Automatic placement and storage policy.
- Scheduled verification of stored replicas.
- Rclone, SFTP, S3 or cold-archive adapters.
- Cost and immutability fields.
- A change to Cloud's one-copy offload.

## Decisions

### D1: The replica list lives in the binding, and verification state lives in rebuildable SQLite

The binding is in the vault and is backed up with it, so the replica list survives the loss of derived state. This mirrors fixity: the binding holds the expected digest, and the table holds only the last observation.

Rejected alternative: keep replicas only in the media job ledger. A lost ledger would then lose the custody record.

The `stored_object` pointer is unchanged and stays the pointer of the store that serves eviction. Extra replicas go in a new sibling key, `replicas`: a list of the same seven-field entries. The logical list is `[stored_object] + replicas`, earliest first. A binding with only the pointer reads as a list of one. A writer keeps every entry it does not change, and `replicas` ignores any entry that does not parse. A release without this change ignores `replicas` and reads the pointer as before. `add-cloud-media-store`'s pointer contract is therefore unchanged, and `original_state` keeps its meaning: only the pointer decides `local`, `cached`, `evicted` or `missing`.

Rejected alternative: make `stored_object` a list. An older reader would fail on it, and the pointer contract would change under `add-cloud-media-store`.

### D2: The capability descriptor holds only what this change reads

The descriptor has three fields: failure domain, retrieval class and whether `head` recomputes content. Their enum values are protocol values that this schema fixes. Code branches only on these values, never on a store's name.

Cost, egress, immutability and minimum retention come with the policy and cold-archive changes that read them. A field that nothing reads would only add friction to setup.

### D3: One configuration format, and the legacy entry is one store

Personal installs use the same `EXOMEM_MEDIA_STORE_CONFIG` file format and parser as Cloud, extended with an optional per-vault `stores` list beside the kept `store` key. Each entry of the list has a name, an adapter, its connection fields (`root` for a filesystem store) and the three descriptor fields. `store_for(vault, name=None)` returns the store that serves eviction, or the named store.

The top-level fields of the vault entry (`adapter`, `root` and the rest) are the connection of the store that `store` names, as before. A `stores` entry of that name supplies only its descriptor, so the two cannot disagree. A vault entry without a list reads as one store with retrieval class `immediate` and unknown other fields. An unknown field is shown, never refused, so Cloud's cellctl-written file keeps working unchanged. An entry that omits a descriptor field, or gives it an invalid value, makes only that store unavailable, and the status names the field.

Writers keep the per-vault `store` key, so an older parser still reads the file after a downgrade.

The file holds paths to key material, never the keys. It lives outside the vault, because a vault copy must not carry the means to decrypt every replica.

### D4: Every replica is ciphertext, and a replica is recoverable only with a surviving key

All replicas go through the existing `age` path. There is one write path and no plaintext branch.

"Verified at upload" names the two checks that offload already makes. The plaintext must hash to the binding before encryption, and the store must confirm the published ciphertext's size and digest. No replica is read back to earn this state.

A replica of an evicted original is a copy of the ciphertext that the store serving eviction holds. It is hashed on the way against the pointer's recorded digest, so it needs no key and never exists as plaintext on this machine. It keeps the pointer's key version.

An encrypted replica is useless if its identity file dies with the primary disk. A replica therefore counts as recoverable only when its key version's identity survives that loss:
- On a personal install, the owner confirms an export of the identity. The confirmation is a CLI acknowledgement for one key version, recorded in the media ledger. Nothing checks the export: the status says that the owner stated it.
- On a Cloud cell, platform key custody wraps it. The status says so only when the configuration says so, through the optional `key_custody` key. Without it, recoverability reads as unknown, never as true.

Recoverability is only a status. Repair uses any verified replica whose identity the install can read, so a missing export never blocks a repair.

What this control prevents: a status that reports two safe copies when a disk loss would leave zero. Its cost when it fires wrongly: the status under-reports protection until the owner confirms the export. Only the owner pays, once per key version.

### D5: The filesystem adapter proves which store it is writing to

The adapter never creates its root. The root holds a marker file naming the configured store, with a random identity. Only an explicit owner command, `exomem media-replicas declare-store`, writes the marker. It refuses a root on the vault's own device (`st_dev`), because an unmounted NAS leaves its mount point there and a marker written into that empty directory would defeat the marker.

Without the marker, an unmounted NAS mount point looks like an empty store. `head` would answer "absent", offload would drop the pointer, and the new upload would land on the primary disk.

The declaration records the marker's identity in the media ledger, so two disks that carry the same store name are told apart; a ledger that lost the record is repaired by declaring the store again. With the marker missing, naming another store or holding another recorded identity, uploads and `head` refuse. The store reads as unavailable, and replica states stay as recorded. The adapter has no delete capability.

### D6: Copying is a bounded owner operation

`replicate` is an operation of `process_media`, for the vault owner only. It records the owner's request for one declared store and returns the progress counts: queued, copied, refused and remaining. A background worker then makes a replica of each original that lacks one, in bounded batches, and yields to foreground work between originals. It takes the vault mutation guard only for the short protection and publication commits, never while it hashes, encrypts or uploads. A retry after a lost reply reuses the recorded object name, through the existing protection record. A replica in a store that already holds a verified one is kept.

The copy queue has its own tables in the media ledger and its own worker thread. It does not use the `jobs` table: that table holds one row per original and feeds the eviction accounting, and a second store's copy of the same original would collide with it. The worker checks that a store answers as itself before it does any work for it. A store that does not answer pauses only its own queue and spends no attempt. A failure that a retry can pass, such as a copy that breaks half way, retries with a growing delay and then counts as refused. A failure that repeats, such as an oversized object, counts as refused at once. A repeat of `replicate` while a request for the store is running changes nothing and returns the counts. A new request after the last finished arms the refused rows again.

### D7: Status reads an aggregate, and reports unknown during a rebuild

The status reads per-store counts and the oldest verification time from the `media_replicas` table in the media ledger, in one grouped query over a covering index. The table is keyed by original and store. A per-original view pages through results by path, and the status of one exact path reads that original's binding.

A rebuild fills the table from the bindings and records a census row when it completes. A table that is missing or has no census shows every value as unknown, with the reason, until the background rebuild finishes. The worker rebuilds the table daily and after any loss. The status never walks the bindings or reads a store itself: whether a store answers is the worker's last observation, shown with its time. Each row carries the number of the rebuild that last confirmed it, so a row that a publication wrote during a rebuild is never mistaken for a stale one, whatever the clock says. A rebuilt row has no upload time, because the binding does not record one: the status counts those replicas as verification time unknown, never as old or new.

### D8: Fixity tries local copies first

Fixity tries a pool blob, another stored original and a local-import file before any replica. A local copy needs no download. Then it tries the pointer of the store that serves eviction, then each replica that was verified at upload, whose key version's identity this install can read, and whose store is `immediate`. It reads a replica from the replica's own store. The route names each replica that it tried and why it failed. It names a replica in a store that is not `immediate` as one that the owner restores.

On Cloud this reverses today's order, in which the media-store version came first. The outcome is the same, at no download cost when a local copy exists.

The Trash-retry download is out of scope. A retry after a restore that could not be put in place stages the copy again, so a replica read repeats. Avoiding that second download belongs to `verify-stored-replicas`.

## Risks / Trade-offs

- **Each replica store rewrites each sidecar once.** Copying 500,000 originals to one store is 500,000 binding writes, with their index, graph and sync work. Mitigation: bounded batches off the foreground guard. The binding-write cost is measured on a generated 10,000-original vault and extrapolated before any live run. An append-only per-store manifest is the alternative, if that cost breaks the budget.
- **A replica of a deleted original stays in its store.** The adapter has no delete capability, so a replica that no binding lists stays until a later change adds a release path for replica stores.
- **No scheduled check of stored replicas yet.** A damaged stored replica stays "verified at upload" until the next change adds verification. Mitigation: the status labels replicas "verified at upload" with their upload time, never "verified".
- **Store-mode vaults.** `make-vault-store-canonical` changes where sidecars live. Mitigation: the replica list is a binding field read through the existing original resolver, not a file-layout assumption.

## Migration Plan

Nothing migrates. A binding gains replicas only when the owner runs `replicate`.

Rollback is a release downgrade. The older reader ignores `replicas`, keeps the pointer as the original's single stored object and keeps reading the configuration through the per-vault `store` key. The replicas stay in their stores, and the next release lists them again.

## Open Questions

- How does a Cloud cell state that the platform holds its key? The optional `key_custody: "platform"` configuration key does it, and cellctl does not write it yet, so a Cell reads recoverability as unknown until it does.
