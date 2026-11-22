## MODIFIED Requirements

### Requirement: A supported filesystem adapter serves a local disk or a mounted NAS

The `filesystem` adapter, which also answers to the name `filesystem-test`, SHALL write versioned ciphertext objects under its root and SHALL expose no deletion. It SHALL NOT create its root. The root SHALL carry a store-identity marker that names the configured store and holds a random identity. Only an explicit owner declaration SHALL write the marker, and it SHALL refuse a root on the vault's own device. The declaration SHALL record the marker's identity in the media ledger, and a later declaration SHALL refuse a root whose marker holds another identity. When the root or its marker is absent, names another store or holds an identity other than the recorded one, the adapter SHALL refuse uploads, reads and `head`, and the store SHALL be unavailable. The adapter SHALL read the marker through the same held view of the root that the operation then reads or writes, so that a volume that goes away in between leaves an unavailable store and never an empty one. An unavailable store SHALL NOT be taken as proof that a replica is gone.

The adapter SHALL need only descriptor-relative operations, create-only publication through a no-replace hard link, directory flushing and unlink. It SHALL serve a root on a mount that refuses an atomic no-replace rename, such as a Windows drive in WSL. It SHALL refuse a root that lacks one of the operations that it needs. A caller that needs an atomic no-replace rename on such a mount SHALL get a refusal and SHALL NOT get a replacing rename.

#### Scenario: An unmounted NAS is unavailable, not empty

- **WHEN** a `filesystem` store's mount point exists but the NAS is not mounted, so the marker is absent
- **THEN** no upload writes under the mount point
- **AND** its replicas keep their recorded state, and the status shows the store as unavailable

#### Scenario: A volume that leaves as an operation starts

- **WHEN** the volume of a `filesystem` store goes away after its marker was last read and before an upload, a read or a `head` holds its root
- **THEN** the operation reports the store as unavailable, a version is not reported absent, and nothing is written under the empty mount point
- **AND** a check quarantines no replica of that store

#### Scenario: Another disk answers to the same name

- **WHEN** a root's marker names the configured store but holds an identity other than the one that the declaration recorded
- **THEN** the store is unavailable and no upload reaches that root

#### Scenario: An owner declares a store

- **WHEN** the owner declares a store whose root is a mounted directory on another device
- **THEN** the root's marker names that store
- **AND** a root on the vault's own device is refused and gets no marker

#### Scenario: A store on a Windows drive in WSL

- **WHEN** the owner declares a store whose root is on a mount that refuses an atomic no-replace rename but supports hard links
- **THEN** the declaration succeeds, and uploads, reads and `head` work
- **AND** opening the vault or the state root on that mount is still refused
