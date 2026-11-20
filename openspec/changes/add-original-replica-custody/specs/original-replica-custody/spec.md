# Spec Delta

## Purpose

Keep verified replicas of each original in configured stores, report how protected each original is, and make every verified replica available to repair.

## ADDED Requirements

### Requirement: Each store has a capability descriptor

The media store configuration SHALL declare each store with a name, an adapter, and a capability descriptor of three fields:

- failure domain, a string;
- retrieval class, one of the protocol values `immediate`, `minutes` or `hours`;
- whether `head` recomputes the stored content, a boolean.

A store entry whose descriptor fails this schema SHALL be unavailable. The owner status SHALL name the failed field. Code SHALL branch only on descriptor values, never on a store's name.

A vault entry with no list of stores SHALL read as one store, as it does today. That store's retrieval class is `immediate`, and its other descriptor fields are unknown. An unknown field SHALL show as unknown in the status and SHALL NOT make the store unavailable.

#### Scenario: A complete descriptor is accepted

- **WHEN** the configuration declares a `filesystem` store with all three descriptor fields
- **THEN** the store is available for replicas
- **AND** the status lists its failure domain and retrieval class

#### Scenario: An incomplete descriptor is refused

- **WHEN** a store entry in a list of stores omits its failure domain
- **THEN** no replica is made in that store
- **AND** the status names the store and the missing field

#### Scenario: A Cloud cell's existing configuration

- **WHEN** a cell's configuration names one `media-edge` store and no list of stores
- **THEN** the cell's stored-object pointers are replicas in that store, with retrieval class `immediate`
- **AND** the status shows that store's failure domain as unknown

### Requirement: A supported filesystem adapter serves a local disk or a mounted NAS

The `filesystem` adapter SHALL write versioned ciphertext objects under its root and SHALL expose no deletion. It SHALL NOT create its root. The root SHALL carry a store-identity marker that matches the configured store. When the root or its marker is absent or does not match, the adapter SHALL refuse uploads and `head`, and the store SHALL be unavailable. An unavailable store SHALL NOT be taken as proof that a replica is gone.

#### Scenario: An unmounted NAS is unavailable, not empty

- **WHEN** a `filesystem` store's mount point exists but the NAS is not mounted, so the marker is absent
- **THEN** no upload writes under the mount point
- **AND** its replicas keep their recorded state, and the status shows the store as unavailable

### Requirement: An original's binding lists its replicas

An original's binding SHALL list each replica held outside the original's own path. Each entry SHALL name the store, object, version, key version, ciphertext size and ciphertext SHA-256. A binding with a single stored-object pointer SHALL read as a list with that one entry. A writer SHALL keep every entry it does not change, and SHALL keep the earliest entry first. A replica in a store other than the one that serves eviction SHALL NOT change the original's state (`local`, `cached`, `evicted` or `missing`).

#### Scenario: A single pointer reads as one replica

- **WHEN** a binding written before this change holds one stored-object pointer
- **THEN** the original has one replica with that pointer's store, object, version and key version

#### Scenario: A second replica keeps the first

- **WHEN** an original with a Cloud or legacy pointer gains a replica in a second store
- **THEN** the binding lists both replicas, with the earlier one first
- **AND** the original's state is unchanged

### Requirement: A replica is verified at upload and recoverable only with a surviving key

A replica SHALL count as verified at upload when both existing checks passed:

- the original's plaintext hashed to its binding's SHA-256 before encryption;
- the store confirmed the ciphertext size and SHA-256 of the published version.

Every published stored-object pointer meets both checks, because offload publishes a pointer only after the store confirms it.

A verified replica SHALL count as recoverable only when its key version's decryption identity survives the loss of the primary machine:

- On a personal install, the owner has confirmed an export of that identity outside the machine.
- On a Cloud cell, platform key custody wraps the identity.

The owner status SHALL show every other replica as present but not recoverable, and SHALL give the reason. Recoverability is a status about surviving the loss of the primary machine. It SHALL NOT stop fixity from repairing from a verified replica whose identity this install can read.

#### Scenario: A replica with an unexported key

- **WHEN** an original on a personal install has one verified replica in a second store, and the owner has not confirmed the key export
- **THEN** the status shows zero recoverable replicas and one replica that is not recoverable, with the reason "key not exported"
- **AND** the status reports the local original separately

### Requirement: An owner operation copies originals to a store in bounded background batches

One owner operation SHALL make a replica in a named store for each original that lacks one. It SHALL run in bounded batches through the media job ledger and SHALL report the counts queued, copied, refused and remaining. It SHALL hold the vault mutation guard only for its short protection and publication commits, never while it hashes, encrypts or uploads. A retry after a lost reply SHALL reuse the recorded object name.

#### Scenario: Copying an archive to a NAS

- **WHEN** the owner copies 10,000 originals to a `filesystem` store
- **THEN** each original's binding gains one replica entry for that store
- **AND** foreground note writes proceed during the copy without a busy refusal caused by the copy

#### Scenario: A lost reply

- **WHEN** an upload lands and its reply is lost
- **THEN** the retry uses the same object name

### Requirement: The owner sees each original's protection

The owner status SHALL report these values, per store and in aggregate:

- replicas;
- verified replicas;
- recoverable replicas;
- distinct failure domains;
- the oldest verification time.

A per-original view SHALL page through results by path. The aggregate SHALL be one bounded query over indexed verification state. A value that the status cannot read SHALL show as unknown, never as zero.

#### Scenario: A lost verification table

- **WHEN** the verification table is missing
- **THEN** the status shows every count and time as unknown
- **AND** a background rebuild restores the table from the bindings, and the status reports the counts once it finishes
