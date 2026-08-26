## ADDED Requirements

### Requirement: Derived Fanout Cannot Terminalize Completed Media Extraction

Before a deferred media sidecar commit, Exomem SHALL admit a revisioned full-refresh receipt for that sidecar; admission failure SHALL abort before floor or canonical mutation. After canonical commit, graph/index failure SHALL NOT fail the media job, roll back the sidecar, or rerun extraction. The admitted receipt SHALL remain until the exact checkpoint is published and every required component either completes or installs a verified durable exact downstream handoff. Any failed, degraded, missing, or unverifiable handoff SHALL retain the receipt. Then only the admitted revision SHALL be cleared.

#### Scenario: Receipt admission fails

- **WHEN** the write-ahead full-refresh receipt cannot be admitted
- **THEN** neither the graph floor nor canonical sidecar changes

#### Scenario: Graph fanout fails after transcript commit

- **WHEN** media extraction commits a transcript and subsequent graph fanout fails
- **THEN** the media job completes and the transcript remains canonical
- **AND** the write-ahead full-refresh receipt remains
- **AND** draining it converges without extraction running again

#### Scenario: Concurrent admission survives success

- **WHEN** another mutation revises the receipt during post-commit convergence
- **THEN** media completion does not clear the newer revision

#### Scenario: Process stops in the post-commit gap

- **WHEN** the process stops after canonical commit but before immediate fanout
- **THEN** the receipt remains queued and the graph floor remains ahead
- **AND** full-receipt drain or watcher startup publishes a recovery checkpoint before rebuild
- **AND** genuine convergence precedes receipt CAS-clear
- **AND** the committed transcript is not extracted again

#### Scenario: Stable graph remains fail-closed

- **WHEN** canonical media is newer than derived graph state
- **THEN** consumers use a verified stable graph or report recovery/unavailability
- **AND** they do not report current solely because the media job completed

### Requirement: Legacy Ambiguous Batch Failures Are Truthful And Governed

A valid stored `BATCH_ROLLBACK_INCOMPLETE` envelope SHALL be reconciliation-required rather than artifact corruption. A trusted `BatchWriteError:` prefix with malformed, truncated, invalid, oversized, or malicious payload SHALL receive the same classification without target authority. Status SHALL replace raw ambiguous malformed text with one bounded stable diagnostic in both per-job and top-level error projections, preserve validated bounded facts, report `retryable=false`, `reconciliation_required=true`, a top-level reconciliation count, and `healthy=false`, and direct the operator to targeted media retry without advising binary repair.

Automatic retry-all SHALL exclude ambiguous jobs. Only reconciliation of sidecar provenance against current binary identity SHALL resolve or retry them. A matching complete transcript SHALL resolve complete. A matching pending sidecar MAY permit one fresh guarded attempt. Missing/conflicting provenance or changed identity SHALL remain blocked/stale. The ambiguous batch SHALL NOT be replayed and retained workspaces SHALL remain inspect-only.

#### Scenario: Status reports incomplete rollback

- **WHEN** a job has an exact `BATCH_ROLLBACK_INCOMPLETE` envelope
- **THEN** aggregate status is unhealthy with reconciliation required
- **AND** the job is non-retryable and does not advise artifact replacement
- **AND** automatic retry-all excludes it

#### Scenario: Malformed trusted envelope fails closed

- **WHEN** an error begins with `BatchWriteError:` but its payload is malformed, truncated, or invalid
- **THEN** it is reconciliation-required and non-retryable without target details

#### Scenario: Matching transcript already committed

- **WHEN** reconciliation finds a complete transcript whose provenance matches the current binary
- **THEN** the job resolves complete without re-extraction

#### Scenario: Matching sidecar remains pending

- **WHEN** reconciliation proves a pending sidecar provenance matches the current binary
- **THEN** targeted retry may permit one fresh guarded attempt
- **AND** it does not replay the ambiguous batch

#### Scenario: Provenance or identity conflicts

- **WHEN** provenance is missing/conflicting or binary/sidecar identity changed
- **THEN** the job remains blocked or stale
- **AND** no old extraction result is retried against the new input
