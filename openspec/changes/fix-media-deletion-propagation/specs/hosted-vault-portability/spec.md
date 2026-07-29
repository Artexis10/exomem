# hosted-vault-portability

## ADDED Requirements

### Requirement: Deletion purges media-derived index residue

When a page, media sidecar, or media binary is deleted, the deletion fan-out
SHALL purge every derived index row for that artifact, including CLIP
image/frame vectors and scene-frame derivatives, leaving no sidecar through which
the deleted content remains searchable. Deleting a media binary SHALL trigger the
same fan-out as deleting a Markdown page. A reconcile pass SHALL heal
pre-existing orphaned CLIP rows and `.frames/` directories idempotently.

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
