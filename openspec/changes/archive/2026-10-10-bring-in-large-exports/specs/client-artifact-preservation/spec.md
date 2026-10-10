## ADDED Requirements

### Requirement: An archive is preserved as content-addressed members

When a client preserves a zip archive with `archive=members`, Exomem SHALL store each
distinct member once, as a compressed blob named by the SHA-256 of the member's
uncompressed bytes, in a member pool that belongs to one `Evidence/<scope>/<category>/`
family. It SHALL write a manifest that records the archive's SHA-256 and size and each
member's path, SHA-256, size and blob. The manifest and every blob SHALL carry the raw
protection marker, so they are owner-only from their first byte. Exomem SHALL validate the
archive's central directory before it reads any member, and SHALL refuse unsafe paths,
symlink entries, duplicate names, encrypted entries, and member counts or sizes over the
documented limits, writing no manifest. A blob SHALL be written only when the pool lacks
it, and the manifest SHALL be written last, as the commit point. Preserving an archive
whose SHA-256 a manifest in the same family already records SHALL return `already_stored`
and write nothing, except that Exomem SHALL restore each blob that manifest names and the
pool lacks. It SHALL take each such member from the uploaded archive, verify its SHA-256
against the manifest, and report how many blobs it restored.

#### Scenario: A second archive shares most members
- **WHEN** an owner preserves an archive whose members the family already holds, except two
- **THEN** Exomem writes two new blobs and one new manifest that lists every member

#### Scenario: The same archive is preserved twice
- **WHEN** an owner preserves an archive with the same SHA-256 into the same family again
- **THEN** the outcome is `already_stored` and no blob or manifest is written

#### Scenario: Preserving an archive again heals a lost member blob
- **WHEN** a blob that a recorded manifest names is missing and the owner preserves the same archive again
- **THEN** the outcome is `already_stored`, the blob is back with its recorded SHA-256, the outcome reports one restored blob, and nothing else is written

#### Scenario: A hostile archive
- **WHEN** an archive contains a member path that leaves the pool, or a symlink entry
- **THEN** Exomem refuses the archive and writes no manifest
