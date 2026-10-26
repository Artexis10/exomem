## MODIFIED Requirements

### Requirement: Deletion purges media-derived index residue

When a page, media sidecar, or media binary is deleted, the deletion fan-out
SHALL purge every derived index row for that artifact, including CLIP
image/frame vectors and scene-frame derivatives, leaving no sidecar through which
the deleted content remains searchable. Deleting a media binary SHALL trigger the
same fan-out as deleting a Markdown page. A reconcile pass SHALL heal
pre-existing orphaned CLIP rows and `.frames/` directories idempotently. A media
binary that is absent because the media store evicted it is not deleted: the
fan-out and the reconcile pass SHALL resolve each binary through the media
store's state function, and SHALL keep every derived row, frame derivative and
sidecar of an evicted original.

#### Scenario: Deleting an image purges its CLIP rows

- **WHEN** an image (or its sidecar) is deleted
- **THEN** its CLIP vectors are removed and a subsequent visual search cannot
  return the deleted image

#### Scenario: Deleting a video clears frame derivatives

- **WHEN** a video is deleted
- **THEN** its `<video>.frames/` directory, per-frame sidecars, and per-frame
  CLIP rows are removed

#### Scenario: Deleting a media binary triggers fan-out

- **WHEN** a media binary is deleted directly
- **THEN** the deletion fan-out runs for it (not skipped as non-Markdown) and all
  its derived index rows are purged

#### Scenario: Reconcile heals prior orphans

- **WHEN** a vault contains CLIP rows or `.frames/` directories orphaned by a
  prior deletion and reconcile runs
- **THEN** the orphans are removed, and running reconcile again changes nothing

#### Scenario: An evicted image is not an orphan

- **WHEN** an image is evicted to the media store and the deletion fan-out or
  reconcile runs
- **THEN** its CLIP vectors, sidecar and any frame derivatives remain, and a
  visual search still returns it

## ADDED Requirements

### Requirement: Export writes evicted originals byte-identical

A Cloud export SHALL include every original that the media store has evicted, at its vault-relative path, with the exact stored bytes. The export SHALL fetch each evicted original through the media edge, decrypt it inside the export Job, and verify its SHA-256 against the sidecar before it writes the manifest entry. A mismatch or an unreadable object SHALL fail the export without publishing it as complete.

#### Scenario: A vault with evicted recordings is exported

- **WHEN** an owner exports a vault in which some recordings are evicted
- **THEN** the archive contains each recording at its path, byte-identical, and each manifest digest equals its sidecar's SHA-256

#### Scenario: A stored object fails verification during export

- **WHEN** a fetched original's SHA-256 differs from its sidecar
- **THEN** the export fails with no completed archive published
