## ADDED Requirements

### Requirement: Vector Table Synchronization

The system SHALL update the corresponding vec0 vector tables in the same transaction as
every sidecar write that mutates the embedding blob tables (`chunks` in
`.embeddings.sqlite`, `images` in `.clip.sqlite`), so a committed sidecar never exposes a
blob/vec row mismatch to readers in the writing process. When a sidecar was written without vec0
maintenance — a pre-existing sidecar from before this capability, or a writer process
where the extension is unavailable — the next vec-aware use MUST detect the mismatch and
rebuild the vec0 rows from the stored blobs without re-embedding and without user action.
Blob tables remain the source of truth: rebuilding vec0 rows MUST never invoke an
embedding model.

#### Scenario: A write keeps blob and vector tables in lockstep

- **WHEN** a file's rows are upserted or deleted through a sidecar writer with the vec0
  backend available
- **THEN** after the write commits, the vec0 table holds exactly one vector row per blob
  row
- **AND** a KNN query against the vec0 table reflects the write with no separate refresh
  step

#### Scenario: A pre-existing sidecar is migrated on first use

- **WHEN** a sidecar created before this capability (blob rows only, no vec0 tables) is
  first used by a vec-aware process
- **THEN** the vec0 tables are created and populated from the stored blobs
- **AND** no embedding model is loaded to do so

#### Scenario: Drift from a non-vec-aware writer self-heals

- **WHEN** a process without the vec0 extension writes blob rows (advancing the sidecar)
  and a vec-aware process later uses the same sidecar
- **THEN** the count mismatch is detected and the vec0 rows are rebuilt from blobs
- **AND** subsequent KNN results reflect the non-vec-aware writer's changes
