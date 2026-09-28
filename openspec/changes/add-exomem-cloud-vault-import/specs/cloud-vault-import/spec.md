## ADDED Requirements

### Requirement: Import archives are encrypted to a key only the import holds

A tenant import SHALL be encrypted to an age X25519 recipient whose private identity the tenant's cell mints on its own volume. Only the public recipient SHALL leave the cell. The identity MUST NOT be written to the control database, object storage, a Kubernetes Secret, a log or any operator-visible channel, and SHALL be destroyed when the import ends, whether it succeeded or failed.

An operator-run import SHALL be encrypted to a one-time recipient minted on the serving node, whose identity is held in memory-backed storage only and destroyed when the import ends.

An archive MUST be refused unless it decrypts with exactly that import's identity.

#### Scenario: A tenant starts an import

- **WHEN** a tenant starts an import
- **THEN** the cell returns a fresh recipient bound to that import
- **AND** no copy of its identity exists outside the cell's volume

#### Scenario: An archive encrypted to another key

- **WHEN** the uploaded archive does not decrypt with the import's identity
- **THEN** the import fails with a typed error, and the vault is unchanged

### Requirement: Ciphertext goes straight to object storage under the cell's own prefix

The archive SHALL be uploaded as ciphertext directly to object storage, into one object under the cell's own prefix. Upload URLs SHALL:

- name exactly that object;
- allow writing only;
- expire within 24 hours.

The web app, the gateway and the control database MUST NOT receive, proxy or store the archive's bytes.

#### Scenario: The upload URL cannot read

- **WHEN** a holder of an upload URL tries to read, list or overwrite any other object
- **THEN** object storage refuses it

### Requirement: An import runs inside the cell's boundary under an import hold

The controller SHALL run an import only while the cell carries an `import` hold. The hold is recorded on the row with its start time and resumed after a controller restart, like the other holds.

The import SHALL run in the cell's namespace, on the cell's own image, with object-storage egress only. It SHALL stream the archive through decryption and unpacking without writing plaintext anywhere but the cell's volume. When it ends, the cell SHALL start again, and the hold SHALL be released.

#### Scenario: The controller restarts during an import

- **WHEN** cellctl restarts while a cell carries an import hold
- **THEN** the row shows the hold and its start time
- **AND** the import either resumes from its recorded step or fails cleanly, and the cell does not stay stopped

### Requirement: Archive members are validated before anything is written

An import SHALL accept only:

- regular files and directories;
- relative paths inside the archive root;
- names that are valid on the cell's filesystem.

It SHALL refuse:

- links, devices and other special entries;
- absolute paths and `..` components;
- archives over the per-import size or member-count cap;
- archives larger than the cell's remaining storage allowance.

A refusal SHALL name its reason with a typed, content-free error. The vault SHALL be left unchanged.

#### Scenario: A path escape

- **WHEN** an archive contains `../../host/credentials`
- **THEN** the import is refused before any member is written

### Requirement: Tenant imports are staged and adopted, never written over existing notes

A tenant import SHALL unpack into a staging folder named for that import, outside the governed Knowledge Base layer. It MUST NOT overwrite or delete any existing note.

`adopt_vault` SHALL be available on a cloud cell for staged import folders, so that the tenant's assistant can scan them and propose how to file them. Filing SHALL go through Exomem's governed writes, with the original paths and hashes kept as provenance.

#### Scenario: A tenant imports an Obsidian vault into a cell that already has notes

- **WHEN** the import completes
- **THEN** the existing notes are unchanged, the imported files are in the import's staging folder, and `adopt_vault` over that folder returns a scan and proposals

### Requirement: An operator restore replaces the vault and keeps the cell's custody

An operator-run restore of an existing Exomem vault SHALL:

- replace the contents of the cell's vault;
- preserve the cell's custody root and its owner-only modes;
- remove derived state, so that the cell rebuilds it.

A restore SHALL be refused when the archive's governance schema is one a standalone cell cannot serve. It SHALL first be verified in a scratch namespace: the cell image starts on the restored copy and answers recall. Only then SHALL the cell's own volume be changed. A backup of the restored cell SHALL complete before the tenant is told the restore is done.

#### Scenario: The restored cell serves recall

- **WHEN** an operator restores a vault into a cell
- **THEN** the cell keeps its identity and custody, starts on the restored vault, answers recall for a known note, and its next backup succeeds

#### Scenario: Incompatible governance schema

- **WHEN** the archive's vault is at a governance schema a standalone cell refuses to serve
- **THEN** the restore stops before the cell's volume is touched

### Requirement: Imports fail safe and leave nothing behind

A failed import SHALL leave the vault as it was before the import. When an import ends, the archive object and the import's identity SHALL be deleted. The tenant SHALL see the import's state and, on failure, its typed error, with no content in either.

Import logs SHALL be content-free: counts, sizes, durations and error codes only, never member names or paths.

#### Scenario: An import fails partway

- **WHEN** unpacking fails after some members were written to staging
- **THEN** the staging folder is removed, existing notes are untouched, and the archive object and identity are deleted
