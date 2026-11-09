## ADDED Requirements

### Requirement: A held zip upload is preserved as members through preserve_artifacts

`preserve_artifacts` SHALL accept an optional `archive` field whose one value is `members`. With it, Exomem SHALL preserve each supplied held upload whose bytes are a zip archive exactly as `/upload` preserves an upload with `archive=members`, as content-addressed members and one export manifest under `Evidence/<scope>/<category>/`. The manifest and every blob SHALL be owner-only whatever `raw_protection` says. Each handle's outcome SHALL keep the states `stored`, `already_stored` and `failed`. A `stored` or `already_stored` outcome SHALL name the export manifest as its `path`, which a collection import takes as `source_ref`. It SHALL report the archive's SHA-256 and size; a `stored` outcome SHALL add its member count and the blobs it wrote, and an `already_stored` outcome the blobs it restored to the pool. A handle that is not a held upload SHALL fail with `INVALID_FILE`, naming the held-upload route, before any byte is fetched. Held bytes that are not a readable zip SHALL fail with `ARCHIVE_INVALID`, write no manifest and leave the hold redeemable. A call that combines `archive` with `adoption` or `transcriptions` SHALL be refused with `INVALID_PRESERVE` before any handle is staged. A held upload for the Sources lane SHALL fail with `HELD_UPLOAD_LANE`, as it does without `archive`.

#### Scenario: An MCP client preserves a held export as members

- **WHEN** an MCP client holds a zip export through `transfer_artifact(hold=true)` and calls `preserve_artifacts` with its handle and `archive: members`
- **THEN** the handle's outcome is `stored` and its `path` is the export manifest
- **AND** a collection import with that manifest as `source_ref` reads the export's members

#### Scenario: A refreshed export shares most members

- **WHEN** the client preserves a later export that adds one member to the members the family holds
- **THEN** the outcome is `stored`, one new blob is written, and the new manifest lists every member

#### Scenario: The same export is preserved again

- **WHEN** the client preserves an export whose SHA-256 a manifest in the family already records
- **THEN** the outcome is `already_stored`, it names that manifest, and no member is written again

#### Scenario: Held bytes that are not a zip

- **WHEN** the client preserves a held file that is not a zip with `archive: members`
- **THEN** the outcome is `failed` with `ARCHIVE_INVALID`, no manifest is written, and the hold stays redeemable

#### Scenario: A fetched attachment with archive

- **WHEN** the client supplies a chat attachment handle with `archive: members`
- **THEN** the outcome is `failed` with `INVALID_FILE`, which names the held-upload route, and nothing is fetched
