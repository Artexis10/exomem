## ADDED Requirements

### Requirement: Process-Lifetime Embedding Matrix Cache

The system SHALL load the embedding (and CLIP) vector matrix from its sidecar at
most once per unchanged sidecar state and reuse it across `find` calls, via a
process-shared per-vault index instance. A brand-new call site MUST NOT construct
an index whose in-memory matrix starts empty and forces a full reload. Startup
warm-up of the matrix MUST prime the same shared instance that `find` reads.

#### Scenario: Repeated finds reuse a single matrix load

- **WHEN** two or more `find` requests run against a vault whose embedding sidecar
  has not changed between them
- **THEN** the vector matrix is loaded from the sidecar at most once for that
  unchanged state
- **AND** the later requests reuse the already-loaded matrix rather than
  re-reading and re-stacking every row

#### Scenario: Warm-up primes the matrix find actually uses

- **WHEN** startup warm-up loads the embedding matrix and a subsequent `find` runs
  with the sidecar unchanged
- **THEN** that `find` reuses the warmed matrix without a fresh full load

### Requirement: Write-Independent Find Latency

An in-process embedding write (upsert or delete) SHALL update the shared in-memory
matrix incrementally so that a concurrent `find` does not pay a full vault-sized
matrix reload per call while the sidecar is being written. A change to the sidecar
made outside the shared instance (for example an out-of-process writer) MUST still
be detected and reflected by the next `find`. An incremental update that cannot be
applied consistently MUST fall back to a correct full reload rather than return a
wrong or partial result.

#### Scenario: In-process write does not force a reload

- **WHEN** a file's rows are upserted or deleted through the shared index while the
  matrix is already loaded
- **THEN** the change is reflected on the next read without a full matrix reload
- **AND** the number of full reloads does not grow with the number of in-process
  writes

#### Scenario: A changed file's search results stay correct after an incremental update

- **WHEN** a file is upserted (including a change to its chunk count) or deleted and
  the matrix is patched in place
- **THEN** a search reflects the new content — the upserted file is findable, a
  deleted file's rows are gone — with the same ranking a full reload would produce

#### Scenario: Out-of-instance sidecar change is still reflected

- **WHEN** the embedding sidecar is modified by a writer that did not go through the
  shared index, advancing the sidecar's freshness
- **THEN** the next `find` detects the change and reflects the new sidecar contents

#### Scenario: An inconsistent incremental update falls back to a full reload

- **WHEN** an incremental matrix update cannot be applied consistently
- **THEN** the cache is invalidated and the next read performs a full reload
- **AND** no `find` returns a torn, partial, or incorrect matrix as a result

### Requirement: Sidecar Concurrency Mode

The embedding and CLIP sidecar connections SHALL use a journaling mode that lets a
reader proceed without blocking a concurrent writer and a writer without blocking
concurrent readers, so `find` latency does not track sidecar write churn. Enabling
this mode MUST soft-fail to the default journal without failing the operation when
the mode is unavailable.

#### Scenario: Reads are not blocked by a concurrent sidecar write

- **WHEN** a `find` reads the sidecar while a backfill is writing it
- **THEN** the read is not serialized behind the writer by the sidecar's journaling
  mode

#### Scenario: Concurrency mode failure is non-fatal

- **WHEN** the concurrency journaling mode cannot be enabled on a sidecar connection
- **THEN** the connection falls back to the default journal
- **AND** the operation still succeeds
