# Design

## Context

See `proposal.md` for the outcome. These parts already exist:

- **Admission digest.** Every original's binding records `binary_sha256` and `binary_size`.
- **`Store` protocol.** `upload(name, stream, size, sha256)`, `head(name, version)` and `open(name, version)` carry offload, eviction and verified hydration. Uploaded bytes are `age` ciphertext. The stored-object pointer pins the ciphertext size and digest.
- **Adapters.** `media-edge` serves Cloud, and `filesystem-test` serves tests. Only cellctl writes the store configuration.
- **Fixity.** Fixity restores a damaged original only from a copy that hashes to the binding. The candidates today are the media-store version, a pool blob, another stored original and a local-import source file.

A drill on a disposable vault with a filesystem store on a second path found these facts:

| Check | Result |
|---|---|
| Damaged local original | Restored from the stored copy |
| Both copies damaged | Reported as `corrupt_no_copy` |
| Search after eviction | Still found the sidecar |
| Offload of 1 GiB on a laptop | 4.7 s (hash, encryption and write) |
| Verified hydration of 1 GiB | 3.1 s |
| Damaged stored copy | Not reported. `FilesystemStore.head` rehashes the object, so eviction refused without a finding. `EdgeStore.head` returns the digest declared to the provider. |

The damaged-stored-copy gap belongs to the next change (`verify-stored-replicas`). This design gives it the capability field it needs.

The repository records no load target. This design sizes for one vault of 500,000 originals and 25 TB, and every original copied to two stores.

## Goals / Non-Goals

**Goals:**
- One original can have several verified copies in owner-declared stores.
- Repair uses any recoverable copy.
- The owner sees how protected each original is.

**Non-Goals:**
- Eviction on personal installs.
- Automatic placement or a storage policy.
- Scheduled verification of remote copies.
- Rclone, SFTP, S3 or cold-archive adapters.
- A change to Cloud's one-copy offload.

## Decisions

### D1: The replica list lives in the binding; verification state lives in rebuildable SQLite

The binding is in the vault and is backed up with it, so the list of copies survives the loss of derived state. It mirrors the fixity design. The binding holds the expected digest, and the table holds only the last observation.

Rejected alternative: keep replicas only in the media job ledger. A lost ledger would then lose the custody record.

A binding with the existing single pointer reads as a list of one. Writers keep every replica entry they do not change.

### D2: A capability descriptor is configuration with a closed schema

These fields are a closed set that this schema fixes:
- failure domain;
- immutability: `none`, `governance` or `compliance`;
- checksum kind;
- whether `head` recomputes content;
- retrieval class: `immediate`, `minutes` or `hours`;
- deletion capability;
- cost per GB-month, egress price and minimum retention days.

The owner or the provider documentation supplies the values. Code branches only on the closed field values, never on a store's name. A descriptor that fails the schema makes that store unavailable, and the status names the reason. Parsing configuration is a parser boundary, so refusing invalid input is correct here.

### D3: One configuration format for personal and Cloud

Personal installs use the same `EXOMEM_MEDIA_STORE_CONFIG` file format and the same parser as Cloud, extended with a list of stores. Cloud keeps writing its file from cellctl.

The file holds paths to key material, never the keys. It lives outside the vault, because a vault copy must not carry the means to decrypt every replica.

### D4: Every replica is ciphertext, and a replica is recoverable only with an escrowed key

All replicas go through the existing `age` path, so there is one write path and no plaintext branch.

An encrypted copy is useless if its identity file dies with the primary disk. So a replica counts as recoverable only when both conditions hold:
- its upload was verified against the declared size and digest;
- the owner has confirmed an export of the decryption identity outside the primary machine.

On a Cloud cell, platform key custody wraps the identity, so that condition is already met. Recoverability is only a status about surviving the loss of the primary machine. Repair still uses any verified replica whose identity the install can read. A missing export therefore never blocks a repair, and Cloud's existing restore from the media-store copy is unchanged.

What this control prevents is a status that reports two safe copies when a disk loss would leave zero. What it costs when it fires wrongly is that status under-reports protection until the owner confirms the export. Only the owner pays, and only once per key.

### D5: Copying is an explicit, bounded owner operation

The operation `replicate` copies originals that lack a replica in the named store. It runs in batches through the media job ledger and the existing offload path. It holds the vault mutation guard only for each pointer publication, and never while it hashes, encrypts or uploads. That is the foreground-first shape the media lock work established.

A retry after a lost reply reuses the recorded object name, as Cloud offload already does.

### D6: Status reads an aggregate, not a walk

The owner status reads per-store counts and the oldest verification time from the SQLite table. That table is indexed by store and state, so the answer is one bounded query at 500,000 originals.

A per-original view pages through results by path.

## Risks / Trade-offs

- **The binding format grows.** A reader older than this change sees the first replica as the single pointer, and the change keeps that entry first. Mitigation: a compatibility test on a binding written by this change.
- **No scheduled check of stored copies yet.** A damaged stored copy stays recoverable in the status until the next change adds verification. Mitigation: status labels the stored copies "verified at upload" with their upload time. This is honest, not "verified".
- **Store-mode vaults.** `make-vault-store-canonical` changes where sidecars live. Mitigation: the replica list is a binding field read through the existing original resolver, not a file-layout assumption.

## Migration Plan

Nothing migrates. A binding gains replicas only when the owner runs `replicate`.

Rollback is a release downgrade. The older reader keeps the first replica and ignores the rest, and the copies stay in their stores.

## Open Questions

- Should the key-export confirmation be a CLI acknowledgement, or a check that the exported identity decrypts a probe object? The second costs one round trip per store, but it proves the export works.
