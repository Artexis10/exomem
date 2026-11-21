# original-replica-custody Specification

## Purpose
Keep verified replicas of each original in configured stores, report how protected each original is, and make every verified replica available to repair.

## Requirements

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
- the oldest verification time;
- the replicas whose last check proved `presence`, `content` or `decrypted`, and the oldest time of each;
- the replicas never checked since upload or since the last rebuild that lost their history;
- the quarantined replicas;
- the replicas of a store whose checks are not scheduled, with the reason;
- the sampled bytes spent this month against the budget, and the replicas skipped for size.

A per-original view SHALL page through results by path, and SHALL show each replica's last check, its level and any finding. A paged list of findings SHALL name each quarantined replica's original, store, failed level, time and re-copy state. The aggregate SHALL be one bounded query over indexed verification state, and SHALL NOT walk the bindings. A value that the status cannot read SHALL show as unknown, never as zero. The status SHALL NOT read a store: it reports whether each store answered as itself as the background worker last observed it, with the time of that observation. A replica that is known not to survive and a replica whose custody is unstated SHALL be reported apart. A replica that a rebuild restored from its binding has no recorded upload time, and the status SHALL count it as verification time unknown. A replica that passed only a presence check SHALL NOT be reported as content-verified. The status is for the vault owner and SHALL NOT reach another audience.

#### Scenario: A lost verification table

- **WHEN** the verification table is missing
- **THEN** the status shows every count and time as unknown
- **AND** a background rebuild restores the table from the bindings, and the status reports the counts once it finishes
- **AND** every replica then reads never checked until its next check

#### Scenario: A finding reaches the owner

- **WHEN** a check quarantines a replica
- **THEN** the status counts it as quarantined, and the findings list names the original, the store, the failed level and the re-copy state

### Requirement: Stored replicas are checked on a cycle with the proof their store allows

The replica worker SHALL check every replica in a store with a declared descriptor on a cycle, with a default of 30 days. The check SHALL use the store's `head`. For a store whose descriptor gives `head_recomputes: true`, the check SHALL be a content check: the returned size and SHA-256 SHALL equal the entry's ciphertext size and ciphertext SHA-256. For a store whose descriptor gives `head_recomputes: false`, the check SHALL be a presence check of the same values, and its result SHALL be labelled `presence`. A store whose descriptor does not state `head_recomputes` SHALL get no scheduled check, and the status SHALL report it as not scheduled, with the reason. Each round SHALL take the replicas checked longest ago first, never-checked first, through an index, and SHALL be bounded in time and bytes. A content check SHALL read under a read-rate cap. A check SHALL NOT hold the vault mutation guard. Code SHALL branch on the descriptor value, never on a store's or an adapter's name.

#### Scenario: A rotted replica on a NAS

- **WHEN** one byte of a replica's ciphertext changes in a `filesystem` store whose `head` recomputes content
- **THEN** the next check of that replica quarantines it, and the status reports the finding with the level `content`

#### Scenario: A store that does not recompute

- **WHEN** a replica's store declares `head_recomputes: false` and the version exists with its recorded size and digest
- **THEN** the replica's last check reads `presence` with its time, never `content`

#### Scenario: A store with no declared descriptor

- **WHEN** a vault entry declares no list of stores, as a Cloud cell's does
- **THEN** no check reads that store
- **AND** the status reports its replicas as not scheduled, with the reason

#### Scenario: An unmounted store

- **WHEN** a store does not answer as itself when its check is due
- **THEN** no replica of that store changes state, and the check tries again on a later round

### Requirement: A sampled check decrypts a replica within a monthly budget

The replica worker SHALL download, decrypt and hash sampled replicas, and SHALL compare the plaintext with the binding's `binary_sha256` and `binary_size`. Each store's sampled checks SHALL stay within a monthly byte budget. The descriptor MAY give it as `sample_bytes_per_month`. The default SHALL be 1 GiB, and zero SHALL turn sampling off. The media ledger SHALL record the bytes spent per store and calendar month in UTC. A sample SHALL take the replica whose last decrypt proof is oldest, never-proven first, among those that fit the remaining budget, and the status SHALL count the replicas skipped for size. A sample SHALL run only for a replica that repair could read: a store whose retrieval class is `immediate`, and a key version whose identity this install can read. The status SHALL give the reason for any other replica. No sampled plaintext SHALL be written to disk.

#### Scenario: A sample proves the key

- **WHEN** a sampled replica decrypts and its plaintext hashes to the binding
- **THEN** the replica's last check reads `decrypted` with its time

#### Scenario: The budget is spent

- **WHEN** a store's sampled checks have used its monthly budget
- **THEN** no further sample reads that store until the next calendar month

#### Scenario: A replica the install cannot decrypt

- **WHEN** a replica's key version has no identity this install can read
- **THEN** it is never sampled, and the status gives the reason `identity_unreadable`

### Requirement: A failed check quarantines the replica, reports it and copies it again

A replica SHALL be quarantined when its store answers that the version is absent, when the store returns another size or SHA-256 than the entry records, or when a sample's plaintext does not match the binding. A store that is unavailable, does not answer as itself or times out SHALL NOT quarantine a replica. A quarantined replica SHALL NOT be a repair candidate, and eviction SHALL NOT drop an original's local bytes while its pointer is quarantined. When eviction's own `head` of the store serving eviction does not match the pointer, eviction SHALL quarantine that pointer and the status SHALL report the finding.

The worker SHALL then write a new version into the same store. The source SHALL be the local original when its bytes hash to the binding, or else another replica that is not quarantined, copied as ciphertext and hashed against its own recorded digest. The publication SHALL replace exactly the quarantined entry in the binding, compared by object and version, under the vault mutation guard. The damaged version SHALL stay in its store, and the finding SHALL name it. With no verified source, the replica SHALL stay quarantined, and the finding SHALL read `no_verified_source`. Check results SHALL live in the derived replica table. A rebuild SHALL keep them for a row whose object and version are unchanged.

#### Scenario: The drill's damaged stored copy

- **WHEN** a replica in the store serving eviction is damaged while the local original is intact, and eviction or a check reads it
- **THEN** the pointer is quarantined and the status reports the finding
- **AND** the worker uploads a new version from the local original, and the binding's pointer names it

#### Scenario: An evicted original with a second replica

- **WHEN** the pointer of an evicted original is quarantined and a replica in another store is not quarantined
- **THEN** the worker copies that replica's ciphertext into the store serving eviction, and the pointer names the new version

#### Scenario: No verified source

- **WHEN** a quarantined replica's original is not on the volume and no other replica is usable
- **THEN** the binding is unchanged, and the finding reads `no_verified_source`

#### Scenario: A rebuild during quarantine

- **WHEN** the daily rebuild runs while a replica is quarantined and its entry is unchanged
- **THEN** the replica is still quarantined after the rebuild
