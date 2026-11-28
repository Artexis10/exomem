## ADDED Requirements

### Requirement: Offload reads the owner total from the binding projection

Offload, the offload scan, the refusal status, upload admission and import window status SHALL read the owner total from the binding projection, which SHALL count what the owner census counts. None of them SHALL read a note for the allowance while it holds the mutation boundary. Under the protection and publication holds, offload SHALL use a total only while the projection is current, and SHALL read the total again when the projection changed after it read the total.

#### Scenario: A save lands during admission at any vault size

- **WHEN** a vault with an allowance binds 1,000 originals, or 100,000, and the owner saves a note while offload admits an original
- **THEN** the save commits without `MUTATION_BUSY`
- **AND** offload reads no note for the allowance while it holds the mutation boundary

#### Scenario: A series of offloads reads the total for each member

- **WHEN** the worker offloads 500 filed originals in a row
- **THEN** each member's admission and publication check read the owner total from the projection, which includes the versions that earlier members published
- **AND** other writes run between two members

#### Scenario: The projection counts what the census counts

- **WHEN** live and Trash companions, two companions that share one version, and a confirmed displaced companion that names another version of the same original reference versions in the selected store, and one companion references another store
- **THEN** the projection's total equals the owner census's total
- **AND** the shared version counts once, the displaced version counts, and the other store's version does not count

#### Scenario: A write between admission and publication is counted

- **WHEN** another companion binds a new version after offload's admission and before its publication check
- **THEN** the publication check reads the total again and decides on the new total

### Requirement: A total that is not current makes offload wait

When the binding projection is not current, offload SHALL return the job to the queue without spending an attempt, and status SHALL report the owner total as being counted. A rebuild that fails SHALL record durably up to five notes with their reasons. Until the projection changes after that failure, the total SHALL be unknown and SHALL name those notes. A page that names a stored version but binds no original SHALL make the total unknown.

#### Scenario: A projection that is not current makes offload wait without spending an attempt

- **WHEN** an outside edit withdraws the projection and offload runs before the rebuild publishes
- **THEN** offload returns the job with `MEDIA_STORE_BUSY`, its attempt count unchanged, and the job runs again after the queue's backoff
- **AND** the original stays local and status reports the total as being counted

#### Scenario: A note that the rebuild cannot count makes the total unknown

- **WHEN** the rebuild meets a companion that names a stored version but binds no original
- **THEN** offload refuses with `MEDIA_ALLOWANCE_UNKNOWN` and names that companion
- **AND** the record of that failure survives a restart of the service

### Requirement: The binding projection follows every change to the references

Every vault with a valid media allowance SHALL keep the binding projection. Each governed write SHALL withdraw the projection or apply its exact change inside its own hold. An outside edit SHALL make the projection not current when the watcher sees it, or at the next reconcile when the event is lost. In file mode the service SHALL withdraw the projection at each start. A rebuild SHALL read the pages outside the mutation boundary.

#### Scenario: An edit made while the service was stopped is counted

- **WHEN** in file mode the owner adds by hand a reference to a version that no other companion names while the service is stopped
- **THEN** after the next start the projection is not current until a rebuild publishes
- **AND** the rebuilt total counts that version

#### Scenario: A rebuild completes while saves continue

- **WHEN** in store mode saves land while a rebuild reads the pages
- **THEN** the rebuild publishes at the frontier of the snapshot that it read, and the audit replay brings the projection to the store's head
- **AND** offload's next check finds the projection current
