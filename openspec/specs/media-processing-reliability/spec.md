# media-processing-reliability Specification

## Purpose
Keep canonical media sidecars durable and truthful when Windows sharing,
post-commit derived fanout, or ambiguous historical batch outcomes fail.
## Requirements
### Requirement: Windows Sharing Violations Receive Bounded Guarded Retries

At the guarded atomic sidecar commit boundary, Exomem SHALL treat only Windows `PermissionError` values with `winerror` 5 or 32 as transient sharing violations. It SHALL requeue the claimed media job without consuming a media attempt, preserve its attempt accounting, exit through the existing lock-unavailable path, and let the supervisor apply its normal backoff. Each retry SHALL rerun the complete guarded commit path against fresh source, destination, and mutation state. No job SHALL receive more than three automatic sharing retries. Startup recovery SHALL requeue only failures with the exact Exomem staged atomic-replacement signature and remaining retry allowance; unrelated permission failures and exhausted sharing violations SHALL remain actionable terminal failures.

#### Scenario: Transient sharing denial recovers safely

- **WHEN** replacement of an existing sidecar raises Windows sharing error 5 or 32 before the retry ceiling and a later guarded attempt succeeds
- **THEN** the job is requeued through normal supervisor backoff, the destination remains intact between attempts, and the successful attempt commits once

#### Scenario: Sharing retry is bounded

- **WHEN** the exact Windows sharing violation persists through three automatic retries
- **THEN** the job remains an actionable terminal failure and Exomem does not loop around `os.replace` or weaken path and mutation guards

#### Scenario: Startup recovery refuses unrelated permission failures

- **WHEN** a historical failure lacks the exact staged atomic-replacement signature, has another permission error, or exhausted its sharing retry allowance
- **THEN** startup recovery leaves it failed for operator action

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
