## Purpose

Keep space available on a personal vault disk while preserving the owner’s ability to recover every evicted original from a declared store.

## ADDED Requirements

### Requirement: An owner command makes a declared store serve eviction

An owner command SHALL make one declared `filesystem` store serve eviction on a personal install, and SHALL set the vault disk's free-space floor. Eviction SHALL stay off until the owner runs it. The command SHALL refuse a store that was not declared, a store whose adapter is not `filesystem`, and a store that holds replicas. Its output SHALL say that an evicted original's plaintext leaves any mirror of the vault, and that the store and the exported key are then its recovery path. A second command SHALL turn eviction off, and the existing drain SHALL bring evicted originals back as space allows.

When `EXOMEM_MEDIA_STORE_CONFIG` is unset, a personal install SHALL read its media store configuration from a file under its state root, and the owner command SHALL write the vault's entry there. A set variable SHALL take precedence.

#### Scenario: The owner turns eviction on

- **WHEN** the owner runs the command with a declared `filesystem` store and a free-space floor, and the variable is unset
- **THEN** the state root's configuration file names that store as the vault's `store`, with eviction enabled and a target derived from the floor
- **AND** the output names the recovery path of an evicted original

#### Scenario: A store that holds replicas

- **WHEN** the owner names a declared store that holds a replica of any original
- **THEN** the command refuses and the configuration does not change

#### Scenario: The owner turns eviction off

- **WHEN** the owner turns eviction off while originals are evicted
- **THEN** no further original is evicted, and the drain hydrates evicted originals that fit while the rest stay readable from the store

### Requirement: Eviction on a personal install keeps every original recoverable

On a personal install, eviction SHALL NOT drop an original's local bytes unless the owner has confirmed an export of the decryption identity of its pointer's key version. Eviction SHALL also require that the pointer's version was published at least `eviction_settle_seconds` before the eviction, and that the `head` read at eviction confirms the size and SHA-256 of the whole ciphertext. A pointer without a recorded publication time SHALL keep its original. A configuration without `eviction_settle_seconds` SHALL read as 0.

#### Scenario: The key is not exported

- **WHEN** an idle original's confirmed copy is in the eviction store and the owner has not confirmed the export of its key version
- **THEN** eviction keeps the original, and the status names `key_not_exported` as the reason

#### Scenario: A copy that is still settling

- **WHEN** an original's copy was published less than the settle period ago
- **THEN** eviction keeps the original, and a later pass evicts it once the period has passed and the copy still hashes correctly

#### Scenario: The store is not mounted when an evicted original is read

- **WHEN** something reads an evicted original while the eviction store's volume is not mounted
- **THEN** the read fails with `MEDIA_STORE_UNAVAILABLE`, nothing is written at the original's path, and no replica is quarantined

### Requirement: Uploads keep a free-space floor on the vault disk

An upload of an original SHALL be refused with `MEDIA_SPACE_BUSY` and a retry hint when the vault volume's free space after the upload would fall below the configured `upload_floor_bytes`. The refusal SHALL name the free space, the floor and the upload's size. The one-shot upload, resumable upload sessions and preserve SHALL apply the same check before they admit bytes. A vault without the key SHALL apply no floor.

#### Scenario: An upload that would cross the floor

- **WHEN** the vault volume has less free space than the floor plus an upload's size
- **THEN** the upload is refused with `MEDIA_SPACE_BUSY` and a retry hint, and no bytes of it are kept

### Requirement: The owner status shows what eviction keeps and why

The owner status SHALL report the count and plaintext bytes of local and evicted bound physical originals as of its last complete inventory. Filings of one shared object SHALL contribute one physical original. The status SHALL show the time of that inventory and one wait reason for the last eviction pass. The reason SHALL be `key_not_exported`, `store_unavailable`, `settling`, `nothing_idle` or `below_target`. An unreadable value SHALL show as unknown.

#### Scenario: Eviction waits for the key export

- **WHEN** the vault disk is above its target and every idle original's key version is unexported
- **THEN** the status shows the local and evicted counts and the wait reason `key_not_exported`
