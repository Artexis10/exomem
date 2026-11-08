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

A Cloud export SHALL run in three steps: a pause, a pack and an upload. The pause SHALL run under the export hold, copy every archive member that is present on the volume into the export's staging claim, and write a plan that lists those members and each evicted original with its object name, version ID, plaintext size and SHA-256. The hold SHALL end when the pause reports, so the vault pauses only for the local copy. The pause SHALL hold no media key and have no egress.

The pack SHALL fetch each evicted original by its version ID through the media edge, decrypt it, and verify its size and SHA-256 against the plan before the archive completes. The pack SHALL have no egress except the media edge. A separate upload step that mounts no media key SHALL transfer the archive. A mismatch, an absent version or an unreadable object SHALL fail the export without publishing it as complete.

An export SHALL NOT omit a file without saying so. Originals under Trash SHALL be members like live ones. A companion that the pause cannot read SHALL be copied as a member, and the export SHALL report the count of such companions as partial. An evicted original that the cell cannot reach SHALL fail the export with a named code.

The pause SHALL have its own deadline: its pod runs within 2 minutes, and then copies at least a configured floor rate. On a miss the export SHALL fail and release the hold, without retrying. The archive's limits, parts and deadline SHALL follow the planned archive bytes, not the cell size.

#### Scenario: A vault with evicted recordings is exported

- **WHEN** an owner exports a vault in which some recordings are evicted
- **THEN** the archive contains each recording at its path, byte-identical, and each manifest digest equals its sidecar's SHA-256

#### Scenario: A stored object fails verification during export

- **WHEN** a fetched original's SHA-256 differs from its sidecar
- **THEN** the export fails with no completed archive published

#### Scenario: The media key stays out of the egress pod

- **WHEN** an export of a vault with evicted originals runs
- **THEN** the pod that decrypts originals reaches only the media edge, the pod that uploads the archive holds no media key, and the pod that copies under the hold holds no key and has no egress

#### Scenario: The vault serves again after the local copy

- **WHEN** the pause has copied the present members and written its plan
- **THEN** the hold ends and the runtime starts while the pack and the upload run

#### Scenario: An evicted original in Trash is exported

- **WHEN** an owner exports a vault whose Trash holds an evicted original
- **THEN** the archive contains it at its Trash path, byte-identical

#### Scenario: A companion cannot be read

- **WHEN** the pause meets a companion that it cannot parse
- **THEN** the companion is in the archive and the export reports itself as partial with one unreadable companion

#### Scenario: A retained store holds an evicted original

- **WHEN** a cell without an edge credential exports a vault with an evicted original
- **THEN** the export fails with `EXPORT_MEDIA_UNREACHABLE`, and no archive omits the original

#### Scenario: The staging claim does not start in time

- **WHEN** the pause pod does not run within 2 minutes
- **THEN** the export fails with a staging diagnostic, the hold ends, and the runtime starts again

#### Scenario: Collection runs during an export

- **WHEN** a hold condemns a version that a running export's plan lists
- **THEN** the pack still reads that version by its ID, because the grace period exceeds the longest export deadline
