## ADDED Requirements

### Requirement: Each cell has its own key hierarchy

Each Cloud cell SHALL have one cell root key, generated for that cell and used by no other cell. Every key that encrypts the cell's data SHALL be derived from that cell root key or wrapped by it. That covers the volume passphrase, the backup repository password, the media key bundle and any later data key. No data key SHALL be shared between cells. No data key SHALL be wrapped only by a platform-wide key.

In standard mode, the platform SHALL store the cell root key wrapped under the platform master key, with the cell ID, purpose and version bound as additional data. A cell whose volume was created under a shared passphrase SHALL count as migrated only after the volume's LUKS master key has been replaced, not only its keyslot. A cell whose storage cannot yet carry a per-cell volume key SHALL be reported as not meeting this requirement.

#### Scenario: Two cells' volume keys

- **WHEN** the volume passphrases of two cells are compared
- **THEN** they differ, and neither opens the other cell's volume

#### Scenario: A data key leaks

- **WHEN** one cell's volume passphrase or backup repository password becomes known
- **THEN** no other cell's volume, backups or stored media decrypt with it

#### Scenario: A media key bundle is unwrapped

- **WHEN** the stored form of a cell's media key bundle is inspected
- **THEN** that cell's cell root key wraps it, and the platform master key does not wrap it directly

#### Scenario: An existing volume migrates

- **WHEN** a cell created under the shared volume passphrase moves to a per-cell key
- **THEN** its volume is re-encrypted under a new LUKS master key before the cell is reported as migrated
- **AND** the shared passphrase no longer opens it

#### Scenario: A cell on storage without per-cell keys

- **WHEN** a cell runs on storage that cannot yet carry a per-cell volume key
- **THEN** its status reports that the per-cell key requirement is not met

### Requirement: Deleting a cell destroys its keys

When a cell is deleted, the platform SHALL destroy every stored copy of its cell root key and of the data keys under it. That covers control-database rows, Secrets and the tenant's wrapped copies. The platform SHALL report the cell deleted only after it observes that those copies are absent. Backup and media objects that a provider still retains SHALL then decrypt with no key that the platform holds.

#### Scenario: Deleted cell's backups

- **WHEN** a cell is deleted while its provider still retains backup objects
- **THEN** no key that the platform holds decrypts those objects

#### Scenario: Deletion is reported after key destruction

- **WHEN** cellctl deletes a cell
- **THEN** it reports the cell deleted only after the control database and the cluster hold no copy of the cell root key or its data keys

### Requirement: A tenant can lock their cell with their own key

A tenant SHALL be able to opt in to a tenant-locked mode. In this mode the tenant's key wraps the cell root key, and the platform holds no copy of it at rest.

- **Keys.** The tenant's key SHALL come from a recovery code that is always issued, and MAY also come from a passkey. The platform-wrapped copy SHALL be destroyed only after the tenant confirms that the recovery code was saved. Before the tenant enables the lock, the product SHALL state that losing both the recovery code and every passkey loses the vault, and that the operator cannot recover it.
- **While unlocked.** The unwrapped cell root key SHALL exist only in memory. A Secret that carries a data key for mounting SHALL be deleted once the mount it served completes.
- **Planned restarts.** An upgrade, rollout or stopping hold SHALL hand the key from the running cell to its replacement without the tenant, and without writing the key to durable storage.
- **Unplanned stops.** After a crash, eviction or node failure, the cell SHALL be locked until the tenant unlocks it.
- **While locked.** Requests SHALL answer with a coded `CELL_LOCKED` state that names the unlock route. Backups, indexing and media jobs SHALL NOT run, and SHALL report `locked`, not `failed`. Content-free health, resource and lifecycle status SHALL remain available to the operator. A restore or relocation SHALL wait for the tenant's unlock.
- **Sleep.** A tenant-locked cell SHALL NOT sleep, unless its tenant opted in to waking locked. No platform process SHALL hold the keys of sleeping cells.
- **Leaving the lock.** While the cell is unlocked, the tenant MAY return to standard mode.

#### Scenario: Enabling the lock

- **WHEN** a tenant enables tenant-locked mode and confirms the saved recovery code
- **THEN** the control database and the cluster hold no copy of the cell root key that the platform can unwrap

#### Scenario: Crash while tenant-locked

- **WHEN** a tenant-locked cell's pod crashes
- **THEN** its next requests answer `CELL_LOCKED` with the unlock route, its backup reports `locked`, and its pod health and resource use stay visible to the operator's routine identity

#### Scenario: Upgrade while tenant-locked

- **WHEN** cellctl upgrades a running, unlocked, tenant-locked cell
- **THEN** the new pod serves without the tenant unlocking again, and no Secret holding a data key remains after its volume is published

#### Scenario: Restore without the tenant

- **WHEN** the operator starts a restore of a locked tenant-locked cell
- **THEN** the restore waits for the tenant's unlock and reads no backup content before it

#### Scenario: Recovery code after a lost passkey

- **WHEN** a tenant who lost their passkey unlocks with the recovery code
- **THEN** the cell unlocks and serves
