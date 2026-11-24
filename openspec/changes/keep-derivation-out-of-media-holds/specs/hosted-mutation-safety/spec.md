## MODIFIED Requirements

### Requirement: Media Work Uses Bounded Per-Artifact Commit Guards

Watcher, startup, and mutation-classified `process_media(process|retry)` discovery scans and provenance hashing SHALL NOT hold the global vault mutation boundary. Those routes MUST acquire a named writer-fenced boundary per artifact only for live binary/access/confinement/content revalidation and canonical sidecar or durable-job mutation. Standard Markdown sidecar fanout after reconciliation, transcript, or failure writes MUST run after that guard is released. Pathless scans MUST NOT wrap the whole scan in one boundary, and explicit process/retry calls MUST retain their existing idempotency identity and writer-lease refusal behavior.

Background media work SHALL NOT hold the vault mutation boundary while it computes a derived result: model inference, image encoding and a sidecar's derived fan-out run before the boundary is taken or after it is released. A re-embed changes no canonical byte, so it SHALL NOT take the boundary. It SHALL publish through the embedding store's own write transaction, which re-checks the sidecar's file signature, its recall admission and the policy identity it read, and publishes nothing when any of them moved. Work inside a background media hold SHALL NOT grow with a sidecar's length. The one derived step that stays inside a hold is the purge of the rows of a scene-frame set that a reprocess replaces: it runs no model and it precedes the new frames' fan-out.

#### Scenario: Foreground mutation overlaps media provenance hashing

- **WHEN** watcher, startup, or explicit media processing is blocked while hashing a large governed binary
- **THEN** a foreground mutation can acquire and release the same vault boundary
- **AND** media processing later reacquires a named per-artifact guard and revalidates before committing

#### Scenario: Background media commit refreshes derived indexes

- **WHEN** a background reconciliation writes or repairs a canonical media sidecar
- **THEN** exact sidecar/job mutation occurs under the per-artifact guard
- **AND** the resulting derived index fanout occurs after that guard is released

#### Scenario: A note save during a long re-embed commits without waiting

- **WHEN** a background re-embed of a long transcript sidecar is encoding its segments
- **AND** a keyed note save arrives
- **THEN** the save commits without waiting for the re-embed and is not refused as busy
- **AND** no `background_media_reembed_commit` hold exists

#### Scenario: A sidecar edited while its re-embed encodes leaves no rows from the old bytes

- **WHEN** a sidecar's text changes, or the sidecar leaves recall admission, after its re-embed started encoding
- **THEN** the embedding store publishes no row computed from the earlier bytes
- **AND** the sidecar's embedding rows are removed and a deferred semantic upsert is recorded

#### Scenario: Scene frames publish with their fan-out outside the hold

- **WHEN** background clip processing persists the scene frames of a video
- **THEN** the frames' image encoding runs before the boundary is taken
- **AND** the frame files commit under one named hold and their index fan-out runs after that hold is released
- **AND** the frames are findable once the fan-out completes
