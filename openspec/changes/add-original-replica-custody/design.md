# Design

## Context

See `proposal.md` for the outcome. These parts already exist:

- **Admission digest.** Every original's binding records `binary_sha256` and `binary_size`.
- **The `Store` protocol.** It has three calls: `upload(name, stream, size, sha256)`, `head(name, version)` and `open(name, version)`. Together they carry offload, eviction and verified hydration.
  - Offload hashes the plaintext against the binding, encrypts it with `age`, uploads it, and publishes the stored-object pointer only after the store confirms the ciphertext size and digest.
- **Adapters.** `media-edge` serves Cloud and `filesystem-test` serves tests. Only cellctl writes the store configuration.
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

A binding with the existing single pointer reads as a list of one. A replica in a store that does not serve eviction never changes the original's state, so the one state function keeps its meaning.

### D2: The capability descriptor holds only what this change reads

The descriptor has three fields: failure domain, retrieval class and whether `head` recomputes content. Their enum values are protocol values that this schema fixes. Code branches only on these values, never on a store's name.

Cost, egress, immutability and minimum retention come with the policy and cold-archive changes that read them. A field that nothing reads would only add friction to setup.

### D3: One configuration format, and the legacy entry is one store

Personal installs use the same `EXOMEM_MEDIA_STORE_CONFIG` file format and parser as Cloud, extended with a list of stores. A vault entry without that list reads as one store with retrieval class `immediate` and unknown other fields. An unknown field is shown, never refused, so Cloud's cellctl-written file keeps working unchanged.

Writers keep the per-vault `store` key, so an older parser still reads the file after a downgrade.

The file holds paths to key material, never the keys. It lives outside the vault, because a vault copy must not carry the means to decrypt every replica.

### D4: Every replica is ciphertext, and a replica is recoverable only with a surviving key

All replicas go through the existing `age` path. There is one write path and no plaintext branch.

"Verified at upload" names the two checks that offload already makes. The plaintext must hash to the binding before encryption, and the store must confirm the published ciphertext's size and digest. No replica is read back to earn this state.

An encrypted replica is useless if its identity file dies with the primary disk. A replica therefore counts as recoverable only when its key version's identity survives that loss:
- On a personal install, the owner confirms an export of the identity.
- On a Cloud cell, platform key custody wraps it.

Recoverability is only a status. Repair uses any verified replica whose identity the install can read, so a missing export never blocks a repair.

What this control prevents: a status that reports two safe copies when a disk loss would leave zero. Its cost when it fires wrongly: the status under-reports protection until the owner confirms the export. Only the owner pays, once per key version.

### D5: The filesystem adapter proves which store it is writing to

The adapter never creates its root. The root holds a marker naming the configured store.

Without the marker, an unmounted NAS mount point looks like an empty store. `head` would answer "absent", offload would drop the pointer, and the new upload would land on the primary disk.

With the marker missing or different, uploads and `head` refuse. The store reads as unavailable.

### D6: Copying is a bounded owner operation

`replicate` makes a replica in a named store for each original that lacks one. It runs in batches through the media job ledger and the existing offload path. It takes the vault mutation guard only for the short protection and publication commits, never while it hashes, encrypts or uploads. A retry after a lost reply reuses the recorded object name.

### D7: Status reads an aggregate, and reports unknown during a rebuild

The status reads per-store counts and the oldest verification time from an indexed SQLite table, in one bounded query. A per-original view pages through results by path.

A lost table shows every value as unknown until the background rebuild from the bindings finishes. The status never walks the bindings itself.

### D8: Fixity tries local copies first

Fixity tries a pool blob or another stored original before any replica. A local copy needs no download. Only replicas in `immediate` stores are tried automatically. Others are named in the route for the owner to restore.

On Cloud this reverses today's order, in which the media-store version came first. The outcome is the same, at no download cost when a local copy exists.

## Risks / Trade-offs

- **Each replica store rewrites each sidecar once.** Copying 500,000 originals to one store is 500,000 binding writes, with their index, graph and sync work. Mitigation: bounded batches off the foreground guard. The implementation plan measures the write cost on a production-sized copy before any live run. An append-only per-store manifest is the alternative, if that cost breaks the budget.
- **No scheduled check of stored replicas yet.** A damaged stored replica stays "verified at upload" until the next change adds verification. Mitigation: the status labels replicas "verified at upload" with their upload time, never "verified".
- **Store-mode vaults.** `make-vault-store-canonical` changes where sidecars live. Mitigation: the replica list is a binding field read through the existing original resolver, not a file-layout assumption.

## Migration Plan

Nothing migrates. A binding gains replicas only when the owner runs `replicate`.

Rollback is a release downgrade. The older reader keeps the first replica as its single pointer and keeps reading the configuration through the per-vault `store` key. The replicas stay in their stores.

## Open Questions

- How should the owner confirm the key export? A CLI acknowledgement is the simple option. A probe that decrypts a test object with the exported identity proves the export works, but it cannot prove the export is off the machine.
