## ADDED Requirements

### Requirement: A staged folder may arrive as an uploaded archive

The action `file`, public as `import_memory` with operation kind `file` and as `manage_memory` `imports.file`, SHALL accept `upload` as an alternative to `path`. `upload` SHALL be the token that `upload_files` returned for the lane `import`. A call that names both `upload` and `path` SHALL be refused with `IMPORT_REQUEST_INVALID`. The first call with an `upload` SHALL unpack the held plain tar, gzip tar or zip into a new `_Imports/<id>`, write the stage record that `exomem import` writes, and then file that stage exactly as `file` files a `path`. Its reply, and every later reply to a call that names an `upload`, SHALL carry the stage's `path`.

The stage's `<id>` SHALL derive from the upload, so that a call repeated with the same `upload` finds its stage, unpacks nothing again and starts no second run. A call with the stage's `path` SHALL carry on the same run. A call that repeats the `upload` after filing removed the stage SHALL answer as a call with the `path` alone does. A stage SHALL appear under its final name only when it is whole, and no refusal SHALL leave a stage, a hidden unpack folder or a file outside `_Imports` behind. The held archive SHALL be redeemed once and released when the stage is on disk.

The unpack SHALL choose the format from the archive's first bytes, never from its name, and SHALL read a zip only when the file starts with the zip's first entry. It SHALL refuse, with a coded refusal that names the member where one is at fault, a link, a hardlink, a device, a sparse entry, an absolute path, a `..` path, a path that resolves outside the stage, two paths that differ only in letter case, a name that is too long, holds control characters or that the volume cannot store, and a zip entry that is not stored or deflated. It SHALL refuse a compression other than gzip. It SHALL refuse an archive of more than 65,536 entries, a pax or long-name header of more than 1 MiB, and one whose members unpack to more bytes than `EXOMEM_IMPORT_MAX_EXPANDED_BYTES` allows, counting the bytes it writes, and SHALL NOT inflate more than that and the headers' share. It SHALL stop reading a tar one record after its end-of-archive block. A refusal for a reason that the same bytes would meet again SHALL release the hold. When the volume cannot hold the archive within the import reserve, the call SHALL answer `filing` `stopped` with the code `INSUFFICIENT_SPACE` and a `free_space` need for the person, and SHALL keep the hold; the need SHALL say that the archive is still held only when it is, and otherwise tell the agent to send it again. A stage that no record vouches for SHALL carry a `discard` need that says its staged copies are kept. A call whose upload no session hold matches SHALL be refused with `IMPORT_UPLOAD_UNAVAILABLE`, which says to call `upload_files` again.

#### Scenario: An agent imports a folder it packed as a tar

- **WHEN** an agent calls `upload_files` with lane `import`, POSTs the tar to the returned URL, and repeats `imports.file` with the returned token and each `continuation` until `filing` is not `working`
- **THEN** every distinct file is filed once, `progress.skipped_duplicates` counts the files whose bytes repeat, and the reply names the stage `path`

#### Scenario: An agent imports a folder it packed as a zip

- **WHEN** the same journey sends a zip
- **THEN** the import reaches `done` with the same counts

#### Scenario: A repeated first call unpacks nothing again

- **WHEN** an agent repeats `imports.file` with the same `upload` under a new idempotency key, then with the `path` that the first reply named
- **THEN** each call carries on the same import, no second run starts and the archive is not unpacked a second time

#### Scenario: A plain tar that holds a zip imports every member

- **WHEN** an uncompressed tar's last member is a zip
- **THEN** every member of the tar is staged, and the zip is one more file of the folder

#### Scenario: An unsafe or oversized archive leaves no stage

- **WHEN** an archive holds a `..` path, an absolute path, a symlink, a hardlink, a device, two paths that differ only in letter case, a name the volume cannot store, more than 65,536 entries, or members that unpack past the cap
- **THEN** the call is refused with a code that names the member, nothing is left under `_Imports`, and a repeat of the call is refused with `IMPORT_UPLOAD_UNAVAILABLE`

#### Scenario: A compression with no output bound is refused

- **WHEN** the archive is a bzip2 or xz tar, or a zip with a member that is neither stored nor deflated
- **THEN** it is refused before it is decompressed, and nothing is left under `_Imports`

#### Scenario: Padding after the end block is never read

- **WHEN** a gzip tar is followed, after its end-of-archive block, by a gigabyte of zeros
- **THEN** the unpack finishes without reading them

#### Scenario: A full volume stops the import and keeps the archive

- **WHEN** the volume cannot hold the archive within the import reserve
- **THEN** the reply is `filing` `stopped` with `INSUFFICIENT_SPACE`, and a repeat after room is made unpacks the same archive and files it

#### Scenario: Both an upload and a path

- **WHEN** a call names an `upload` and a `path`
- **THEN** it is refused with `IMPORT_REQUEST_INVALID` and nothing is unpacked
