## ADDED Requirements

### Requirement: Adoption files a recorded artifact member by rename

Adoption SHALL file an artifact member of a recorded stage through a Source-writer primitive that renames the staged file into place. This applies when the stage and the Knowledge Base share one filesystem. Before the rename, the primitive SHALL compare the staged file's identity with the identity in the stage manifest:
- On a match, the manifest SHA-256 SHALL be the Source's `binary_sha256`, and the primitive SHALL read no byte of the file.
- On a mismatch, or for a manifest entry without an identity, the primitive SHALL hash the file once between two stat checks. It SHALL rename the file only when the digest equals the manifest SHA-256 and the two stats agree. Otherwise the member SHALL stay in the residual as changed.

A rename that fails with a cross-device error SHALL fall back to the streamed copy. The adoption ceiling SHALL apply to that copy and to the hash of a mismatched member, and SHALL NOT apply to a rename after an identity match. After the rename, adoption SHALL queue the artifact's offload job.

#### Scenario: A large video files without a byte read

- **WHEN** adoption applies a 900 MiB staged video whose identity matches its manifest entry, and the adoption ceiling is 200 MiB
- **THEN** the video moves to its Knowledge Base path without a byte read or written
- **AND** the Source's `binary_sha256` equals the manifest SHA-256, and the video's offload job exists when the call returns

#### Scenario: A file above the offloadable limit files and stays

- **WHEN** adoption applies a 1.5 GiB staged video whose identity matches, and the object bound is 1 GiB
- **THEN** the video files by rename and stays on the volume as a resident original

#### Scenario: A restore changed the identity

- **WHEN** a backup restore gave a 150 MiB staged recording a new inode, and its bytes still match the manifest SHA-256
- **THEN** adoption hashes it once, renames it, and files it

#### Scenario: A member changed after the import

- **WHEN** a staged file's identity no longer matches, and its digest differs from the manifest SHA-256
- **THEN** adoption leaves the file in the stage and reports it in the residual as changed

#### Scenario: A mismatched member above the ceiling

- **WHEN** a 600 MiB staged member's identity no longer matches, and the adoption ceiling is 200 MiB
- **THEN** adoption reads no byte of it and reports it in the residual with the code that names the ceiling

#### Scenario: The stage is on another filesystem

- **WHEN** the rename of a 50 MiB member fails with a cross-device error
- **THEN** adoption files the member by the streamed copy and consumes the staged file after the verified filing

### Requirement: A rename survives a stop

The rename primitive SHALL rename before it commits the Source page, and replay SHALL finish or reverse each state without loss:
- **Renamed, Source page not committed.** Replay SHALL compare the binary's device, inode, size and `mtime_ns` with the manifest identity. On a match, it SHALL commit the Source page with the manifest SHA-256. Otherwise it SHALL rename the file back to its staged path.
- **Source page committed.** Replay SHALL settle the member as already applied when the Source's `binary_sha256` equals the manifest SHA-256, without reading bytes. It SHALL queue the offload job if none exists.

A rollback SHALL reverse a rename by renaming the file back to its staged path, and SHALL NOT remove a renamed file.

#### Scenario: A stop after the rename

- **WHEN** the process stops after a member's rename and before its Source page commits, and the file is unchanged
- **THEN** the next `apply` commits the Source page with the manifest SHA-256 and reports the item as applied

#### Scenario: A stop after the commit

- **WHEN** the process stops after a member's Source page commits and before the run records the outcome
- **THEN** the next `apply` reports the item as already applied without reading the file

#### Scenario: A failed batch rolls back

- **WHEN** a later write in the same batch fails after a member's rename
- **THEN** the renamed file returns to its staged path with its bytes intact, and the item stays unfiled

### Requirement: Links resolve against the whole series

When a recorded stage holds a series path list, adoption SHALL pass that list as `whole_scan`. A bare link name then resolves against every file of the series. Adoption SHALL never list or file the series path list itself.

#### Scenario: A name repeats in a later window

- **WHEN** a note in window 6 links a bare name that window 6 holds, and window 7, not yet landed, holds another file with the same name
- **THEN** the link stays unresolved as ambiguous, and it does not resolve to the file in window 6

### Requirement: The collection root of a recorded stage comes from its manifest

Adoption SHALL take the collection root of a recorded stage from its manifest, not from a walk of the stage.

#### Scenario: A stage whose artifacts are all filed

- **WHEN** `start` runs again on a recorded stage whose artifacts are all filed and whose empty folders are removed
- **THEN** the run keeps the collection root and collection paths of the first run

### Requirement: The residual tells the person to keep the source

`adoption_studio(action="finish")` and the status SHALL report the residual of each recorded stage with counts and bytes. While a residual is not empty, the tool text SHALL tell the person to keep the source folder.

#### Scenario: A window keeps a residual

- **WHEN** `finish` runs on a window with two changed members and one member above the ceiling
- **THEN** it reports the three members with their codes and bytes
- **AND** it tells the person to keep the source folder until the residual is empty
