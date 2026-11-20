# Spec Delta

## Purpose

Keep verified replicas of each original in configured stores, report how protected each original is, and make every verified replica available to repair.

## ADDED Requirements

### Requirement: Each store has a capability descriptor

The media store configuration SHALL declare each store in an optional `stores` list beside the per-vault `store` key, which names the store that serves eviction. An entry SHALL have a name, an adapter, the connection fields of its adapter and a capability descriptor of three fields:

- failure domain, a string;
- retrieval class, one of the protocol values `immediate`, `minutes` or `hours`;
- whether `head` recomputes the stored content, a boolean.

A store entry whose descriptor fails this schema SHALL be unavailable, and only that store. So SHALL a store entry whose connection fields are invalid, and a `media-edge` entry that is not the store serving eviction, because the edge is one connection for the process and has none of its own. The owner status SHALL name the missing or invalid field, or the reason. Code SHALL branch only on descriptor values, never on a store's name.

A vault entry with no list of stores SHALL read as one store, as it does today, and a vault entry that has the list SHALL keep its `store` key. That store's retrieval class is `immediate`, and its other descriptor fields are unknown unless the list gives them. An unknown field SHALL show as unknown in the status and SHALL NOT make the store unavailable.

#### Scenario: A complete descriptor is accepted

- **WHEN** the configuration declares a `filesystem` store with all three descriptor fields
- **THEN** the store is available for replicas
- **AND** the status lists its failure domain and retrieval class

#### Scenario: An incomplete descriptor is refused

- **WHEN** a store entry in a list of stores omits its failure domain
- **THEN** no replica is made in that store
- **AND** the status names the store and the missing field

#### Scenario: A malformed connection field

- **WHEN** a store entry gives a connection field that its adapter cannot read, such as a size limit that is not a number
- **THEN** only that store is unavailable, and the status names the reason
- **AND** the store serving eviction and every other store keep working

#### Scenario: A Cloud cell's existing configuration

- **WHEN** a cell's configuration names one `media-edge` store and no list of stores
- **THEN** the cell's stored-object pointers are the replicas of that store, with retrieval class `immediate`
- **AND** the status shows that store's failure domain as unknown

### Requirement: A supported filesystem adapter serves a local disk or a mounted NAS

The `filesystem` adapter, which also answers to the name `filesystem-test`, SHALL write versioned ciphertext objects under its root and SHALL expose no deletion. It SHALL NOT create its root. The root SHALL carry a store-identity marker that names the configured store and holds a random identity. Only an explicit owner declaration SHALL write the marker, and it SHALL refuse a root on the vault's own device. The declaration SHALL record the marker's identity in the media ledger, and a later declaration SHALL refuse a root whose marker holds another identity. When the root or its marker is absent, names another store or holds an identity other than the recorded one, the adapter SHALL refuse uploads and `head`, and the store SHALL be unavailable. An unavailable store SHALL NOT be taken as proof that a replica is gone.

#### Scenario: An unmounted NAS is unavailable, not empty

- **WHEN** a `filesystem` store's mount point exists but the NAS is not mounted, so the marker is absent
- **THEN** no upload writes under the mount point
- **AND** its replicas keep their recorded state, and the status shows the store as unavailable

#### Scenario: Another disk answers to the same name

- **WHEN** a root's marker names the configured store but holds an identity other than the one that the declaration recorded
- **THEN** the store is unavailable and no upload reaches that root

#### Scenario: An owner declares a store

- **WHEN** the owner declares a store whose root is a mounted directory on another device
- **THEN** the root's marker names that store
- **AND** a root on the vault's own device is refused and gets no marker

### Requirement: An original's binding lists its replicas

An original's binding SHALL list each replica held outside the original's own path. The `stored_object` pointer SHALL stay the pointer of the store that serves eviction, with its present fields. A sibling key `replicas` SHALL list the replicas in every other store, each with the same fields as the pointer: store, object, version, key version, ciphertext size, ciphertext SHA-256 and format. The binding's logical list is the pointer, then `replicas`, earliest first. A binding with only a pointer SHALL read as a list with that one entry. A writer SHALL keep every entry it does not change. A release that does not read `replicas` SHALL still read the pointer. A replica in a store other than the one that serves eviction SHALL NOT change the original's state (`local`, `cached`, `evicted` or `missing`).

#### Scenario: A single pointer reads as one replica

- **WHEN** a binding written before this change holds one stored-object pointer
- **THEN** the original has one replica with that pointer's store, object, version and key version

#### Scenario: A release without replicas reads the binding

- **WHEN** a release that does not know `replicas` reads a binding that lists a second replica
- **THEN** it reads the same pointer and the same original state as before the replica existed

#### Scenario: A second replica keeps the first

- **WHEN** an original with a Cloud or legacy pointer gains a replica in a second store
- **THEN** the binding lists both replicas, with the earlier one first, the pointer unchanged and the new replica in `replicas`
- **AND** the original's state is unchanged

### Requirement: A replica is verified at upload and recoverable only with a surviving key

A replica SHALL count as verified at upload when both existing checks passed:

- the original's plaintext hashed to its binding's SHA-256 before encryption;
- the store confirmed the ciphertext size and SHA-256 of the published version.

Every published stored-object pointer meets both checks, because offload publishes a pointer only after the store confirms it. A replica of an evicted original is a copy of the ciphertext that the store serving eviction holds, hashed against that pointer's recorded digest; the plaintext checks were made at that upload.

A verified replica SHALL count as recoverable only when its key version's decryption identity survives the loss of the primary machine:

- On a personal install, the owner has confirmed an export of that identity outside the machine, once for each key version, and the media ledger records the confirmation.
- On a Cloud cell, platform key custody wraps the identity, and the configuration says so.

The owner status SHALL show every other replica of a personal install as present but not recoverable, and SHALL give the reason. A replica whose custody the configuration does not state SHALL show recoverability as unknown. Recoverability is a status about surviving the loss of the primary machine. It SHALL NOT stop fixity from repairing from a verified replica whose identity this install can read.

#### Scenario: A replica with an unexported key

- **WHEN** an original on a personal install has one verified replica in a second store, and the owner has not confirmed the key export
- **THEN** the status shows zero recoverable replicas and one replica that is not recoverable, with the reason "key not exported"
- **AND** the status reports the local original separately

#### Scenario: The owner confirms the export

- **WHEN** the owner confirms the export of the key version
- **THEN** the status counts that replica as recoverable

### Requirement: An owner operation copies originals to a store in bounded background batches

One owner operation, the `replicate` operation of `process_media`, SHALL make a replica in a named declared store for each original that lacks one. It SHALL refuse a caller that is not the vault owner. It SHALL run in bounded background batches and SHALL report the counts queued, copied, refused and remaining. It SHALL hold the vault mutation guard only for its short protection and publication commits, never while it hashes, encrypts or uploads. A retry after a lost reply SHALL reuse the recorded object name. It SHALL refuse the store that serves eviction, because offload fills that store, and a store that is unavailable. A repeat while a request for the store is running SHALL change nothing and SHALL return the current counts; once the request has finished, a repeat starts a new one. The worker SHALL check that a store answers as itself before it pins, hashes, encrypts or downloads for that store. A store that does not answer SHALL pause only its own queue, spend no attempt and write no refusal.

#### Scenario: Copying an archive to a NAS

- **WHEN** the owner copies 10,000 originals to a `filesystem` store
- **THEN** each original's binding gains one replica entry for that store
- **AND** foreground note writes proceed during the copy without a busy refusal caused by the copy

#### Scenario: An evicted original

- **WHEN** the owner copies an original whose bytes are not on the volume
- **THEN** the replica is the ciphertext that the store serving eviction holds, and the original stays evicted

#### Scenario: A repeat while the copy runs

- **WHEN** the owner repeats `replicate` for a store whose copy is partly done
- **THEN** the counts of copied originals stay and no new scan of the vault starts

#### Scenario: A store drops off during the copy

- **WHEN** a store's mount disappears while originals wait to be copied
- **THEN** the worker reads no original and encrypts nothing for that store
- **AND** its queued originals stay pending with their attempts unspent, and the other stores' queues go on

#### Scenario: A lost reply

- **WHEN** an upload lands and its reply is lost
- **THEN** the retry uses the same object name

### Requirement: The owner sees each original's protection

The owner status SHALL report these values, per store and in aggregate:

- replicas;
- verified replicas;
- recoverable replicas;
- distinct failure domains, unknown when a store that holds replicas declares none;
- the oldest verification time.

A per-original view SHALL page through results by path. The aggregate SHALL be one bounded query over indexed verification state, and SHALL NOT walk the bindings. A value that the status cannot read SHALL show as unknown, never as zero. The status SHALL NOT read a store: it reports whether each store answered as itself as the background worker last observed it, with the time of that observation. A replica that is known not to survive and a replica whose custody is unstated SHALL be reported apart. A replica that a rebuild restored from its binding has no recorded upload time, and the status SHALL count it as verification time unknown. The status is for the vault owner and SHALL NOT reach another audience.

#### Scenario: A lost verification table

- **WHEN** the verification table is missing
- **THEN** the status shows every count and time as unknown
- **AND** a background rebuild restores the table from the bindings, and the status reports the counts once it finishes
