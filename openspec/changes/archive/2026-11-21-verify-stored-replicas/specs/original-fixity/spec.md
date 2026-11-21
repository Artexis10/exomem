# Spec Delta

## MODIFIED Requirements

### Requirement: A mismatch restores only from a copy whose hash matches the binding

An original whose bytes do not hash to its binding's SHA-256 is damaged. Fixity SHALL restore a damaged original only from a copy whose bytes hash to that SHA-256. Candidate copies SHALL be a pool blob, another stored original, and each replica in the binding's replica list (the pointer and `replicas`) that was verified at upload, is not quarantined, and whose decryption identity this install can read. The route SHALL name a quarantined replica as `quarantined` and SHALL NOT read it. A replica that fixity reads and that does not hash to the binding SHALL be quarantined, as a failed scheduled check is. Fixity SHALL try local copies before replicas, and SHALL read each replica from its own store. It SHALL use only replicas in stores whose retrieval class is `immediate`. It SHALL name any other replica in the route as a candidate that the owner can restore. The route SHALL name each replica that fixity tried, and why it failed. A replica entry that cannot name an object SHALL be a failed candidate with a code in the route, and the pass SHALL go on. A route for a caller outside the vault's owner SHALL NOT name a replica's store, key version, object or version. On a local install, the outside file that a local import journal recorded SHALL also be a candidate. Before it moves anything, fixity SHALL re-check under the vault mutation guard that the file and its binding are unchanged since the hash. It SHALL move the changed bytes to Trash with their protection, and SHALL check the file once more just before the replace. When a verified copy cannot be put in place, fixity SHALL record `corrupt_no_copy` with that copy and the failure code in the route. It SHALL keep the last verification time, and the next pass SHALL try again. It SHALL NOT record the changed bytes' hash as the expected hash. With no matching copy, it SHALL change nothing and record `corrupt_no_copy`. When the changed bytes are in Trash and only the replace failed, a later attempt SHALL NOT put a second copy of the same bytes in Trash. When a copy verified but could not be put in place, fixity SHALL keep that copy in its private state, at most one per row, and the next attempt SHALL try it first and rehash it, so a replica is not downloaded again. Fixity SHALL remove the kept copy after a restore, when the row's hash changes, or when the row is retired.

#### Scenario: A matching copy exists

- **WHEN** a stored original's bytes change while its identity stays the recorded one, and a pool blob holds its recorded bytes
- **THEN** the original again hashes to its recorded SHA-256
- **AND** the corrupted bytes are recoverable from Trash
- **AND** the row is `restored` with the cause `corrupted`

#### Scenario: The first replica is damaged and a second replica matches

- **WHEN** a stored original is corrupted, no local copy matches, its first replica fails decryption or does not hash to the binding, and its second replica matches
- **THEN** fixity restores the original from the second replica
- **AND** the route names both replicas and the failure of the first

#### Scenario: No matching copy exists

- **WHEN** a stored original is corrupted and no copy hashes to its recorded SHA-256
- **THEN** the file is byte-identical to its state before the pass
- **AND** the row is `corrupt_no_copy`, and the binding still names the original hash

#### Scenario: A verified copy cannot be put in place

- **WHEN** a copy verifies but the restore cannot move the changed bytes to Trash
- **THEN** the original is unchanged and the row is `corrupt_no_copy`, with the copy and a failure code in the route
- **AND** the next pass restores the original once the cause is gone
- **AND** when the copy came from a replica, the next pass reads the kept copy and downloads nothing

#### Scenario: An outside program edits an original

- **WHEN** an outside program rewrites a stored original and a matching copy exists
- **THEN** fixity restores the original and keeps the edited bytes in Trash
- **AND** the finding carries the cause `edited`

#### Scenario: A governed write rebinds the path during a pass

- **WHEN** a binding starts to name another hash after fixity hashed its file
- **THEN** fixity moves nothing and restores nothing for the earlier hash

#### Scenario: A replica fails its hash during repair

- **WHEN** fixity reads a replica to repair an original and the plaintext does not hash to the binding
- **THEN** the replica is quarantined, and the route names it with the failure
- **AND** a later repair does not read it while it stays quarantined
