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

In store mode, when the binding projection is not current, offload SHALL return the job to the queue without spending an attempt, and status SHALL report the owner total as being counted. A failed rebuild SHALL record up to five notes and their reasons where the media worker reads them. Until the store changes after that failure, the total SHALL be unknown and SHALL name those notes. A malformed stored reference, or one without a binding, SHALL make the total unknown.

#### Scenario: A projection that is not current makes offload wait without spending an attempt

- **WHEN** a migration's audit row withdraws the projection and offload runs before the rebuild publishes
- **THEN** offload returns the job with `MEDIA_STORE_BUSY`, its attempt count unchanged, and the job runs again after the queue's backoff
- **AND** the original stays local and status reports the total as being counted

#### Scenario: A reference that the census cannot count makes the total unknown

- **WHEN** a companion's stored reference is malformed, or names a stored version but binds no original
- **THEN** offload in the media worker process refuses with `MEDIA_ALLOWANCE_UNKNOWN` and names that companion

### Requirement: The binding projection follows every change to the references in store mode

In store mode, every vault with a valid media allowance SHALL keep the binding projection, and each change to a page that the store owns SHALL reach it through the store's audit trail. A change that the trail cannot express SHALL make it not current. Pages that the census walks but the store does not own SHALL be read at each rebuild and checked again at each store sweep. A rebuild SHALL read the pages outside the mutation boundary.

#### Scenario: An edit made while the service was stopped is counted

- **WHEN** in store mode the owner adds by hand a reference to a version that no other companion names while the service is stopped
- **THEN** the first store sweep after start imports the edit, and the projection follows it
- **AND** the total counts that version

#### Scenario: A rebuild completes while saves continue

- **WHEN** in store mode saves land while a rebuild reads the pages
- **THEN** the rebuild publishes at the frontier of the snapshot that it read, and the audit replay brings the projection to the store's head
- **AND** offload's next check finds the projection current

#### Scenario: A page that the store does not own is counted

- **WHEN** a sync conflict copy of a companion names a stored version that no other page names
- **THEN** after the next store sweep the projection decides on that page as the owner census does
