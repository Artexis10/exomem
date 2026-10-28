## ADDED Requirements

### Requirement: An evicted original keeps its processing state

On a Cloud cell with the media store, media processing SHALL resolve each original through the store's state function. An evicted original SHALL keep its sidecar, extraction marker, transcript and derived rows, and reconciliation SHALL treat it as unchanged. Eviction SHALL NOT select an original with pending or running processing. An explicit re-process or retry of an evicted original SHALL hydrate and verify it before the worker reads it. Processing status SHALL report an evicted original as stored, not missing.

The status overview SHALL bound stored-original and offload-refusal candidates by the existing job-status limit.
It SHALL use the media ledger to select candidates and the sidecar to resolve each original's state.
It SHALL state that coverage is partial, identify the candidate source and limit, and leave the inventory total unknown.
It SHALL report unavailable custody without creating or repairing the ledger.
Candidate failures SHALL stay in path-bearing rows, so terminal governance can withhold them.
Sibling metadata SHALL NOT reveal candidate counts, truncation flags or failures derived from withheld paths.
An exact-path status request SHALL resolve its sidecar even when the candidate ledger is absent.
It SHALL select exact job rows before counts, errors and bounded projections, and query index demand only for the bound sidecar.
It SHALL return all path-derived facts inside one path-bearing result row that terminal governance can withhold.
It SHALL report unavailable job or index custody as unknown counts, and an empty index queue SHALL NOT imply index freshness.
A current extraction job SHALL supply processing state; an offload-only job SHALL NOT override the sidecar's recorded extraction state.
A completed sidecar state SHALL require a valid transcript marker and an exact original binding.
Sidecar status SHALL identify its source and SHALL NOT imply verification of the current original bytes.
This overview SHALL NOT claim exhaustive enumeration or pagination.

An offload verification failure SHALL remain terminal for the same path and sidecar digest.
A changed sidecar digest or explicit targeted retry SHALL permit another offload attempt.
Retry SHALL preserve a completed extraction and rearm its independent offload demand.
A size refusal SHALL cease to block discovery when its measured ciphertext fits the current configured bound, or that bound is removed.
A refusal before ciphertext is measured SHALL report its ciphertext size as unknown.

#### Scenario: Reconciliation passes over an evicted recording

- **WHEN** periodic reconciliation runs over a completed recording that is evicted
- **THEN** it creates no job and does not rewrite the sidecar

#### Scenario: An owner re-processes an evicted recording

- **WHEN** a caller explicitly re-processes an evicted recording
- **THEN** the original is hydrated and verified first, and the job then runs on the verified bytes

#### Scenario: Status lists an evicted recording

- **WHEN** a caller reads processing status for an evicted recording
- **THEN** the status reports it as stored with its last processing state, not as missing

#### Scenario: Custody no longer contains the stored-original inventory

- **WHEN** the media ledger is missing or unreadable, and an owner requests status for an exact evicted-original path
- **THEN** the sidecar supplies the stored state and last processing state
- **AND** overview inventory coverage remains unavailable or partial, never a claim that no stored originals exist
- **AND** status creates no ledger

#### Scenario: Exact status outlives the global job prefix

- **WHEN** a requested recording has an older job outside the global status limit
- **THEN** exact status reports only that recording's jobs, counts, errors and sidecar index demand
- **AND** unrelated jobs or index demand do not change its result

#### Scenario: A restricted caller reads exact status

- **WHEN** release policy or a content floor withholds the requested original
- **THEN** terminal governance removes its entire result row, including scalar facts and index demand
- **AND** the same rule applies when the original is evicted


#### Scenario: A restricted caller reads a bounded overview

- **WHEN** withheld originals enter the candidate prefix or have unreadable sidecars
- **THEN** terminal governance filters their path-bearing rows
- **AND** sibling coverage metadata discloses no count, truncation or candidate-failure signal about those originals

#### Scenario: A failed offload is repaired

- **WHEN** exact original verification fails before encryption
- **THEN** later discovery leaves that input parked and reports an unknown ciphertext size
- **AND** an explicit targeted retry rearms offload after repair without replacing a completed transcript

#### Scenario: An object bound increases

- **WHEN** a parked size refusal's measured ciphertext fits a raised bound, or the bound is removed
- **THEN** discovery rearms that offload
- **AND** the policy change does not clear a verification refusal
