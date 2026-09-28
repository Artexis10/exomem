## ADDED Requirements

### Requirement: Import archives are encrypted to a key only the import holds

A tenant import SHALL be encrypted to an age X25519 recipient whose private identity the tenant's cell mints on its own volume. Only the public recipient SHALL leave the cell. The identity MUST NOT be written to the control database, object storage, a backup, a Kubernetes Secret, a log or any operator-visible channel. It SHALL be destroyed when the import ends, whether it succeeded, failed, was cancelled or expired.

A one-time recipient minted on the serving node SHALL be used only to restore the Cloud owner's own vault into the owner's own cell. Its identity is held in memory-backed storage only and destroyed when the restore ends. Any other user's restore SHALL use a cell-minted identity.

An archive MUST be refused unless it decrypts with exactly that import's identity.

#### Scenario: A tenant starts an import

- **WHEN** a tenant starts an import
- **THEN** the cell returns a fresh recipient bound to that import
- **AND** no copy of its identity exists outside the cell's volume, including in the cell's backups

#### Scenario: An archive encrypted to another key

- **WHEN** the uploaded archive does not decrypt with the import's identity
- **THEN** the import fails with a typed error, and the vault is unchanged

### Requirement: Ciphertext goes straight to object storage and is authenticated by the tenant's session

The archive SHALL be uploaded as ciphertext directly to object storage, into one object under the cell's own prefix. Part upload URLs SHALL:

- name exactly one part of that object;
- allow writing only;
- expire within 24 hours.

The tenant's client SHALL report the parts' ETags and the ciphertext's SHA-256 and size through its authenticated session. The controller SHALL complete the upload with exactly those ETags, and the import SHALL verify the SHA-256 before decrypting. Encryption to the import's recipient is not evidence of who uploaded an archive.

The web app, the gateway and the control database MUST NOT receive, proxy or store the archive's bytes.

#### Scenario: The upload URL cannot read

- **WHEN** a holder of a part upload URL tries to read, list or overwrite any other object
- **THEN** object storage refuses it

#### Scenario: A part is replaced by someone else

- **WHEN** a part is overwritten through a leaked URL with a validly encrypted substitute
- **THEN** the completion or the digest check fails, the import fails with a typed error, and the vault is unchanged

### Requirement: An import runs inside the cell's boundary under an import hold

The controller SHALL run every import Job, including key preparation and cleanup, only while the cell carries an `import` hold and no runtime pod uses the cell's volume. The hold is recorded on the row with its start time and resumed after a controller restart, like the other holds. It SHALL have a deadline, after which the import fails and the cell starts again.

The import SHALL run in the cell's namespace, on the cell's own image, with object-storage egress only. It SHALL stream the archive through decryption and unpacking without writing plaintext anywhere but the cell's volume. When it ends, the cell SHALL start again, and the hold SHALL be released.

#### Scenario: The controller restarts during an import

- **WHEN** cellctl restarts while a cell carries an import hold
- **THEN** the row shows the hold and its start time
- **AND** the import either resumes from its recorded step or fails cleanly, and the cell does not stay stopped

#### Scenario: An import Job hangs

- **WHEN** an import hold passes its deadline
- **THEN** the import fails with a typed error, the cell starts again, and the vault is unchanged

### Requirement: Archive members are validated, and nothing is committed until the whole archive is proven

An import SHALL accept only:

- regular files and directories;
- relative paths inside the archive root;
- names that are valid on the cell's filesystem.

It SHALL refuse:

- links, devices and other special entries;
- absolute paths and `..` components;
- archives over the per-import size or member-count cap;
- archives larger than the cell's remaining storage allowance.

Nothing SHALL be written outside the import's own staging directory until decryption and validation complete. The staging directory SHALL be committed only when the decryptor finished successfully, the archive's end-of-archive marker was read, and the file and byte counts match what the sender declared.

A refusal SHALL name its reason with a typed, content-free error, and SHALL remove the staging directory. The vault SHALL be left unchanged.

#### Scenario: A path escape

- **WHEN** an archive contains `../../host/credentials`
- **THEN** the import is refused, nothing is written outside its staging directory, and the staging directory is removed

#### Scenario: A stream that stops between members

- **WHEN** the decrypted stream ends after a complete member but before the end-of-archive marker
- **THEN** the import is refused as unreadable, and nothing is committed

### Requirement: Tenant imports are staged and adopted, never written over existing notes

A tenant import SHALL unpack into a staging folder named for that import, outside the governed Knowledge Base layer. It MUST NOT overwrite or delete any existing note.

`adopt_vault` SHALL be available on cloud cells, so that the tenant's assistant can scan an import folder and propose how to file it. Filing SHALL go through Exomem's governed writes, with the original paths and hashes kept as provenance.

A tenant SHALL be able to discard a staged import folder completely, not only to trash.

#### Scenario: A tenant imports an Obsidian vault into a cell that already has notes

- **WHEN** the import completes
- **THEN** the existing notes are unchanged, the imported files are in the import's staging folder, and `adopt_vault` over that folder returns a scan and proposals

#### Scenario: A tenant discards an import

- **WHEN** a tenant discards a staged import folder
- **THEN** the folder is removed from the vault, not moved to trash, and later backups no longer contain it

### Requirement: An owner restore replaces the vault, keeps the cell's custody and can be reversed

A restore of the Cloud owner's existing Exomem vault into the owner's cell SHALL:

- replace the contents of the cell's vault;
- preserve the cell's custody directory and its owner-only modes;
- set aside the vault's derived state, so that the cell rebuilds it;
- keep the prior vault and its derived state until the restored cell has answered recall and a backup has completed.

A restore SHALL be refused when the source vault's governance schema is one a standalone cell cannot serve. The cell SHALL be stopped by a means the control plane cannot undo mid-restore, and the operator SHALL confirm no runtime pod uses the volume before each change to it. The swap SHALL be rehearsed on the same cell image before production. A cell SHALL NOT initialise an empty vault while a restore's prior directory exists.

#### Scenario: The restored cell serves recall

- **WHEN** an operator restores the owner's vault into the owner's cell
- **THEN** the cell keeps its identity and custody, starts on the restored vault, answers recall for a known note, and no longer answers for notes only the prior vault held
- **AND** its next backup succeeds before the prior vault is deleted

#### Scenario: Incompatible governance schema

- **WHEN** the source vault is at a governance schema a standalone cell refuses to serve
- **THEN** the restore stops before anything is transferred

### Requirement: Imports fail safe and leave nothing behind

A failed import SHALL leave the vault as it was before the import. When an import ends, the archive object, any unfinished upload and the import's identity SHALL be deleted. The tenant SHALL see the import's state and, on failure, its typed error, with no content in either.

Import logs SHALL be content-free: counts, sizes, durations and error codes only, never member names or paths.

#### Scenario: An import fails partway

- **WHEN** unpacking fails after some members were written to staging
- **THEN** the staging folder is removed, existing notes are untouched, and the archive object and identity are deleted
