## ADDED Requirements

### Requirement: Each cell has its own key hierarchy

Each Cloud cell SHALL have one cell root key, generated for that cell and used by no other cell.

A content key is a key that decrypts the cell's content without any other tenant-held key. Every content key SHALL be derived from the cell root key or wrapped by it. That covers the volume passphrase in standard mode, the inner-layer key in tenant-locked mode, the backup repository password, the media key bundle and any later key that decrypts content. No content key SHALL be wrapped only by a platform-wide key.

An infrastructure key is a per-cell key that decrypts only the ciphertext of a tenant-held inner layer. A tenant-locked cell's outer volume passphrase is one, and it MAY be wrapped under the platform master key. No key of either kind SHALL be shared between cells.

In standard mode, the platform SHALL store the cell root key wrapped under the platform master key, with the cell ID, purpose and version bound as additional data.

Every content-bearing scratch path of a cell SHALL sit on the cell's own encrypted volume or in memory-backed storage within the cell's memory limit. These paths include the runtime's temporary directory, media job scratch, and import and export staging. A cell whose scratch does not meet this SHALL be reported as not meeting this requirement.

A cell whose volume was created under a shared passphrase SHALL count as migrated only once its data runs on a volume whose LUKS master key was generated for that cell. A keyslot change alone does not migrate a cell. A cell whose storage cannot yet carry a per-cell volume key SHALL be reported as not meeting this requirement.

#### Scenario: Two cells' volume keys

- **WHEN** the volume passphrases of two cells are compared
- **THEN** they differ, and neither opens the other cell's volume

#### Scenario: A content key leaks

- **WHEN** one cell's volume passphrase or backup repository password becomes known
- **THEN** no other cell's volume, backups or stored media decrypt with it

#### Scenario: A media key bundle is stored

- **WHEN** the stored form of a cell's media key bundle is inspected
- **THEN** that cell's cell root key wraps it, and the platform master key does not wrap it directly

#### Scenario: An existing volume migrates

- **WHEN** a cell created under the shared volume passphrase moves to a per-cell key
- **THEN** its data runs on a new volume created under the cell's own passphrase, verified by recall, governance status and a governed write
- **AND** the shared passphrase opens no volume of that cell

#### Scenario: Media and export scratch

- **WHEN** a cell extracts frames and a transcript from a video, and stages an owner export
- **THEN** no frame, transcript or archive is written to node storage outside the cell's encrypted volume or memory

#### Scenario: A cell on storage without per-cell keys

- **WHEN** a cell runs on storage that cannot yet carry a per-cell volume key
- **THEN** its status reports that the per-cell key requirement is not met

#### Scenario: A streamed owner export completes

- **WHEN** the cell uploads its canonical archive one part at a time
- **THEN** the controller completes a private object and records its immutable version and packing report while the export remains preparing
- **AND** a cell-image Job verifies only that version through bounded Range reads with the canonical archive verifier
- **AND** the export becomes ready only when the full archive, manifest, size and hash pass
- **AND** failed, superseded or deleted exports remove their unpublished object through the existing cleanup path


### Requirement: Deleting a cell expires its keys by a stated date

When a cell is deleted, the platform SHALL destroy every live copy of its cell root key, of the content keys under it and of any infrastructure key. That covers control-database rows, Secrets and the tenant's wraps.

Copies inside platform backups SHALL expire with those backups' retention. Platform backups include control-database backups, cluster datastore snapshots and backup object versions. The deletion record SHALL retain each deployed policy's source, window and observation time. It SHALL set `keys_expire_at` only after the last live key copy is absent and every retention window is known. The date SHALL start at the last live copy's destruction, plus the longest retention window. Unknown retention or absence SHALL remain unknown. The platform SHALL report the cell's keys destroyed only after the proven date. Until then, the platform SHALL say which platform backups can still hold copies.

Database retirement SHALL precede Kubernetes and provider probes. The controller SHALL delete the cell's child Secret independently of provider cleanup. A running or terminating workload SHALL prevent a claim that all live copies are absent. A legacy shared passphrase or a retained old volume SHALL prevent a finite destruction claim without its own evidence.

#### Scenario: A provider cannot answer during deletion

- **WHEN** a deleted cell's provider cannot confirm volume or object absence
- **THEN** the controller still retires its live database content wraps
- **AND** it attempts child Secret deletion independently, retains the cleanup credential, and reports unresolved copies as unknown

#### Scenario: A retention policy is unknown

- **WHEN** one deployed backup source has no observed retention window
- **THEN** `keys_expire_at` remains unknown, and the platform does not report complete key destruction

#### Scenario: Live copies at deletion

- **WHEN** cellctl deletes a cell
- **THEN** the live control database and the cluster hold no copy of its cell root key, content keys or infrastructure key, and the deletion record shows `keys_expire_at`

#### Scenario: Keys reported destroyed

- **WHEN** a deleted cell's status is read before `keys_expire_at`
- **THEN** it names the platform backups that still hold copies, and does not report the keys destroyed

### Requirement: A tenant can lock their cell with their own key

A tenant SHALL be able to opt in to a tenant-locked mode, in which the tenant's key wraps the cell root key.

- **No platform copy of a content key.** For a tenant-locked cell, no copy of its cell root key or of any content key SHALL be written to the cluster datastore (including Secrets), the control database (except the tenant's own wraps), a log, or any platform backup. A mount mechanism that passes a content key through a Kubernetes Secret does not meet this requirement. The cell's infrastructure key MAY be held as in standard mode, because it alone yields only inner-layer ciphertext. The cell's content-bearing scratch SHALL sit inside the inner layer or in memory-backed storage.
- **Keys.** The tenant's key SHALL come from a recovery code that is always issued, and MAY also come from a passkey. The platform-wrapped copy SHALL be destroyed only after the tenant confirms that the recovery code was saved. Before the tenant enables the lock, the product SHALL state that losing both the recovery code and every passkey loses the vault, and that the operator cannot recover it.
- **Conversion.** Enabling the lock on an existing cell SHALL replace its cell root key, every content key and its infrastructure key. Its data, backups and stored media SHALL move under the new keys, and the old backup repository and media objects SHALL be deleted once the new ones verify.
- **Completion.** Every tenant-locked cell's status SHALL show `lock_complete_at`: the time the last platform copy of its cell root key or content keys was destroyed, plus the longest platform backup retention. Earlier keys left in platform backups expire with that retention. A new cell counts the platform-wrapped copy that exists until its tenant confirms the recovery code.
- **Unlock and handover.** An unlock or handover SHALL be bound to the cell, the operation, a one-use attempt and an expiry. A handover SHALL be bound to the replacement instance's ephemeral key, and only one SHALL run per cell at a time. A handover that expires or fails SHALL leave the cell locked.
- **Planned restarts.** An upgrade, rollout or stopping hold SHALL hand the key from the running cell to its replacement without the tenant.
- **Unplanned stops.** After a crash, eviction or node failure, the cell SHALL be locked until the tenant unlocks it.
- **While locked.** The gateway SHALL answer requests from the recorded cell state with a coded `CELL_LOCKED` state that names the unlock route. `CELL_LOCKED` SHALL be distinct from any retryable waking state. Backups, indexing and media jobs SHALL NOT run, and SHALL report `locked`, not `failed`. Content-free health, resource and lifecycle status SHALL remain available to the operator. A restore or relocation SHALL wait for the tenant's unlock.
- **Sleep.** A tenant-locked cell SHALL NOT sleep.
- **Leaving the lock.** While the cell is unlocked, the tenant MAY return to standard mode.

#### Scenario: Enabling the lock on a new cell

- **WHEN** a tenant creates a tenant-locked cell, confirms the saved recovery code, and the cell unlocks and serves
- **THEN** no content key or cell root key that the platform can unwrap appears in the cluster datastore, the control database, the logs, or any platform backup taken from then on
- **AND** the infrastructure key that does appear opens only the inner layer's ciphertext

#### Scenario: Converting an existing cell

- **WHEN** a tenant enables the lock on an existing cell
- **THEN** the cell serves under a new cell root key and new content keys, its old backup repository is deleted after the new one verifies, and its status shows `lock_complete_at`

#### Scenario: Crash while tenant-locked

- **WHEN** a tenant-locked cell's pod crashes
- **THEN** the gateway answers its next requests with `CELL_LOCKED` and the unlock route, its backup reports `locked`, and its pod health and resource use stay visible to the operator's routine identity

#### Scenario: Upgrade while tenant-locked

- **WHEN** cellctl upgrades a running, unlocked, tenant-locked cell
- **THEN** the new pod serves without the tenant unlocking again

#### Scenario: Handover expires

- **WHEN** a handover does not complete within its bound
- **THEN** the cell is locked, and the expired handover cannot be replayed

#### Scenario: Restore without the tenant

- **WHEN** the operator starts a restore of a locked tenant-locked cell
- **THEN** the restore waits for the tenant's unlock and reads no backup content before it

#### Scenario: Recovery code after a lost passkey

- **WHEN** a tenant who lost their passkey unlocks with the recovery code
- **THEN** the cell unlocks and serves

#### Scenario: Idle tenant-locked cell

- **WHEN** a tenant-locked cell stays idle past the sleep threshold
- **THEN** it stays awake and keeps its key
