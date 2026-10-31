## ADDED Requirements

### Requirement: Adoption files a recorded artifact member by rename

Adoption SHALL file an artifact member of a recorded stage through a Source-writer primitive that renames the staged file into place. This applies when the stage and the Knowledge Base share one filesystem. Before the rename, the primitive SHALL compare the staged file's inode, size, `mtime_ns` and `ctime_ns` with the identity in the stage manifest:
- On a match, the manifest SHA-256 SHALL be the Source's `binary_sha256`, and the primitive SHALL read no byte of the file.
- On a mismatch, or for a manifest entry without an identity, the primitive SHALL hash the file once between two stat checks. It SHALL rename the file only when the digest equals the manifest SHA-256 and the two stats agree. Otherwise the member SHALL stay in the residual as changed.

The rename SHALL NOT replace an existing file. When a file already exists at the Knowledge Base path, the member SHALL stay unfiled, and adoption SHALL report the conflict. The batch writer SHALL commit the Source page, and the renamed binary SHALL NOT be one of its destinations. A rename that fails with a cross-device error SHALL fall back to the streamed copy. The adoption ceiling SHALL apply to that copy and to the hash of a mismatched member. It SHALL NOT apply to a rename after an identity match. After the rename, adoption SHALL queue the artifact's offload job when the classification rule calls the artifact offloadable.

#### Scenario: A large video files without a byte read

- **WHEN** adoption applies a 900 MiB staged video whose identity matches its manifest entry, and the adoption ceiling is 200 MiB
- **THEN** the video moves to its Knowledge Base path without a byte read or written
- **AND** the Source's `binary_sha256` equals the manifest SHA-256, and the video's offload job exists when the call returns

#### Scenario: A file above the offloadable limit files and stays

- **WHEN** adoption applies a 6 GiB staged video whose identity matches, and the largest offloadable file is 5 GiB
- **THEN** the video files by rename, gets no offload job, and stays on the volume as a resident original

#### Scenario: The Knowledge Base path is taken

- **WHEN** a file already exists at a member's Knowledge Base path when adoption renames the member
- **THEN** the existing file keeps its bytes, the member stays in the stage, and adoption reports the conflict

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

The rename primitive SHALL rename before it commits the Source page, and replay SHALL finish each state without loss:
- **Renamed, Source page not committed.** Replay SHALL compare the binary's inode, size and `mtime_ns` with the manifest identity. On a match, it SHALL commit the Source page with the manifest SHA-256. On a mismatch, it SHALL hash the file where it is, within the adoption ceiling. It SHALL commit the Source page when the digest equals the manifest SHA-256. Otherwise it SHALL leave the file unbound at its Knowledge Base path and report it as changed. Replay SHALL NOT move a file back into a stage.
- **Source page committed.** Replay SHALL settle the member as already applied when the Source's `binary_sha256` equals the manifest SHA-256, without reading bytes. It SHALL queue the offload job if none exists and the member is offloadable.

A rollback of a failed batch SHALL rename each renamed binary back to its staged path, without replacing an existing file. It SHALL recreate a staged parent folder that is gone. When the staged path is taken, the binary SHALL stay unbound at its Knowledge Base path, and the run SHALL report it. Neither replay nor rollback SHALL remove a renamed binary.

#### Scenario: A stop after the rename

- **WHEN** the process stops after a member's rename and before its Source page commits, and the file is unchanged
- **THEN** the next `apply` commits the Source page with the manifest SHA-256 and reports the item as applied

#### Scenario: A file changed after the rename

- **WHEN** the process stops after a member's rename, and the binary's size no longer matches the manifest identity
- **THEN** the next `apply` hashes the binary where it is, finds a different digest, and leaves it unbound at its Knowledge Base path
- **AND** it reports the member as changed and moves nothing into the stage

#### Scenario: A stop after the commit

- **WHEN** the process stops after a member's Source page commits and before the run records the outcome
- **THEN** the next `apply` reports the item as already applied without reading the file

#### Scenario: A failed batch rolls back

- **WHEN** a later write in the same batch fails after a member's rename
- **THEN** the renamed file returns to its staged path with its bytes intact, and the item stays unfiled

#### Scenario: A rollback after the staged folder went

- **WHEN** a batch fails after a member's rename, and `finish` has removed the member's empty staged folder
- **THEN** the rollback recreates the folder and renames the file back into it

### Requirement: A discard settles renamed members first

Before `import_discard` asks for a window's discard, it SHALL settle each member of that window that is renamed but not committed, by the replay rule. When replay leaves a member's file unbound, the discard SHALL refuse and name the member and the file's Knowledge Base path. Once that file is moved or deleted, the discard SHALL proceed.

#### Scenario: A discard after a stop

- **WHEN** the person discards a window, and one member is renamed but its Source page is not committed
- **THEN** the discard commits that member's Source page first, and then discards the window

#### Scenario: A member that cannot settle

- **WHEN** a renamed but uncommitted member is above the adoption ceiling, and its identity no longer matches
- **THEN** the discard refuses, names the member and its Knowledge Base path, and keeps the window's manifest
- **AND** after the person deletes that file, the discard proceeds

### Requirement: Links resolve against the whole series

When a recorded stage holds a series file, adoption SHALL pass its path list as `whole_scan`. A bare link name then resolves against every file of the series. Adoption SHALL never list or file the series file itself.

#### Scenario: A name repeats in a later window

- **WHEN** a note in window 6 links a bare name that window 6 holds, and window 7, not yet landed, holds another file with the same name
- **THEN** the link stays unresolved as ambiguous, and it does not resolve to the file in window 6

### Requirement: The collection root of a recorded stage comes from its record

Adoption SHALL take the collection root of a recorded stage from its record, not from a walk of the stage. For a stage with a series file, it SHALL apply the collection-root rule to the series path list. It SHALL take the series label as the default label. For any other recorded stage, it SHALL apply the rule to the manifest's member paths. The series file SHALL NOT count as a member for this rule.

#### Scenario: A stage whose artifacts are all filed

- **WHEN** `start` runs again on a recorded stage whose artifacts are all filed and whose empty folders are removed
- **THEN** the run keeps the collection root and collection paths of the first run

#### Scenario: Two windows of one series

- **WHEN** window 1 holds only files under `Recordings/`, and window 2 holds files under `Recordings/` and `Notes/`
- **THEN** both windows get the same collection root and the series label, and each file's collection path is its path in the source folder

#### Scenario: A resend keeps the first series' root

- **WHEN** a resend window holds only residual members under `Recordings/`, and it carries the first series' file
- **THEN** it gets the first series' collection root and label, and each member's collection path is its path in the source folder

### Requirement: The residual tells the person to keep the source

`adoption_studio(action="finish")` and the status SHALL report the residual of each recorded stage with counts and bytes. They SHALL list its members by path, page by page. The status SHALL also list the members of each residual record as "discarded, not filed". While a landed window has an unfiled member, the tool text SHALL tell the person to keep the source folder. It SHALL also do so while a residual record lists a member. An unfiled member is a manifest member, other than the series file, that no adoption run has filed. A landed window with no run therefore counts every member except the series file. A residual record that lists no member SHALL NOT count. `adoption_studio(action="acknowledge")` SHALL take an import ID and the paths to clear from that window's residual record. It SHALL refuse a call without paths. Its tool text SHALL say that acknowledging is the person's decision.

#### Scenario: A window keeps a residual

- **WHEN** `finish` runs on a window with two changed members and one member above the ceiling
- **THEN** it reports the three members with their codes and bytes
- **AND** it tells the person to keep the source folder until the residual is empty

#### Scenario: Every window is discarded, and a member was never filed

- **WHEN** every window of a series is discarded, and one window's residual record lists a recording
- **THEN** the status lists the recording as "discarded, not filed"
- **AND** the tool text tells the person to keep the source folder

#### Scenario: The person does not want a member

- **WHEN** the person acknowledges one path of a window's residual record
- **THEN** the status no longer lists that path, and it still lists the record's other members

#### Scenario: An acknowledgement without paths

- **WHEN** an agent calls `acknowledge` with an import ID and no paths
- **THEN** the call is refused, and the residual record keeps every member

#### Scenario: A window has landed and no run has started

- **WHEN** a window has landed, and no adoption run has started on its stage
- **THEN** the status tells the person to keep the source folder, although no residual exists yet

#### Scenario: A series is fully filed and discarded

- **WHEN** adoption filed every member of every window of a series, and every window is discarded
- **THEN** the status reports no unfiled member and no residual record that lists a member
- **AND** the tool text no longer tells the person to keep the source folder
