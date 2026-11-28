## ADDED Requirements

### Requirement: Offload reads the owner total from the binding projection in store mode

In store mode, offload, the offload scan, the refusal status, upload admission and import window status SHALL read the owner total from the binding projection, which SHALL count what the owner census counts. While offload holds the mutation boundary, it SHALL walk no notes for the allowance and SHALL replay at most one batch of 256 audit rows or fewer. It SHALL use a total under a hold only while the projection is current, and SHALL read it again if it moved.

#### Scenario: A save lands during admission at any vault size

- **WHEN** a vault in store mode with an allowance binds 1,000 originals, or 100,000, and the owner saves a note while offload admits an original
- **THEN** the save commits without `MUTATION_BUSY`
- **AND** offload walks no notes for the allowance while it holds the mutation boundary

#### Scenario: A series of offloads reads the total for each member

- **WHEN** the worker offloads 500 filed originals in a row in store mode
- **THEN** each member's admission and publication check read the owner total from the projection, which includes the versions that earlier members published
- **AND** other writes run between two members

#### Scenario: The projection counts what the census counts

- **WHEN** live and Trash companions, two companions that share one version, and a confirmed displaced companion that names another version of the same original reference versions in the selected store, and one companion references another store
- **THEN** the projection's total equals the owner census's total
- **AND** the shared version counts once, the displaced version counts, and the other store's version does not count

#### Scenario: A write between admission and publication is counted

- **WHEN** another companion binds a new version after offload's admission and before its publication check
- **THEN** the publication check reads the total again and decides on the new total

#### Scenario: A lagging projection catches up before the hold

- **WHEN** 600 saves land between offload's admission and its publication hold
- **THEN** offload follows the audit trail until the projection lags by 256 rows or fewer, before it takes the hold
- **AND** under the hold it replays at most 256 rows and publishes without `MEDIA_STORE_BUSY`

### Requirement: A total that is not current makes offload wait

In store mode, when the binding projection is not current, offload SHALL return the job without spending an attempt, and status SHALL report the total as being counted. A note that the projection cannot count, such as one with a malformed or unbound stored reference, SHALL make the total unknown and be named, while the projection stays published. A failed rebuild SHALL record up to five notes where the media worker reads them. An unrelated save SHALL NOT clear a named note or start a rebuild.

#### Scenario: A projection that is not current makes offload wait without spending an attempt

- **WHEN** a migration's audit row withdraws the projection and offload runs before the rebuild publishes
- **THEN** offload returns the job with `MEDIA_STORE_BUSY`, its attempt count unchanged, and the job runs again after the queue's backoff
- **AND** the original stays local and status reports the total as being counted

#### Scenario: A reference that the census cannot count makes the total unknown

- **WHEN** a companion's stored reference is malformed, or names a stored version but binds no original
- **THEN** offload in the media worker process refuses with `MEDIA_ALLOWANCE_UNKNOWN` and names that companion

#### Scenario: An unrelated save does not clear a named note

- **WHEN** a companion that does not parse makes the total unknown, and the owner then saves a note that holds no stored reference
- **THEN** the total stays unknown and names that companion, and no rebuild starts
- **AND** after the owner fixes the companion, the follow makes the total known without a rebuild

### Requirement: The binding projection follows every change to the references in store mode

In store mode, every vault with a valid allowance SHALL keep the binding projection. It SHALL follow the audit trail for each page the store owns, and be current at its head whatever derived-row work raises the revision. A change that the trail cannot express SHALL make it not current. Pages the census walks but the store does not own SHALL be read at each rebuild and checked at each sweep; a save during that walk SHALL NOT withdraw it. A rebuild SHALL read pages outside the mutation boundary.

#### Scenario: An edit made while the service was stopped is counted

- **WHEN** in store mode the owner adds by hand a reference to a version that no other companion names while the service is stopped
- **THEN** the first store sweep after start imports the edit, and the projection follows it
- **AND** the total counts that version

#### Scenario: A rebuild completes while saves continue

- **WHEN** in store mode saves land while a rebuild reads the pages
- **THEN** the rebuild publishes at the audit position of the snapshot that it read, and the audit replay brings the projection to the store's head
- **AND** offload's next check finds the projection current

#### Scenario: A page that the store does not own is counted

- **WHEN** a sync conflict copy of a companion names a stored version that no other page names
- **THEN** after the next store sweep the projection decides on that page as the owner census does

#### Scenario: A save's derivation leaves the projection current

- **WHEN** in store mode the owner saves a note and the derivation drain then raises the store revision
- **THEN** the projection is current once it has followed the save's audit row
- **AND** offload's next check publishes without `MEDIA_STORE_BUSY`, and eviction finds the projection current without a rebuild

#### Scenario: A save during the sweep's walk keeps the projection

- **WHEN** saves land on pages that the store owns while the store sweep reads the pages that it does not own
- **THEN** the check completes and the projection stays published
