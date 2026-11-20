# Spec Delta

## Purpose

Keep verified copies of each original in owner-declared stores, report how protected each original is, and make every recoverable copy available to repair.

## ADDED Requirements

### Requirement: Stores are declared with a capability descriptor

The owner SHALL declare each store in the media store configuration. Each store entry SHALL have a name, an adapter, and a capability descriptor with these fields:

- failure domain;
- immutability (`none`, `governance` or `compliance`);
- checksum kind;
- whether `head` recomputes content;
- retrieval class (`immediate`, `minutes` or `hours`);
- deletion capability;
- cost per GB-month, egress price and minimum retention days.

A store whose entry fails the schema SHALL be unavailable. The owner status SHALL name the field that failed. Code SHALL branch only on descriptor field values, never on a store's name.

#### Scenario: A complete descriptor is accepted

- **WHEN** the owner declares a `filesystem` store with every descriptor field
- **THEN** the store is available for copies
- **AND** the status lists its failure domain and retrieval class

#### Scenario: An incomplete descriptor is refused

- **WHEN** a store entry omits its failure domain
- **THEN** no copy is made to that store
- **AND** the status names the store and the missing field

### Requirement: A supported filesystem adapter serves a local disk or a mounted NAS

The `filesystem` adapter SHALL write versioned ciphertext objects under its root and SHALL expose no deletion. When its root is absent, the adapter SHALL report the store as unavailable. It SHALL NOT treat an absent root as proof that a copy is gone.

#### Scenario: An unmounted NAS is unavailable, not empty

- **WHEN** a `filesystem` store's root is not mounted
- **THEN** its replicas keep their recorded state
- **AND** the status shows the store as unavailable

### Requirement: An original's binding lists its replicas

An original's binding SHALL list each copy held outside the original's own path. Each entry SHALL name the store, object, version, ciphertext size and ciphertext SHA-256. A binding with a single stored-object pointer SHALL read as a list with that one entry. A writer SHALL keep every entry it does not change, and SHALL keep the earliest entry first.

#### Scenario: A single pointer reads as one replica

- **WHEN** a binding written before this change holds one stored-object pointer
- **THEN** the original has one replica with that pointer's store, object and version

#### Scenario: Adding a replica keeps the existing one

- **WHEN** the owner copies an original that has a Cloud or legacy pointer to a second store
- **THEN** the binding lists both replicas, with the earlier one first

### Requirement: A replica is recoverable only when its upload and its key are verified

A replica SHALL count as recoverable only when both conditions hold:

- its upload matched the declared plaintext size and SHA-256;
- the replica's decryption identity survives the loss of the primary machine. For a personal install, the owner has confirmed an export of the identity outside that machine. For a Cloud cell, platform key custody wraps the identity.

The owner status SHALL show any other replica as present but not recoverable, and SHALL give the reason. Recoverability is a status about surviving the loss of the primary machine. It SHALL NOT stop fixity from repairing from a replica whose identity this install can read.

#### Scenario: A copy with an unexported key

- **WHEN** an original has a verified copy in a second store and the owner has not confirmed the key export
- **THEN** the status shows one recoverable copy (the local original) and one copy that is not recoverable, with the reason "key not exported"

### Requirement: The owner copies originals to a store in bounded background batches

One owner operation SHALL copy originals that lack a replica in a named store. It SHALL run in bounded batches through the media job ledger and SHALL report the counts queued, copied, refused and remaining. The operation SHALL hold the vault mutation guard only while it publishes a replica entry, never while it hashes, encrypts or uploads. A retry after a lost reply SHALL reuse the recorded object name.

#### Scenario: Copying an archive to a NAS

- **WHEN** the owner copies 10,000 originals to a `filesystem` store
- **THEN** each original's binding gains one replica entry for that store
- **AND** foreground note writes proceed during the copy without a busy refusal caused by the copy

#### Scenario: A lost reply

- **WHEN** an upload lands and its reply is lost
- **THEN** the retry uses the same object name
- **AND** the store holds one object for that original

### Requirement: The owner sees each original's protection

The owner status SHALL report, per store and in aggregate:

- replicas;
- recoverable replicas;
- distinct failure domains;
- the oldest verification time.

A per-original view SHALL page through results by path. A value that the status cannot read SHALL show as unknown, never as zero. The aggregate SHALL be one bounded query over indexed verification state.

#### Scenario: An unreadable state table

- **WHEN** the verification table is missing
- **THEN** the status shows replica counts from the bindings and the verification times as unknown
- **AND** the table is rebuilt from the bindings
